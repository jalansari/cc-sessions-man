import asyncio
import json
import os
import shutil

import pytest

import cc_sessions_man.accounts as accounts
import cc_sessions_man.cli as cli
import cc_sessions_man.ui as ui
from cc_sessions_man.delete import session_files
from cc_sessions_man.move import move_session
from cc_sessions_man.scan import RUNNING, live_sessions, parse_transcript, scan
from cc_sessions_man.terminal import resume_command


@pytest.fixture
def base(tmp_path, monkeypatch):
    """A fake home folder whose ~/.claude* folders are the only ones found."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(accounts, "DEFAULT_CLAUDE_HOME", home / ".claude")
    monkeypatch.setenv(accounts.PATTERN_ENV, str(home / ".claude*"))
    return home


def config(base, name, account="", projects="own"):
    """A config folder. projects: "own", None, or the name of the folder whose
    projects/ it links to.
    """
    d = base / name
    d.mkdir()
    (d / "sessions").mkdir()
    settings = base / ".claude.json" if name == ".claude" else d / ".claude.json"
    oauth = {"oauthAccount": {"accountUuid": account}} if account else {}
    settings.write_text(json.dumps(oauth))
    if projects == "own":
        (d / "projects").mkdir()
    elif projects:
        (d / "projects").symlink_to(base / projects / "projects")
    return d


def transcript(d, sid, owners=(), cwd="/w"):
    """A finished session in d's projects, with a session-owner entry per account."""
    proj = d / "projects" / "-w"
    proj.mkdir(parents=True, exist_ok=True)
    entries = [
        {"type": "session-owner", "accountUuid": a, "sessionId": sid} for a in owners
    ]
    entries += [
        {
            "type": "user",
            "message": {"content": f"prompt {sid}"},
            "cwd": cwd,
            "timestamp": "2026-10-01T00:00:00Z",
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "done"}]},
            "timestamp": "2026-10-01T00:00:30Z",
        },
    ]
    path = proj / f"{sid}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in entries))
    return path


def running(d, sid, cwd="/w"):
    (d / "sessions" / "1.json").write_text(
        json.dumps({"sessionId": sid, "pid": os.getpid(), "cwd": cwd})
    )


@pytest.fixture
def machine(base):
    """~/.claude (account D), ~/.claude_work (account J, sharing ~/.claude's projects),
    ~/.claude_ent (account E, its own projects) and ~/.claude_api (no account, no
    projects), plus the ~/.claude.json file the pattern also matches.
    """
    return {
        "default": config(base, ".claude", "D"),
        "work": config(base, ".claude_work", "J", projects=".claude"),
        "ent": config(base, ".claude_ent", "E"),
        "api": config(base, ".claude_api", projects=None),
    }


def test_discover_finds_each_config_folder_and_its_account(base, machine):
    found = {d.path.name: d for d in accounts.discover()}
    assert sorted(found) == [".claude", ".claude_api", ".claude_ent", ".claude_work"]
    # The default folder's account is in ~/.claude.json, beside it.
    assert found[".claude"].account_uuid == "D"
    assert found[".claude_work"].account_uuid == "J"
    assert found[".claude_api"].account_uuid == ""
    assert found[".claude_work"].projects == (base / ".claude" / "projects").resolve()
    assert found[".claude_work"].projects_linked
    assert found[".claude_api"].projects is None


def test_discover_takes_a_pattern_and_claude_config_dir(base, machine, monkeypatch):
    monkeypatch.setenv(accounts.PATTERN_ENV, str(base / ".claude_e*"))
    assert [d.path.name for d in accounts.discover()] == [".claude_ent"]
    elsewhere = config(base.parent, "elsewhere", "X")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(elsewhere))
    assert {d.path.name for d in accounts.discover()} == {".claude_ent", "elsewhere"}


def test_a_folder_reached_twice_counts_once(base, machine):
    (base / ".claude_alias").symlink_to(base / ".claude_ent")
    names = [d.path.name for d in accounts.discover()]
    assert names.count(".claude_ent") == 1 and ".claude_alias" not in names


def test_each_projects_folder_is_read_once_by_its_real_owner(base, machine):
    # .claude_0 sorts first but only links to .claude's projects.
    config(base, ".claude_0", "Z", projects=".claude")
    roots = accounts.project_roots(accounts.discover())
    owners = {root.parent.name: owner.path.name for root, owner in roots}
    assert owners == {".claude": ".claude", ".claude_ent": ".claude_ent"}


def test_the_last_session_owner_entry_wins(base, machine):
    path = transcript(machine["default"], "s1", owners=["D", "J"])
    assert parse_transcript(path).account_uuid == "J"


def test_sessions_belong_to_their_account_or_their_projects_owner(base, machine):
    transcript(machine["default"], "by-work", owners=["J"])
    transcript(machine["default"], "by-default", owners=["D"])
    transcript(machine["default"], "old", owners=[])  # before session-owner entries
    transcript(machine["default"], "stranger", owners=["not-on-this-machine"])
    transcript(machine["ent"], "ent-old")

    sessions = {s.session_id: s for s in scan(cache_path=None)}

    # Listed once, though .claude_work links to the same projects.
    assert sorted(sessions) == ["by-default", "by-work", "ent-old", "old", "stranger"]
    owner = {sid: os.path.basename(s.config_dir) for sid, s in sessions.items()}
    assert owner == {
        "by-work": ".claude_work",
        "by-default": ".claude",
        "old": ".claude",
        "stranger": ".claude",
        "ent-old": ".claude_ent",
    }


def test_running_sessions_are_found_in_every_config_folder(base, machine):
    transcript(machine["default"], "s1")  # no session-owner: would belong to .claude
    running(machine["work"], "s1")

    assert live_sessions(accounts.discover())["s1"]["configDir"] == str(
        machine["work"].resolve()
    )
    (s,) = scan(cache_path=None)
    assert s.state == RUNNING
    assert s.config_dir == str(machine["work"].resolve())  # where it's running


def test_an_explicit_folder_reads_its_projects_even_through_a_symlink(base, machine):
    transcript(machine["default"], "s1")
    (s,) = scan(machine["work"], cache_path=None)
    assert s.session_id == "s1" and s.config_dir == str(machine["work"].resolve())


def test_resume_uses_the_sessions_config_folder(base, machine):
    transcript(machine["default"], "s1", owners=["J"])
    (s,) = scan(cache_path=None)
    command = resume_command(s, "/bin/claude")
    assert ["env", f"CLAUDE_CONFIG_DIR={machine['work'].resolve()}"] == command[5:7]
    line = cli.resume_command(cli.session_home(s), s.session_id, s.cwd)
    assert f"CLAUDE_CONFIG_DIR={machine['work'].resolve()} claude" in line


def test_delete_finds_backups_in_every_config_folder(base, machine):
    transcript(machine["default"], "s1")
    for d in (machine["default"], machine["work"]):
        (d / "file-history" / "s1").mkdir(parents=True)
    (s,) = scan(cache_path=None)
    files = session_files(s, accounts.discover())
    assert machine["work"].resolve() / "file-history" / "s1" in files
    assert machine["default"].resolve() / "file-history" / "s1" in files


def test_move_keeps_the_session_in_its_projects_folder(base, machine, tmp_path):
    dest = tmp_path / "dest"
    dest.mkdir()
    transcript(machine["ent"], "s1")
    (s,) = scan(cache_path=None)
    target = move_session(s, dest, accounts.discover())
    assert target.parent.parent == (machine["ent"] / "projects").resolve()


def test_cli_json_shows_each_sessions_config_dir(base, machine, capsys):
    transcript(machine["default"], "s1", owners=["J"])
    assert cli.main(["--json", "--no-llm"]) == 0
    (row,) = json.loads(capsys.readouterr().out)
    assert row["config_dir"] == str(machine["work"].resolve())
    assert "CLAUDE_CONFIG_DIR=" in row["resume"]


def test_ui_shows_the_cc_dir_column_and_finds_new_folders_on_refresh(
    base, machine, monkeypatch
):
    transcript(machine["default"], "s1", owners=["J"])
    monkeypatch.setattr(ui, "short_path", lambda p: os.path.basename(p))
    seen = []

    async def steps(app, pilot):
        table = app.query_one(ui.DataTable)
        seen.append(str(list(table.columns.values())[-1].label))
        seen.append({r.session.session_id: str(r.session.config_dir) for r in app.rows})
        seen.append(str(table.get_row_at(0)[-1]))
        new = config(base, ".claude_new", "N")
        transcript(new, "s2")
        app.action_refresh()
        await app.workers.wait_for_complete()
        await pilot.pause()
        seen.append(sorted(r.session.session_id for r in app.rows))

    async def go():
        app = ui.SessionsApp(None, "haiku", use_llm=False)
        async with app.run_test(size=(200, 40)) as pilot:
            await app.workers.wait_for_complete()
            await pilot.pause()
            await steps(app, pilot)

    asyncio.run(go())
    heading, owners, cell, after = seen
    assert heading == "CC dir"
    assert owners == {"s1": str(machine["work"].resolve())}
    assert cell == ".claude_work"
    assert after == ["s1", "s2"]


def test_an_account_only_counts_if_it_shares_the_projects_folder(base, machine):
    # The transcript says .claude_ent's account, but .claude_ent can't see
    # .claude's projects, so claude --resume would fail there.
    transcript(machine["default"], "s1", owners=["E"])
    (s,) = scan(cache_path=None)
    assert s.config_dir == str(machine["default"].resolve())


def test_a_copied_config_folder_doesnt_list_sessions_twice(base, machine):
    old = transcript(machine["default"], "s1")
    shutil.copytree(machine["default"], base / ".claude.bak")
    os.utime(old, (1, 1))  # the copy is now the newer transcript
    sessions = scan(cache_path=None)
    assert [s.session_id for s in sessions] == ["s1"]
    assert sessions[0].config_dir == str((base / ".claude.bak").resolve())


def test_one_folder_still_sees_sessions_running_under_another(base, machine):
    transcript(machine["default"], "s1")
    running(machine["work"], "s1")
    (s,) = scan(machine["default"], cache_path=None)
    assert s.state == RUNNING and s.config_dir == str(machine["work"].resolve())


def test_one_folder_still_finds_backups_kept_under_another(base, machine):
    transcript(machine["default"], "s1")
    (machine["work"] / "file-history" / "s1").mkdir(parents=True)
    (s,) = scan(machine["default"], cache_path=None)
    known = accounts.with_discovered([accounts.config_dir(machine["default"])])
    assert machine["work"].resolve() / "file-history" / "s1" in session_files(s, known)


def test_folders_other_tools_made_are_skipped(base, machine):
    (base / ".claude-server-commander").mkdir()  # no projects, no settings
    names = [d.path.name for d in accounts.discover()]
    assert ".claude-server-commander" not in names


def test_the_default_folder_falls_back_to_settings_inside_it(base, monkeypatch):
    # With CLAUDE_CONFIG_DIR=~/.claude, Claude Code keeps settings inside the folder.
    d = config(base, ".claude")
    (base / ".claude.json").unlink()
    (d / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "inside"}})
    )
    assert accounts.account_of(d.resolve()) == "inside"


def test_a_changed_login_is_picked_up(base, machine):
    settings = machine["ent"] / ".claude.json"
    assert accounts.account_of(machine["ent"].resolve()) == "E"
    settings.write_text(json.dumps({"oauthAccount": {"accountUuid": "E-new-login"}}))
    assert accounts.account_of(machine["ent"].resolve()) == "E-new-login"
