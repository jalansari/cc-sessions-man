"""Resume a session in a new terminal window, so the UI can stay open. Linux only."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .scan import CLAUDE_HOME, Session, claude_env

# Run in the new terminal: resume in the session's folder, then stay at a shell there.
# The folder and the command come in as arguments, so nothing needs quoting.
SCRIPT = 'cd "$1" && shift && "$@"; exec "${SHELL:-/bin/sh}"'


class NoTerminal(RuntimeError):
    """No way to open a terminal window was found."""


def resume_command(s: Session, claude: str, home: Path = CLAUDE_HOME) -> list[str]:
    """The command the new terminal runs.

    CLAUDE_CONFIG_DIR is set (or cleared) in the command itself: a terminal that is
    already running may open the window from its own process, with its own environment.
    """
    env = claude_env(home)
    if "CLAUDE_CONFIG_DIR" in env:
        config = ["env", f"CLAUDE_CONFIG_DIR={env['CLAUDE_CONFIG_DIR']}"]
    else:
        config = ["env", "-u", "CLAUDE_CONFIG_DIR"]
    return ["sh", "-c", SCRIPT, "sh", s.cwd, *config, claude, "--resume", s.session_id]


def terminal_launcher(command: list[str]) -> list[str] | None:
    """The command that opens a terminal window running `command`, or None.

    Tried in order: $TERMINAL, xdg-terminal-exec, x-terminal-emulator. Terminals
    disagree on what -e takes (Terminator wants one string, Konsole accepts either), so
    it gets the command as one shell-quoted string. xdg-terminal-exec takes it as
    arguments.
    """
    if terminal := shlex.split(os.environ.get("TERMINAL", "")):
        if shutil.which(terminal[0]):
            return [*terminal, "-e", shlex.join(command)]
    if tool := shutil.which("xdg-terminal-exec"):
        return [tool, *command]
    if tool := shutil.which("x-terminal-emulator"):
        return [tool, "-e", shlex.join(command)]
    return None


def open_in_terminal(s: Session, home: Path = CLAUDE_HOME) -> None:
    """Start a new terminal window resuming `s`.

    The terminal runs in its own session, so quitting the UI leaves it open.
    """
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("claude is not on PATH")
    launcher = terminal_launcher(resume_command(s, claude, home))
    if launcher is None:
        raise NoTerminal(
            "No terminal found: set $TERMINAL, or install xdg-terminal-exec or "
            "x-terminal-emulator."
        )
    subprocess.Popen(
        launcher,
        cwd=s.cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
