"""Resume a session in a new terminal window, so the UI can stay open. Linux only."""

import os
import shlex
import shutil
import subprocess
from pathlib import Path

from .scan import Session, claude_env, session_home

# Run in the new terminal: resume in the session's folder, then stay at a shell there.
# The folder and the command come in as arguments, so nothing needs quoting.
SCRIPT = (
    'if cd "$1"; then shift; "$@"; '
    'else echo "Can\'t open $1, so the session was not resumed."; fi; '
    'exec "${SHELL:-/bin/sh}"'
)

# Terminals whose -e takes the whole command as one string (their -x takes arguments).
# The rest take the program and its arguments after -e, as Debian's
# x-terminal-emulator convention says.
ONE_STRING_E = {
    "terminator",
    "xfce4-terminal",
    "mate-terminal",
    "gnome-terminal.wrapper",
}

# A terminal that can't start usually fails at once: wait this long for that.
LAUNCH_CHECK = 0.5  # seconds


class NoTerminal(RuntimeError):
    """No way to open a terminal window was found."""


def resume_command(s: Session, claude: str, home: Path | None = None) -> list[str]:
    """The command the new terminal runs, with the config folder `home` (default: the
    one the session belongs to).

    CLAUDE_CONFIG_DIR is set (or cleared) in the command itself: a terminal that is
    already running may open the window from its own process, with its own environment.
    """
    env = claude_env(home or session_home(s))
    if "CLAUDE_CONFIG_DIR" in env:
        config = ["env", f"CLAUDE_CONFIG_DIR={env['CLAUDE_CONFIG_DIR']}"]
    else:
        config = ["env", "-u", "CLAUDE_CONFIG_DIR"]
    return ["sh", "-c", SCRIPT, "sh", s.cwd, *config, claude, "--resume", s.session_id]


def terminal_launcher(command: list[str]) -> list[str] | None:
    """The command that opens a terminal window running `command`, or None.

    Tried in order: $TERMINAL, xdg-terminal-exec, x-terminal-emulator.
    xdg-terminal-exec takes the command as arguments; the others get it after -e.
    """
    if terminal := shlex.split(os.environ.get("TERMINAL", "")):
        if path := shutil.which(terminal[0]):
            return with_e([path, *terminal[1:]], command)
    if tool := shutil.which("xdg-terminal-exec"):
        return [tool, *command]
    if tool := shutil.which("x-terminal-emulator"):
        return with_e([tool], command)
    return None


def with_e(terminal: list[str], command: list[str]) -> list[str]:
    """`terminal -e command`, in the form the terminal takes. x-terminal-emulator is a
    link to the real terminal, so it's followed to find which one.
    """
    if os.path.basename(os.path.realpath(terminal[0])) in ONE_STRING_E:
        return [*terminal, "-e", shlex.join(command)]
    return [*terminal, "-e", *command]


def open_in_terminal(s: Session, home: Path | None = None) -> None:
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
    proc = subprocess.Popen(
        launcher,
        cwd=s.cwd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        code = proc.wait(timeout=LAUNCH_CHECK)
    except subprocess.TimeoutExpired:
        return  # still running: the window is open
    # Some terminals hand the window to a running instance and exit straight away.
    if code != 0:
        raise RuntimeError(
            f"{os.path.basename(launcher[0])} exited with code {code} without opening "
            "a window."
        )
