import asyncio
import json

import pytest

import cc_sessions_man.delete as delete_mod
import cc_sessions_man.ui as ui
from cc_sessions_man.classify import Cache
from cc_sessions_man.scan import RUNNING, parse_transcript


def make_home(tmp_path, ids, cwd="/w"):
    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    for i, sid in enumerate(ids):
        (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"type": "user", "message": {"content": f"prompt {sid}"}, "cwd": str(cwd), "timestamp": f"2026-10-01T{i // 60:02d}:{i % 60:02d}:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]}, "timestamp": f"2026-10-01T{i // 60:02d}:{i % 60:02d}:30Z"},
        ]))
        (proj / sid / "subagents").mkdir(parents=True)
        (tmp_path / "file-history" / sid).mkdir(parents=True)
        (tmp_path / "session-env" / sid).mkdir(parents=True)
    (tmp_path / "sessions").mkdir()
    return tmp_path


@pytest.fixture
def trashed(monkeypatch):
    """Record what would go to the Trash, and actually remove it so rescans see the change."""
    import shutil
    moved = []

    def fake(p):
        moved.append(p)
        shutil.rmtree(p) if p.is_dir() else p.unlink()

    monkeypatch.setattr(delete_mod, "send2trash", fake)
    return moved


def test_trash_session_moves_all_session_files(tmp_path, trashed):
    home = make_home(tmp_path, ["aaa"])
    s = parse_transcript(home / "projects" / "-w" / "aaa.jsonl")
    delete_mod.trash_session(s, home)
    assert sorted(p.relative_to(home).as_posix() for p in trashed) == [
        "file-history/aaa", "projects/-w/aaa", "projects/-w/aaa.jsonl", "session-env/aaa",
    ]


def mark_live(home, sid):
    """Register a live session the way Claude Code does, using this test's own (alive) pid."""
    import os
    (home / "sessions" / f"{sid}.json").write_text(json.dumps({"pid": os.getpid(), "sessionId": sid}))


def test_trash_session_refuses_running(tmp_path, trashed):
    home = make_home(tmp_path, ["aaa"])
    s = parse_transcript(home / "projects" / "-w" / "aaa.jsonl")
    mark_live(home, "aaa")
    with pytest.raises(ValueError):
        delete_mod.trash_session(s, home)
    assert trashed == []


def test_trash_session_uses_live_state_not_scan_state(tmp_path, trashed):
    home = make_home(tmp_path, ["aaa"])
    s = parse_transcript(home / "projects" / "-w" / "aaa.jsonl")
    s.state = RUNNING  # was open when scanned, has since closed
    delete_mod.trash_session(s, home)
    assert trashed


def run_ui(home, monkeypatch, steps):
    monkeypatch.setattr(ui, "CACHE_PATH", home / "cache.json")
    monkeypatch.setattr(ui, "Cache", lambda _p: Cache(home / "cache.json"))

    async def go():
        app = ui.SessionsApp(home, "haiku", use_llm=False)
        async with app.run_test(size=(160, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await steps(app, pilot)
            return {r.session.session_id for r in app.rows}

    return asyncio.run(go())


def test_ui_cancel_keeps_session(tmp_path, monkeypatch, trashed):
    home = make_home(tmp_path, ["aaa", "bbb"])

    async def steps(app, pilot):
        await pilot.press("d")
        await pilot.pause()
        assert isinstance(app.screen, ui.ConfirmDelete)
        await pilot.press("enter")  # Cancel has focus by default
        await pilot.pause()

    assert run_ui(home, monkeypatch, steps) == {"aaa", "bbb"}
    assert trashed == []


def test_ui_delete_marked_sessions(tmp_path, monkeypatch, trashed):
    home = make_home(tmp_path, ["aaa", "bbb", "ccc"])

    async def steps(app, pilot):
        await pilot.press("space", "space")  # mark the top two rows (newest first)
        await pilot.press("d")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()

    assert run_ui(home, monkeypatch, steps) == {"aaa"}
    assert not (home / "projects" / "-w" / "ccc.jsonl").exists()
    assert (home / "projects" / "-w" / "aaa.jsonl").exists()


def test_ui_streams_classification_in_batches(tmp_path, monkeypatch):
    import threading

    import cc_sessions_man.classify as classify_mod

    home = make_home(tmp_path, [f"s{i:02d}" for i in range(7)])
    gate = threading.Event()
    calls = []

    def fake_run(prompt, model):
        ids = [line.split('"')[1] for line in prompt.splitlines() if line.startswith("<session id=")]
        calls.append(ids)
        if len(calls) > 1:
            gate.wait(5)  # hold later batches until the test has looked at the table
        return [{"id": i, "label": "done", "pending": ""} for i in ids]

    monkeypatch.setattr(classify_mod, "_run_claude", fake_run)
    monkeypatch.setattr(ui, "Cache", lambda _p: Cache(home / "cache.json"))

    async def go():
        app = ui.SessionsApp(home, "haiku", use_llm=True, batch_size=5)
        async with app.run_test(size=(160, 40)) as pilot:
            for _ in range(50):  # wait for the first batch to land
                await pilot.pause(0.05)
                if any(r.group == "done" for r in app.rows):
                    break
            first = sorted(r.group for r in app.rows)
            queued = len(app.classifying)
            gate.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            return first, queued, sorted(r.group for r in app.rows), app.classifying

    first, queued, final, left = asyncio.run(go())
    assert sorted(len(c) for c in calls) == [2, 5]
    assert first.count("done") in (2, 5) and first.count("unclassified") == 7 - first.count("done")
    assert queued == 7 - first.count("done")
    assert final == ["done"] * 7 and left == set()


def test_ui_keeps_selected_row_in_place_on_refresh(tmp_path, monkeypatch):
    ids = [f"s{i:03d}" for i in range(80)]
    home = make_home(tmp_path, ids)
    monkeypatch.setattr(ui, "Cache", lambda _p: Cache(home / "cache.json"))

    async def go():
        app = ui.SessionsApp(home, "haiku", use_llm=False)
        async with app.run_test(size=(160, 30)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            # Alternate labels: 40 "action" rows sort above 40 "done" rows.
            app.apply_batch({sid: {"label": ("action", "done")[i % 2], "pending": ""} for i, sid in enumerate(ids)}, ids, 80)
            await pilot.pause()
            table = app.query_one(ui.DataTable)
            table.move_cursor(row=70)
            table.scroll_to(y=60, animate=False)
            await pilot.pause()
            sid = app.current_row().session.session_id
            before = table.cursor_row - table.scroll_y

            app.render_table()  # plain refresh, e.g. after marking
            await pilot.pause()
            plain = (app.current_row().session.session_id, table.cursor_row - table.scroll_y)

            # a batch result moves the selected "done" row up into the "action" group
            app.apply_batch({sid: {"label": "action", "pending": ""}}, [sid], 1)
            await pilot.pause()
            moved = (app.current_row().session.session_id, table.cursor_row - table.scroll_y, table.cursor_row)
            return sid, before, plain, moved

    sid, before, plain, moved = asyncio.run(go())
    assert before == 10
    assert plain == (sid, 10)
    assert moved[:2] == (sid, 10)  # same row, same screen line...
    assert moved[2] < 40  # ...though it now sits in the action group


def test_ui_all_clears_group_and_search(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa", "bbb", "ccc"])

    async def steps(app, pilot):
        table = app.query_one(ui.DataTable)
        await pilot.press("p")  # nothing needs you here, so the list empties
        await pilot.press("/", *"bbb", "enter")
        await pilot.pause()
        assert table.row_count == 0
        await pilot.press("a")
        await pilot.pause()
        assert app.query_one("#search", ui.Input).value == ""
        assert app.query_one("#group", ui.Select).value == "all"
        assert table.row_count == 3

    run_ui(home, monkeypatch, steps)


def test_ui_shift_space_clears_marks_or_marks_one(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa", "bbb", "ccc"])

    async def steps(app, pilot):
        await pilot.press("shift+space")  # nothing marked: marks the top row, like Space
        assert app.marked == {"ccc"}
        await pilot.press("space")
        assert app.marked == {"ccc", "bbb"}
        await pilot.press("shift+space")  # something marked: clears them all
        assert app.marked == set()
        await pilot.press("ctrl+home", "space", "space", "u")  # u unmarks too, in any terminal
        assert app.marked == set()
        cursor = app.query_one(ui.DataTable).cursor_row
        await pilot.press("u")  # nothing marked: u must not mark anything
        assert app.marked == set()
        assert app.query_one(ui.DataTable).cursor_row == cursor

    run_ui(home, monkeypatch, steps)


def test_rename_session_writes_transcript_and_sidecar(tmp_path):
    from cc_sessions_man.rename import rename_session

    home = make_home(tmp_path, ["aaa"])
    path = home / "projects" / "-w" / "aaa.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))  # no trailing newline: must not glue lines
    rename_session(parse_transcript(path), "  my   new title ", home)
    last = json.loads(path.read_text().splitlines()[-1])
    assert last == {"type": "custom-title", "customTitle": "my new title", "sessionId": "aaa"}
    assert json.loads((path.with_suffix("") / "custom-title.json").read_text()) == {"customTitle": "my new title"}
    assert parse_transcript(path).title == "my new title"

    s = parse_transcript(path)
    s.state = RUNNING  # stale scan state alone doesn't block a rename
    rename_session(s, "after close", home)
    assert parse_transcript(path).title == "after close"
    mark_live(home, "aaa")  # a live process does
    with pytest.raises(ValueError):
        rename_session(parse_transcript(path), "nope", home)
    with pytest.raises(ValueError):
        rename_session(parse_transcript(path), "   ", home)


def test_ui_rename_keeps_label(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])
    cache = Cache(home / "cache.json")
    s = parse_transcript(home / "projects" / "-w" / "aaa.jsonl")
    cache.put(s, "decision", "pick one")
    cache.save()

    async def steps(app, pilot):
        await pilot.press("n")
        await pilot.pause()
        assert isinstance(app.screen, ui.RenameSession)
        await pilot.press("ctrl+a", "ctrl+k", *"renamed", "enter")  # clear the prefilled title, type a new one
        await pilot.pause()
        row = app.current_row()
        assert row.session.title == "renamed"
        assert row.group == "decision"  # label survived the transcript change

    run_ui(home, monkeypatch, steps)
    assert parse_transcript(home / "projects" / "-w" / "aaa.jsonl").title == "renamed"
    fresh = parse_transcript(home / "projects" / "-w" / "aaa.jsonl")
    assert Cache(home / "cache.json").get(fresh)["label"] == "decision"  # no re-classification needed


def test_ui_refresh_is_refused_while_classifying(tmp_path, monkeypatch):
    import threading

    import cc_sessions_man.classify as classify_mod

    home = make_home(tmp_path, [f"s{i}" for i in range(3)])
    gate = threading.Event()
    calls = []

    def slow_run(prompt, model):
        calls.append(prompt)
        gate.wait(5)
        ids = [line.split('"')[1] for line in prompt.splitlines() if line.startswith("<session id=")]
        return [{"id": i, "label": "done", "pending": ""} for i in ids]

    monkeypatch.setattr(classify_mod, "_run_claude", slow_run)
    monkeypatch.setattr(ui, "Cache", lambda _p: Cache(home / "cache.json"))

    async def go():
        app = ui.SessionsApp(home, "haiku", use_llm=True, batch_size=5)
        async with app.run_test(size=(160, 40)) as pilot:
            while not calls:
                await pilot.pause(0.02)
            await pilot.press("r")  # mid-classification
            await pilot.pause()
            refused = app.loading
            gate.set()
            await app.workers.wait_for_complete()
            await pilot.pause()
            after = app.loading
            await pilot.press("r")  # now allowed
            await pilot.pause()
            await app.workers.wait_for_complete()
            return refused, after, [r.group for r in app.rows]

    refused, after, groups = asyncio.run(go())
    assert refused is True and after is False
    assert len(calls) == 1  # no second classify run; the refresh after finishing found it all cached
    assert groups == ["done"] * 3


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


def test_ui_resume_refuses_when_folder_is_gone(tmp_path, monkeypatch):
    notices = []

    def attempt(home):
        async def go():
            app = ui.SessionsApp(home, "haiku", use_llm=False)
            app.notify = lambda msg, **kw: notices.append(kw.get("title"))
            async with app.run_test(size=(160, 40)) as pilot:
                await app.workers.wait_for_complete()
                await pilot.pause()
                await pilot.press("enter")
                await pilot.pause()
            return app.return_value

        return asyncio.run(go())

    monkeypatch.setattr(ui, "Cache", lambda _p: Cache(tmp_path / "cache.json"))
    gone = make_home(tmp_path / "a", ["aaa"], cwd=tmp_path / "deleted-project")
    assert attempt(gone) is None and notices == ["Can't resume"]

    project = tmp_path / "project"
    project.mkdir()
    there = make_home(tmp_path / "b", ["bbb"], cwd=project)
    assert attempt(there).session_id == "bbb"


def test_main_refuses_to_resume_into_missing_folder(tmp_path, monkeypatch, capsys):
    from cc_sessions_man.scan import Session

    s = Session(session_id="x", path=tmp_path / "x.jsonl", mtime=0, cwd=str(tmp_path / "gone"))
    monkeypatch.setattr(ui.SessionsApp, "run", lambda self: s)
    monkeypatch.setattr(ui.os, "execve", lambda *a: pytest.fail("must not exec claude"))
    assert ui.main(["--no-llm", "--claude-home", str(tmp_path)]) == 1
    assert "no longer exists" in capsys.readouterr().err


def test_theme_picker_marks_current_theme(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])

    async def steps(app, pilot):
        app.theme = "nord"
        provider = ui.MarkedThemeProvider(app.screen)
        shown = {hit.text: str(hit.prompt) async for hit in provider.discover()}
        assert shown["nord"] == "* nord"
        assert shown["dracula"] == "  dracula"
        assert all(p.isascii() for p in shown.values())  # no ambiguous-width marker
        found = [str(hit.prompt) async for hit in provider.search("nor")]
        assert found == ["* nord"]

    run_ui(home, monkeypatch, steps)


def test_theme_command_opens_marked_picker(tmp_path, monkeypatch):
    """The built-in "Theme" command must reach our provider, not Textual's plain one."""
    from textual.command import CommandPalette

    home = make_home(tmp_path, ["aaa"])

    async def steps(app, pilot):
        app.action_change_theme()  # what the command palette's "Theme" entry runs
        await pilot.pause()
        assert isinstance(app.screen, CommandPalette)
        assert [type(p) for p in app.screen._providers] == [ui.MarkedThemeProvider]

    run_ui(home, monkeypatch, steps)


def test_s_toggles_folder_sort(tmp_path, monkeypatch):
    # Two folders, each with an "action" and a "done" session, at different times.
    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    (tmp_path / "sessions").mkdir()
    layout = [  # (session id, folder, minute)
        ("b-done-new", "/b", 40), ("a-act-old", "/a", 10), ("b-act-old", "/b", 20),
        ("a-done-new", "/a", 50), ("a-act-new", "/a", 30),
    ]
    for sid, folder, minute in layout:
        (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"type": "user", "message": {"content": sid}, "cwd": folder, "timestamp": f"2026-10-01T10:{minute:02d}:00Z"},
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}, "timestamp": f"2026-10-01T10:{minute:02d}:30Z"},
        ]))
    labels = {sid: {"label": "action" if "act" in sid else "done", "pending": ""} for sid, _, _ in layout}

    async def steps(app, pilot):
        app.apply_batch(labels, list(labels), len(labels))
        await pilot.pause()
        order = lambda: [r.session.session_id for r in app.visible_rows()]
        assert order() == ["a-act-new", "b-act-old", "a-act-old", "a-done-new", "b-done-new"]  # status, then date
        await pilot.press("s")
        assert order() == ["a-act-new", "a-act-old", "a-done-new", "b-act-old", "b-done-new"]  # folder, status, date
        await pilot.press("s")
        assert order() == ["a-act-new", "b-act-old", "a-act-old", "a-done-new", "b-done-new"]  # back again

    run_ui(tmp_path, monkeypatch, steps)


def test_trash_session_tries_every_file_and_reports_failures(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["aaa"])
    s = parse_transcript(home / "projects" / "-w" / "aaa.jsonl")
    moved = []

    def flaky(p):
        if p.name == "aaa" and p.parent.name == "file-history":
            raise OSError("cross-device trash")
        moved.append(p.relative_to(home).as_posix())

    monkeypatch.setattr(delete_mod, "send2trash", flaky)
    with pytest.raises(delete_mod.PartialDelete) as exc:
        delete_mod.trash_session(s, home)
    assert "projects/-w/aaa.jsonl" in moved and "session-env/aaa" in moved  # carried on past the failure
    assert [p.parent.name for p, _ in exc.value.failed] == ["file-history"]


def test_ui_partial_delete_still_removes_the_row(tmp_path, monkeypatch):
    import shutil

    home = make_home(tmp_path, ["aaa", "bbb"])
    notices = []

    def flaky(p):
        if p.parent.name == "file-history":
            raise OSError("permission denied")
        shutil.rmtree(p) if p.is_dir() else p.unlink()

    monkeypatch.setattr(delete_mod, "send2trash", flaky)

    async def steps(app, pilot):
        app.notify = lambda msg, **kw: notices.append(kw.get("title"))
        app.cache.put(app.rows[0].session, "done", "")
        target = app.current_row().session.session_id
        app.delete_sessions([app.current_row().session])
        await pilot.pause()
        assert target not in {r.session.session_id for r in app.rows}
        assert target not in app.cache.data

    run_ui(home, monkeypatch, steps)
    assert "Deleted, but some files were left behind" in notices
    assert "Delete failed" not in notices
