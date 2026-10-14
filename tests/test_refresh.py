"""Refreshing: manual (r), automatic (timer), and classification pauses after errors."""

import asyncio
import json
import threading

import pytest

import cc_sessions_man.classify as classify_mod
import cc_sessions_man.ui as ui
from cc_sessions_man.classify import Cache
from test_delete import make_home, trashed  # noqa: F401 (trashed is a fixture)


def add_session(home, sid, minute=59):
    (home / "projects" / "-w" / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in [
        {"type": "user", "message": {"content": f"prompt {sid}"}, "cwd": "/w", "timestamp": f"2026-10-01T05:{minute:02d}:00Z"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}, "timestamp": f"2026-10-01T05:{minute:02d}:30Z"},
    ]) + "\n")


def ids(app):
    return {r.session.session_id for r in app.rows}


def batch_ids(prompt):
    return [line.split('"')[1] for line in prompt.splitlines() if line.startswith("<session id=")]


def run_app(home, monkeypatch, steps, interval=0, use_llm=False):
    monkeypatch.setattr(ui, "Cache", lambda _p: Cache(home / "cache.json"))

    async def go():
        app = ui.SessionsApp(home, "haiku", use_llm=use_llm, refresh_interval=interval)
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            return await steps(app, pilot)

    return asyncio.run(go())


async def until(pilot, condition, timeout=3.0):
    for _ in range(int(timeout / 0.05)):
        if condition():
            return True
        await pilot.pause(0.05)
    return condition()


async def settle(app, pilot):
    """Wait for any running and queued loads to finish."""
    await until(pilot, lambda: not app.loading and not app.refresh_queued)
    await app.workers.wait_for_complete()
    await pilot.pause()


# Automatic refresh ---------------------------------------------------------------


def test_auto_refresh_picks_up_new_sessions_quietly(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])

    async def steps(app, pilot):
        status_seen = []
        app.set_status = lambda msg: status_seen.append(msg) or ui.SessionsApp.set_status(app, msg)
        add_session(home, "new")  # appears on disk while the app is open
        await until(pilot, lambda: "new" in ids(app))
        return ids(app), status_seen

    found, status_seen = run_app(home, monkeypatch, steps, interval=0.2)
    assert found == {"aaa", "new"}  # no r pressed
    assert "Scanning transcripts…" not in status_seen  # quiet: no flashing status


def test_auto_refresh_waits_while_a_dialog_is_open(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])

    async def steps(app, pilot):
        await pilot.press("n")  # rename dialog open
        await pilot.pause()
        add_session(home, "new")
        await pilot.pause(0.6)  # several ticks
        during = ids(app)
        await pilot.press("escape")
        await until(pilot, lambda: "new" in ids(app))
        return during, ids(app)

    during, after = run_app(home, monkeypatch, steps, interval=0.2)
    assert during == {"aaa"}
    assert after == {"aaa", "new"}


def test_auto_refresh_off_with_zero(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])

    async def steps(app, pilot):
        add_session(home, "new")
        await pilot.pause(0.6)
        return ids(app)

    assert run_app(home, monkeypatch, steps, interval=0) == {"aaa"}


def test_unchanged_refresh_does_not_rebuild_table_or_jump_sideways(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa", "bbb"])

    async def steps(app, pilot):
        renders = []
        real = app.render_table
        app.render_table = lambda: renders.append(1) or real()
        await pilot.pause(0.7)  # several ticks, nothing changed on disk
        quiet = len(renders)
        table = app.query_one(ui.DataTable)
        await pilot.resize_terminal(60, 40)  # narrow: the table scrolls sideways
        await pilot.pause()
        table.scroll_to(x=20, animate=False)
        await pilot.pause()
        add_session(home, "new")  # a real change: rebuilds...
        await until(pilot, lambda: "new" in ids(app))
        return quiet, len(renders), table.scroll_x

    quiet, total, scroll_x = run_app(home, monkeypatch, steps, interval=0.2)
    assert quiet == 0
    assert total >= 1
    assert scroll_x == 20  # ...but keeps the sideways scroll


# Manual refresh while something is running ---------------------------------------


def test_r_during_a_load_is_queued_not_refused(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])
    gate = threading.Event()
    real_scan = ui.scan
    scans = []

    def slow_scan(h):
        scans.append(1)
        if len(scans) == 2:
            gate.wait(5)  # the first refresh is slow
        return real_scan(h)

    monkeypatch.setattr(ui, "scan", slow_scan)

    async def steps(app, pilot):
        notices = []
        app.notify = lambda msg, **kw: notices.append((kw.get("severity"), msg))
        await pilot.press("r")  # starts refresh #1 (scan #2), held by the gate
        await until(pilot, lambda: len(scans) == 2)
        await pilot.press("r")  # pressed while #1 runs
        await pilot.pause()
        queued = app.refresh_queued
        add_session(home, "new")
        gate.set()
        await settle(app, pilot)
        return queued, notices, ids(app)

    queued, notices, found = run_app(home, monkeypatch, steps)
    assert queued is True
    assert not any(sev == "warning" for sev, _ in notices)  # no "still scanning" refusal
    assert len(scans) == 3 and "new" in found  # the queued refresh ran afterwards


# Pausing classification after an error ------------------------------------------------


def test_errors_pause_auto_classification_until_r_succeeds(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])
    state = {"up": False}
    calls = []

    def flaky(prompt, model):
        calls.append(batch_ids(prompt))
        if not state["up"]:
            raise RuntimeError("Not logged in · Please run /login")
        return [{"id": i, "label": "done", "pending": ""} for i in batch_ids(prompt)]

    monkeypatch.setattr(classify_mod, "_run_claude", flaky)
    notices = []  # recorded from startup on, so the first toast counts too
    monkeypatch.setattr(ui.SessionsApp, "notify", lambda self, msg, **kw: notices.append(kw.get("title")))

    async def steps(app, pilot):
        await settle(app, pilot)
        first = (app.classify_paused, [r.group for r in app.rows], len(calls))

        add_session(home, "new")  # a new session while paused
        await until(pilot, lambda: "new" in ids(app))
        await pilot.pause(0.6)  # several more ticks
        paused = (len(calls), {r.session.session_id: r.group for r in app.rows}, app.sub_title)

        state["up"] = True  # logged back in
        await pilot.press("r")  # manual retry
        await settle(app, pilot)
        after_r = (app.classify_paused, {r.group for r in app.rows})

        add_session(home, "newer")  # auto refresh classifies again
        await until(pilot, lambda: any(r.session.session_id == "newer" and r.group == "done" for r in app.rows))
        return first, paused, after_r, {r.session.session_id: r.group for r in app.rows}

    first, paused, after_r, final = run_app(home, monkeypatch, steps, interval=0.2, use_llm=True)
    assert first == (True, ["error"], 1)  # startup run failed: paused
    calls_while_paused, groups, status = paused
    assert calls_while_paused == 1  # no model calls on auto ticks while paused
    assert groups == {"aaa": "error", "new": "unclassified"}  # error kept; new one waits
    assert "paused" in status and "Press r" in status
    assert after_r == (False, {"done"})  # r retried everything and lifted the pause
    assert final["newer"] == "done"
    assert notices.count("Classification failed") == 1  # no repeated toasts


def test_failed_r_keeps_classification_paused(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])
    monkeypatch.setattr(classify_mod, "_run_claude", lambda p, m: (_ for _ in ()).throw(RuntimeError("offline")))

    async def steps(app, pilot):
        app.notify = lambda *a, **kw: None
        await settle(app, pilot)
        await pilot.press("r")
        await settle(app, pilot)
        return app.classify_paused, [r.group for r in app.rows]

    assert run_app(home, monkeypatch, steps, use_llm=True) == (True, ["error"])


# Changes made while a refresh is scanning ------------------------------------------------


def hold_next_scan(monkeypatch):
    """Make the next scan block until released, returning (started, release)."""
    started, release = threading.Event(), threading.Event()
    real_scan = ui.scan
    count = []

    def scan(h):
        count.append(1)
        result = real_scan(h)  # read the disk *before* the user's change...
        if len(count) == 2:
            started.set()
            release.wait(5)  # ...and only hand it over afterwards
        return result

    monkeypatch.setattr(ui, "scan", scan)
    return started, release


def test_delete_during_a_scan_does_not_come_back(tmp_path, monkeypatch, trashed):
    home = make_home(tmp_path, ["aaa", "bbb"])
    started, release = hold_next_scan(monkeypatch)

    async def steps(app, pilot):
        app.notify = lambda *a, **kw: None
        await pilot.press("r")
        await until(pilot, started.is_set)
        app.delete_sessions([next(r.session for r in app.rows if r.session.session_id == "bbb")])
        release.set()
        await settle(app, pilot)
        return ids(app)

    assert run_app(home, monkeypatch, steps) == {"aaa"}


def test_rename_during_a_scan_is_kept(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])
    started, release = hold_next_scan(monkeypatch)

    async def steps(app, pilot):
        app.notify = lambda *a, **kw: None
        await pilot.press("r")
        await until(pilot, started.is_set)
        app.apply_rename(app.rows[0], "new name")
        release.set()
        await settle(app, pilot)
        return app.rows[0].session.title

    assert run_app(home, monkeypatch, steps) == "new name"


def test_marks_survive_a_scan_that_misses_a_session(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa", "bbb"])
    real_scan = ui.scan
    skip = {"bbb"}

    def flaky_scan(h):
        return [s for s in real_scan(h) if s.session_id not in skip]

    async def steps(app, pilot):
        app.marked = {"aaa", "bbb"}
        monkeypatch.setattr(ui, "scan", flaky_scan)  # bbb is briefly unreadable
        await pilot.press("r")
        await settle(app, pilot)
        kept = set(app.marked)
        skip.clear()
        (home / "projects" / "-w" / "aaa.jsonl").unlink()  # aaa really goes away
        await pilot.press("r")
        await settle(app, pilot)
        return kept, set(app.marked)

    kept, after = run_app(home, monkeypatch, steps)
    assert kept == {"aaa", "bbb"}  # bbb's mark kept though it was missing from one scan
    assert after == {"bbb"}  # aaa's transcript is gone: its mark goes too


# The --refresh-interval option ----------------------------------------------------------


def test_refresh_interval_option(tmp_path, monkeypatch, capsys):
    seen = {}

    def fake_run(self):
        seen["interval"] = self.refresh_interval
        return None

    monkeypatch.setattr(ui.SessionsApp, "run", fake_run)
    assert ui.main(["--claude-home", str(tmp_path)]) == 0
    assert seen["interval"] == 30
    for ok in ("5", "1", "0"):
        ui.main(["--claude-home", str(tmp_path), "--refresh-interval", ok])
        assert seen["interval"] == float(ok)
    for bad in ("-1", "0.5", "inf", "nan", "soon"):
        with pytest.raises(SystemExit):
            ui.main(["--claude-home", str(tmp_path), "--refresh-interval", bad])
    err = capsys.readouterr().err
    assert "must be 0 (off) or at least 1" in err and "'soon' is not a number" in err



def test_ui_failed_batch_shows_error_and_retries_on_refresh(tmp_path, monkeypatch):
    import cc_sessions_man.classify as classify_mod

    home = make_home(tmp_path, [f"s{i}" for i in range(4)])
    attempts = []

    def flaky_run(prompt, model):
        ids = [line.split('"')[1] for line in prompt.splitlines() if line.startswith("<session id=")]
        attempts.append(ids)
        if "s0" in ids and sum("s0" in a for a in attempts) == 1:  # first try at s0's batch fails
            raise RuntimeError("Not logged in · Please run /login\nmore detail")
        return [{"id": i, "label": "done", "pending": ""} for i in ids]

    monkeypatch.setattr(classify_mod, "_run_claude", flaky_run)
    monkeypatch.setattr(ui, "Cache", lambda _p: Cache(home / "cache.json"))
    notices = []

    async def go():
        app = ui.SessionsApp(home, "haiku", use_llm=True, batch_size=2)
        app.notify = lambda msg, **kw: notices.append((kw.get("title"), msg))
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            first = {r.session.session_id: (r.group, r.detail) for r in app.rows}
            cached = set(json.loads((home / "cache.json").read_text()))
            await pilot.press("r")  # retry
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
            second = {r.session.session_id: r.group for r in app.rows}
            return first, cached, second

    first, cached, second = asyncio.run(go())
    failed = {sid for sid, (g, _) in first.items() if g == "error"}
    assert "s0" in failed and len(failed) == 2  # the whole batch of 2
    assert all(first[sid][1] == "Not logged in · Please run /login" for sid in failed)
    assert all(g == "done" for sid, (g, _) in first.items() if sid not in failed)
    assert not (failed & cached)  # errors are never cached...
    assert set(second.values()) == {"done"}  # ...so refresh retried and fixed them
    assert sum(1 for title, _ in notices if title == "Classification failed") == 1