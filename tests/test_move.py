import json
from dataclasses import replace
from pathlib import Path

import pytest

import cc_sessions_man.move as move_mod
import cc_sessions_man.ui as ui
from cc_sessions_man.classify import Cache
from cc_sessions_man.move import move_session, project_dir_name
from cc_sessions_man.scan import parse_transcript, scan
from test_delete import mark_live, run_ui

SID = "11111111-2222-3333-4444-555555555555"


def test_project_dir_name_matches_claude_code():
    # Reference values from Claude Code's own function, run under Node.
    long = "/a/" + "x" * 250
    deep = "/home/ann/" + "deep/" * 45 + "café 😀"
    assert project_dir_name("/home/ann/.claude") == "-home-ann--claude"
    assert project_dir_name("/home/ann/code/my_app2") == "-home-ann-code-my-app2"
    assert project_dir_name("/tmp/😀dir") == "-tmp---dir"  # an emoji is two UTF-16 code units
    assert project_dir_name(long) == "-a-" + "x" * 197 + "-oej0v1"
    assert project_dir_name(deep) == "-home-ann-" + "deep-" * 38 + "-4cz7se"


def make_session(tmp_path, root, cwds, extra_lines=()):
    """A transcript in root's project folder, with one entry per cwd, plus its session folder."""
    home = tmp_path / "home"
    proj = home / "projects" / project_dir_name(str(root))
    proj.mkdir(parents=True)
    (home / "sessions").mkdir()
    lines = [json.dumps({"type": "user", "message": {"content": f"prompt {i}"}, "cwd": str(c),
                         "timestamp": f"2026-10-01T10:0{i}:00Z"}) for i, c in enumerate(cwds)]
    lines += list(extra_lines)
    lines.append(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "done"}]},
                             "timestamp": "2026-10-01T10:09:00Z"}))
    path = proj / f"{SID}.jsonl"
    path.write_text("\n".join(lines) + "\n")
    (proj / SID / "subagents").mkdir(parents=True)
    (proj / SID / "subagents" / "agent-1.jsonl").write_text("{}\n")
    return home, parse_transcript(path)


def test_move_rewrites_cwds_and_moves_the_files(tmp_path):
    root, dest = tmp_path / "old repo", tmp_path / "new.repo"
    (root / "src").mkdir(parents=True)
    (dest / "src").mkdir(parents=True)  # exists in the new folder too
    (root / "build").mkdir()  # doesn't
    untouched = ['{"type": "summary",   "summary": "spacing kept"}', "not json at all"]
    home, s = make_session(tmp_path, root, [root, root / "src", root / "build", tmp_path / "elsewhere", root],
                           untouched)

    path = move_session(s, dest, home)

    assert path == home / "projects" / project_dir_name(str(dest)) / f"{SID}.jsonl"
    assert not s.path.exists() and not s.path.with_suffix("").exists()
    assert (path.with_suffix("") / "subagents" / "agent-1.jsonl").exists()  # session folder came along
    lines = path.read_text().splitlines()
    cwds = [json.loads(line)["cwd"] for line in lines if line.startswith("{") and '"cwd"' in line]
    assert cwds == [str(dest), str(dest / "src"), str(dest), str(dest), str(dest)]
    for line in untouched:
        assert line in lines  # byte for byte
    moved = parse_transcript(path)
    assert (moved.cwd, moved.title, moved.state) == (str(dest), s.title, s.state)


def test_moving_this_kind_of_session(tmp_path):
    # Started in ~/.claude, later ran commands from ~/.claude/projects: both end up in dest.
    root, dest = tmp_path / ".claude", tmp_path / "cc-sessions-man"
    (root / "projects").mkdir(parents=True)
    dest.mkdir()
    home, s = make_session(tmp_path, root, [root, root / "projects", root])
    path = move_session(s, dest, home)
    assert path.parent.name == project_dir_name(str(dest)) and parse_transcript(path).cwd == str(dest)
    assert {e.get("cwd") for e in map(json.loads, path.read_text().splitlines())} - {None} == {str(dest)}


def test_move_refusals_leave_everything_in_place(tmp_path):
    root, dest = tmp_path / "a", tmp_path / "b"
    root.mkdir()
    dest.mkdir()
    home, s = make_session(tmp_path, root, [root])
    before = s.path.read_bytes()

    with pytest.raises(ValueError, match="not a folder"):
        move_session(s, tmp_path / "missing", home)
    with pytest.raises(ValueError, match="already in"):
        move_session(s, root, home)
    taken = home / "projects" / project_dir_name(str(dest))
    taken.mkdir()
    (taken / f"{SID}.jsonl").write_text("")
    with pytest.raises(ValueError, match="already has"):
        move_session(s, dest, home)
    (taken / f"{SID}.jsonl").unlink()
    mark_live(home, SID)
    with pytest.raises(ValueError, match="still running"):
        move_session(s, dest, home)

    assert s.path.read_bytes() == before and s.path.with_suffix("").is_dir()
    assert list(taken.iterdir()) == []


def test_a_failed_move_restores_the_original(tmp_path, monkeypatch):
    root, dest = tmp_path / "a", tmp_path / "b"
    root.mkdir()
    dest.mkdir()
    home, s = make_session(tmp_path, root, [root])
    before = s.path.read_bytes()

    target_dir = home / "projects" / project_dir_name(str(dest))
    real_rename, real_unlink = move_mod.os.rename, move_mod.os.unlink

    def check_restored():
        assert s.path.read_bytes() == before and (s.path.with_suffix("") / "subagents").is_dir()
        assert list(target_dir.iterdir()) == []  # no new transcript, folder or temp file
        assert [x.session_id for x in scan(home, cache_path=None)] == [SID]

    # The session folder can't be moved.
    def no_rename(*_):
        raise OSError("cross-device link")
    monkeypatch.setattr(move_mod.os, "rename", no_rename)
    with pytest.raises(OSError):
        move_session(s, dest, home)
    check_restored()
    monkeypatch.setattr(move_mod.os, "rename", real_rename)

    # The original can't be removed, after everything else has moved: all of it goes back.
    def no_unlink_original(path):
        if Path(path) == s.path:
            raise PermissionError("read-only")
        real_unlink(path)
    monkeypatch.setattr(move_mod.os, "unlink", no_unlink_original)
    with pytest.raises(PermissionError):
        move_session(s, dest, home)
    check_restored()
    monkeypatch.setattr(move_mod.os, "unlink", real_unlink)

    # Writing the new copy fails (disk full): no partial temp file is left behind.
    def full(*_):
        raise OSError("No space left on device")
    monkeypatch.setattr(move_mod.os, "fsync", full)
    with pytest.raises(OSError):
        move_session(s, dest, home)
    check_restored()


def test_new_copy_is_on_disk_before_the_original_is_removed(tmp_path, monkeypatch):
    root, dest = tmp_path / "a", tmp_path / "b"
    root.mkdir()
    dest.mkdir()
    home, s = make_session(tmp_path, root, [root])
    events = []
    real_fsync, real_unlink = move_mod.os.fsync, move_mod.os.unlink
    monkeypatch.setattr(move_mod.os, "fsync", lambda fd: (events.append("fsync"), real_fsync(fd)))
    monkeypatch.setattr(move_mod.os, "unlink", lambda p: (events.append(("unlink", Path(p))), real_unlink(p)))
    move_session(s, dest, home)
    removed = events.index(("unlink", s.path))
    assert events[:removed].count("fsync") >= 2  # the file, and the folder holding its name


def test_a_session_resumed_during_the_move_is_left_alone(tmp_path, monkeypatch):
    root, dest = tmp_path / "a", tmp_path / "b"
    root.mkdir()
    dest.mkdir()
    home, s = make_session(tmp_path, root, [root])
    real_write = move_mod._write_temp

    def write_then_resume(target, lines):
        temp = real_write(target, lines)
        with s.path.open("a") as fh:  # Claude Code appends while the copy is being made
            fh.write(json.dumps({"type": "user", "message": {"content": "new"}, "cwd": str(root)}) + "\n")
        return temp

    monkeypatch.setattr(move_mod, "_write_temp", write_then_resume)
    with pytest.raises(ValueError, match="changed during the move"):
        move_session(s, dest, home)
    assert b'"new"' in s.path.read_bytes() and s.path.with_suffix("").is_dir()
    assert list((home / "projects" / project_dir_name(str(dest))).iterdir()) == []


def test_lines_with_unpaired_surrogates_can_be_moved(tmp_path):
    root, dest = tmp_path / "a", tmp_path / "b"
    root.mkdir()
    dest.mkdir()
    cut = '{"type":"user","message":{"content":"cut mid-emoji \\ud83d"},"cwd":"%s"}' % root
    home, s = make_session(tmp_path, root, [root], [cut])
    path = move_session(s, dest, home)
    entry = next(json.loads(line) for line in path.read_text().splitlines() if "mid-emoji" in line)
    assert entry == {"type": "user", "message": {"content": "cut mid-emoji \ud83d"}, "cwd": str(dest)}


def test_moving_into_a_subfolder_keeps_folders_already_inside_it(tmp_path):
    root = tmp_path / "repo"
    (root / "sub" / "x").mkdir(parents=True)
    home, s = make_session(tmp_path, root, [root, root / "sub" / "x", root / "other"])
    path = move_session(s, root / "sub", home)
    cwds = [json.loads(line).get("cwd") for line in path.read_text().splitlines()]
    assert cwds[:3] == [str(root / "sub"), str(root / "sub" / "x"), str(root / "sub")]


def test_ui_move_keeps_the_label_and_shows_the_new_folder(tmp_path, monkeypatch):
    root, dest = tmp_path / "a", tmp_path / "b"
    root.mkdir()
    dest.mkdir()
    home, s = make_session(tmp_path, root, [root])
    old_path = s.path
    cache = Cache(home / "cache.json")
    cache.put(s, "decision", "pick one")
    cache.save()

    async def steps(app, pilot):
        await pilot.press("m")
        await pilot.pause()
        field = app.screen.query_one("#new-folder", ui.Input)
        assert field.value == str(root)
        field.value = str(dest)
        await pilot.press("enter")
        await pilot.pause()
        row = app.rows[0]
        assert row.session.cwd == str(dest) and row.group == "decision"  # label carried over
        assert row.session.path.parent.name == project_dir_name(str(dest))
        assert Cache(home / "cache.json").get(row.session)["label"] == "decision"
        # A scan that ran during the move can list it in both folders: shown once.
        app.show([s, row.session], {})
        assert app.query_one(ui.DataTable).row_count == 1

    run_ui(home, monkeypatch, steps)
    assert not old_path.exists()


def test_ui_move_refuses_a_running_session_and_bad_folders(tmp_path, monkeypatch):
    root = tmp_path / "a"
    root.mkdir()
    home, s = make_session(tmp_path, root, [root])

    async def steps(app, pilot):
        await pilot.press("m")
        await pilot.pause()
        app.screen.query_one("#new-folder", ui.Input).value = str(tmp_path / "missing")
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(app.screen, ui.MoveSession)  # stays open on a bad folder
        await pilot.press("escape")
        await pilot.pause()
        mark_live(home, SID)
        await pilot.press("m")
        await pilot.pause()
        assert not isinstance(app.screen, ui.MoveSession)

    run_ui(home, monkeypatch, steps)
    assert s.path.exists()


def test_ui_moves_to_the_last_folder_when_the_session_started_elsewhere(tmp_path, monkeypatch):
    # Started in /a (so filed under /a's project folder), last worked in /b.
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    home, s = make_session(tmp_path, a, [a, b])

    async def steps(app, pilot):
        await pilot.press("m")
        await pilot.pause()
        assert app.screen.query_one("#new-folder", ui.Input).value == str(b)
        await pilot.press("enter")  # the folder shown: still a real move, to /b's project
        await pilot.pause()
        assert app.rows[0].session.path.parent.name == project_dir_name(str(b))

    run_ui(home, monkeypatch, steps)


def test_ui_move_updates_rows_replaced_while_the_dialog_was_open(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    home, s = make_session(tmp_path, a, [a])

    async def steps(app, pilot):
        await pilot.press("m")
        await pilot.pause()
        app.rows = [ui.Row(replace(r.session), {}) for r in app.rows]  # an auto-refresh landed
        app.screen.query_one("#new-folder", ui.Input).value = str(b)
        await pilot.press("enter")
        await pilot.pause()
        assert app.rows[0].session.cwd == str(b)
        assert app.rows[0].session.path.parent.name == project_dir_name(str(b))

    run_ui(home, monkeypatch, steps)


def test_a_session_started_during_the_move_is_left_alone(tmp_path, monkeypatch):
    root, dest = tmp_path / "a", tmp_path / "b"
    root.mkdir()
    dest.mkdir()
    home, s = make_session(tmp_path, root, [root])
    real_write = move_mod._write_temp

    def write_then_start(target, lines):
        temp = real_write(target, lines)
        mark_live(home, SID)  # resumed, not yet written anything
        return temp

    monkeypatch.setattr(move_mod, "_write_temp", write_then_start)
    with pytest.raises(ValueError, match="changed during the move"):
        move_session(s, dest, home)
    assert s.path.exists() and list((home / "projects" / project_dir_name(str(dest))).iterdir()) == []


def test_tab_in_the_move_dialog_completes_folders(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    for name in ("proj-one", "proj-two"):
        (tmp_path / "fakehome" / name).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    home, s = make_session(tmp_path, a, [a])

    async def steps(app, pilot):
        await pilot.press("m")
        await pilot.pause()
        field = app.screen.query_one("#new-folder", ui.PathInput)
        field.value = ""
        await pilot.press("tab")
        assert field.value == "~/proj-one" and field.has_focus  # Tab completes, not moves focus
        await pilot.press("tab")
        assert field.value == "~/proj-two" and field.cursor_position == len("~/proj-two")
        await pilot.press("enter")
        await pilot.pause()
        assert app.rows[0].session.cwd == str(tmp_path / "fakehome" / "proj-two")

    run_ui(home, monkeypatch, steps)


def test_move_dialog_takes_a_bare_name_as_a_folder_in_home(tmp_path, monkeypatch):
    a = tmp_path / "a"
    a.mkdir()
    (tmp_path / "fakehome" / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path / "fakehome"))
    monkeypatch.chdir(tmp_path)  # where "proj" doesn't exist
    home, s = make_session(tmp_path, a, [a])

    async def steps(app, pilot):
        await pilot.press("m")
        await pilot.pause()
        app.screen.query_one("#new-folder", ui.PathInput).value = "proj"
        await pilot.press("enter")
        await pilot.pause()
        assert app.rows[0].session.cwd == str(tmp_path / "fakehome" / "proj")

    run_ui(home, monkeypatch, steps)
