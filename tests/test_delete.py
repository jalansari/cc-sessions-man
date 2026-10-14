import asyncio
import json

import pytest

import cc_sessions_man.delete as delete_mod
import cc_sessions_man.ui as ui
from cc_sessions_man.classify import Cache
from cc_sessions_man.scan import RUNNING, parse_transcript


def make_home(tmp_path, ids):
    proj = tmp_path / "projects" / "-w"
    proj.mkdir(parents=True)
    for i, sid in enumerate(ids):
        (proj / f"{sid}.jsonl").write_text("\n".join(json.dumps(e) for e in [
            {"type": "user", "message": {"content": f"prompt {sid}"}, "cwd": "/w", "timestamp": f"2026-10-01T{i // 60:02d}:{i % 60:02d}:00Z"},
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


def test_trash_session_refuses_running(tmp_path, trashed):
    home = make_home(tmp_path, ["aaa"])
    s = parse_transcript(home / "projects" / "-w" / "aaa.jsonl")
    s.state = RUNNING
    with pytest.raises(ValueError):
        delete_mod.trash_session(s, home)
    assert trashed == []


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
        await pilot.press("ctrl+home", "space", "space", "u")  # u does the same, in any terminal
        assert app.marked == set()
        await pilot.press("u")
        assert len(app.marked) == 1

    run_ui(home, monkeypatch, steps)


def test_rename_session_writes_transcript_and_sidecar(tmp_path):
    from cc_sessions_man.rename import rename_session

    home = make_home(tmp_path, ["aaa"])
    path = home / "projects" / "-w" / "aaa.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))  # no trailing newline: must not glue lines
    rename_session(parse_transcript(path), "  my   new title ")
    last = json.loads(path.read_text().splitlines()[-1])
    assert last == {"type": "custom-title", "customTitle": "my new title", "sessionId": "aaa"}
    assert json.loads((path.with_suffix("") / "custom-title.json").read_text()) == {"customTitle": "my new title"}
    assert parse_transcript(path).title == "my new title"

    s = parse_transcript(path)
    s.state = RUNNING
    with pytest.raises(ValueError):
        rename_session(s, "nope")
    with pytest.raises(ValueError):
        rename_session(parse_transcript(path), "   ")


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
