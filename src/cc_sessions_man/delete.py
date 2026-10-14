"""Move a session's files to the desktop Trash, so a delete can be undone."""

from pathlib import Path

from send2trash import send2trash

from .scan import CLAUDE_HOME, Session, live_sessions


def session_files(s: Session, home: Path = CLAUDE_HOME) -> list[Path]:
    """Everything Claude Code keeps for one session, keyed by its ID.

    The prompt history in ~/.claude/history.jsonl is shared by all sessions and is left alone.
    """
    candidates = [
        s.path,  # the transcript
        s.path.with_suffix(""),  # subagent transcripts, tool results, custom title
        home / "file-history" / s.session_id,  # file backups used by /rewind
        home / "session-env" / s.session_id,
    ]
    return [p for p in candidates if p.exists()]


def trash_session(s: Session, home: Path = CLAUDE_HOME) -> list[Path]:
    """Trash one session's files. Returns the paths moved.

    Checks for a live process now, not the state from when the session was scanned:
    it may have been resumed, or closed, since.
    """
    if s.session_id in live_sessions(home):
        raise ValueError(f"session {s.session_id} is still running")
    moved = []
    for p in session_files(s, home):
        send2trash(p)
        moved.append(p)
    return moved
