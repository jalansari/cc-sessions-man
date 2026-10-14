import json
import shutil
import time
from types import SimpleNamespace

import pytest
from test_delete import make_home, run_ui

import cc_sessions_man.terminal as terminal
import cc_sessions_man.ui as ui
from cc_sessions_man.scan import DEFAULT_CLAUDE_HOME

# Fake terminals: Terminator's -e takes one string; Konsole's takes arguments.
ONE_STRING = '[ "$1" = -e ] && exec sh -c "$2"'
ARGUMENTS = '[ "$1" = -e ] && shift && exec "$@"'


def tool(bin_dir, name, body):
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    """A PATH holding only fake tools, and links to the few real ones the command
    needs, so no real terminal can open.
    """
    d = tmp_path / "bin"
    d.mkdir()
    for name in ("sh", "env", "python3"):
        (d / name).symlink_to(shutil.which(name))
    monkeypatch.setenv("PATH", str(d))
    monkeypatch.delenv("TERMINAL", raising=False)
    monkeypatch.setenv("SHELL", "/bin/true")  # the shell left open after claude
    return d


def wait_for(path, timeout=5.0):
    end = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < end:
        time.sleep(0.05)
    assert path.exists(), f"{path.name} was never written"
    return path.read_text()


def fake_claude(bin_dir, out):
    """A claude that records its folder, arguments and CLAUDE_CONFIG_DIR."""
    tool(
        bin_dir,
        "claude",
        'python3 -c \'import json, os, sys; json.dump({"cwd": os.getcwd(), '
        '"args": sys.argv[1:], "config": os.environ.get("CLAUDE_CONFIG_DIR")}, '
        f'open("{out}", "w"))\' "$@"',
    )


def test_resumes_in_the_folder_through_x_terminal_emulator(
    tmp_path, bin_dir, monkeypatch
):
    out = tmp_path / "claude.json"
    fake_claude(bin_dir, out)
    # x-terminal-emulator links to the real terminal, here one taking one string.
    (bin_dir / "x-terminal-emulator").symlink_to(
        tool(bin_dir, "terminator", ONE_STRING)
    )
    folder = tmp_path / "my 'odd' project"
    folder.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/elsewhere")

    session = SimpleNamespace(cwd=str(folder), session_id="abc-123")
    terminal.open_in_terminal(session, DEFAULT_CLAUDE_HOME)

    seen = json.loads(wait_for(out))
    assert seen["cwd"] == str(folder)
    assert seen["args"] == ["--resume", "abc-123"]
    assert seen["config"] is None  # the default home: CLAUDE_CONFIG_DIR is cleared


def test_resumes_through_a_terminal_taking_arguments(tmp_path, bin_dir, monkeypatch):
    out = tmp_path / "claude.json"
    fake_claude(bin_dir, out)
    tool(bin_dir, "konsole", ARGUMENTS)
    monkeypatch.setenv("TERMINAL", "konsole")
    folder = tmp_path / "my 'odd' project"
    folder.mkdir()

    terminal.open_in_terminal(SimpleNamespace(cwd=str(folder), session_id="abc-123"))

    seen = json.loads(wait_for(out))
    assert seen["cwd"] == str(folder) and seen["args"] == ["--resume", "abc-123"]


def test_passes_another_claude_home_on(tmp_path, bin_dir):
    out = tmp_path / "claude.json"
    fake_claude(bin_dir, out)
    tool(bin_dir, "xdg-terminal-exec", 'exec "$@"')  # takes the command as arguments
    home = tmp_path / "claude-work"
    home.mkdir()

    session = SimpleNamespace(cwd=str(tmp_path), session_id="abc-123")
    terminal.open_in_terminal(session, home)

    assert json.loads(wait_for(out))["config"] == str(home.resolve())


def test_says_so_when_the_folder_cant_be_opened(tmp_path, bin_dir):
    out, screen = tmp_path / "claude.json", tmp_path / "screen.txt"
    fake_claude(bin_dir, out)
    # A terminal whose window shows what the command printed.
    tool(bin_dir, "xdg-terminal-exec", f'"$@" > "{screen}" 2>&1')
    folder = tmp_path / "gone"
    folder.mkdir()
    command = terminal.resume_command(
        SimpleNamespace(cwd=str(folder), session_id="abc-123"),
        str(bin_dir / "claude"),
    )
    folder.rmdir()  # removed after the UI's check, before the terminal's cd

    terminal.subprocess.run(terminal.terminal_launcher(command), check=True)

    assert "so the session was not resumed" in wait_for(screen)
    assert not out.exists()


def test_a_terminal_that_fails_at_once_is_an_error(tmp_path, bin_dir):
    fake_claude(bin_dir, tmp_path / "unused.json")
    tool(bin_dir, "xdg-terminal-exec", "exit 3")
    session = SimpleNamespace(cwd=str(tmp_path), session_id="abc-123")
    with pytest.raises(RuntimeError, match="exited with code 3"):
        terminal.open_in_terminal(session)


def test_terminal_order_and_forms(tmp_path, bin_dir, monkeypatch):
    command = ["sh", "-c", "echo 'hi there'"]
    assert terminal.terminal_launcher(command) is None

    xte = tool(bin_dir, "x-terminal-emulator", "")
    assert terminal.terminal_launcher(command) == [str(xte), "-e", *command]

    xte.unlink()
    xte.symlink_to(tool(bin_dir, "terminator", ""))
    assert terminal.terminal_launcher(command) == [
        str(xte),
        "-e",
        "sh -c 'echo '\"'\"'hi there'\"'\"''",
    ]

    xdg = tool(bin_dir, "xdg-terminal-exec", "")
    assert terminal.terminal_launcher(command) == [str(xdg), *command]

    myterm = tool(bin_dir, "myterm", "")
    monkeypatch.setenv("TERMINAL", "myterm --single")
    assert terminal.terminal_launcher(command) == [
        str(myterm),
        "--single",
        "-e",
        *command,
    ]

    monkeypatch.setenv("TERMINAL", "not-installed")
    assert terminal.terminal_launcher(command)[0] == str(xdg)


def test_no_terminal_or_no_claude_raises(tmp_path, bin_dir):
    session = SimpleNamespace(cwd=str(tmp_path), session_id="abc-123")
    with pytest.raises(RuntimeError, match="claude is not on PATH"):
        terminal.open_in_terminal(session)
    fake_claude(bin_dir, tmp_path / "unused.json")
    with pytest.raises(terminal.NoTerminal, match="No terminal found"):
        terminal.open_in_terminal(session)


def run_keys(tmp_path, monkeypatch, keys=("o",), cwd=None, fail=None, live=False):
    """Press keys in the UI; return what o opened, the notices and the rescans."""
    cwd = cwd or str(tmp_path)
    home = make_home(tmp_path, ["a1"], cwd=cwd)
    opened, notices, rescans = [], [], []

    def fake_open(s, home):
        if fail:
            raise fail
        opened.append(s.session_id)

    monkeypatch.setattr(ui, "open_in_terminal", fake_open)
    monkeypatch.setattr(ui, "RESCAN_AFTER_OPEN", 0.01)
    if live:
        monkeypatch.setattr(ui, "live_sessions", lambda _home: {"a1"})

    async def steps(app, pilot):
        app.notify = lambda msg, **kw: notices.append((kw.get("severity"), msg))
        app.auto_refresh = lambda: rescans.append(True)
        for key in keys:
            await pilot.press(key)
        await pilot.pause(0.1)

    run_ui(home, monkeypatch, steps)
    return opened, notices, rescans


def test_o_opens_the_session_and_rescans(tmp_path, monkeypatch):
    opened, notices, rescans = run_keys(tmp_path, monkeypatch)
    assert opened == ["a1"]
    assert notices == [(None, "Resuming prompt a1 in a new terminal.")]
    assert rescans


def test_a_session_just_opened_isnt_opened_again(tmp_path, monkeypatch):
    opened, notices, _ = run_keys(tmp_path, monkeypatch, keys=("o", "o", "enter"))
    assert opened == ["a1"]
    assert (
        notices[1:]
        == [("warning", "That session was just opened in a new terminal.")] * 2
    )


def test_o_refuses_a_running_session(tmp_path, monkeypatch):
    opened, notices, rescans = run_keys(tmp_path, monkeypatch, live=True)
    assert not opened and not rescans
    assert notices == [("warning", "That session is still open in another terminal.")]


def test_o_refuses_a_missing_folder(tmp_path, monkeypatch):
    opened, notices, _ = run_keys(tmp_path, monkeypatch, cwd="/no/such/folder")
    assert not opened
    assert notices[0][0] == "error" and "no longer exists" in notices[0][1]


def test_o_shows_why_it_couldnt_open(tmp_path, monkeypatch):
    fail = terminal.NoTerminal("No terminal found: set $TERMINAL")
    opened, notices, rescans = run_keys(tmp_path, monkeypatch, fail=fail)
    assert not rescans
    assert notices == [("error", "No terminal found: set $TERMINAL")]


def test_o_is_in_the_footer(tmp_path, monkeypatch):
    home = make_home(tmp_path, ["a1"])
    footer = []

    async def steps(app, pilot):
        footer.extend((k.key, k.description) for k in app.screen.query("FooterKey"))

    run_ui(home, monkeypatch, steps)
    assert ("o", "Open") in footer
    assert "slash" not in {key for key, _ in footer}  # named in the search box
