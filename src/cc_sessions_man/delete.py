"""Move a session's files to the desktop Trash, so a delete can be undone."""

from pathlib import Path

from send2trash import send2trash

from .scan import CLAUDE_HOME, Homes, Session, home_paths, live_sessions


def session_files(s: Session, homes: Homes = CLAUDE_HOME) -> list[Path]:
    """Everything Claude Code keeps for one session, keyed by its ID, in any of the
    config folders: each folder it ran under keeps its own backups.

    The prompt history in ~/.claude/history.jsonl is shared by all sessions and is left
    alone.
    """
    candidates = [
        s.path,  # the transcript
        s.path.with_suffix(""),  # subagent transcripts, tool results, custom title
    ]
    for home in home_paths(homes):
        candidates += [
            home / "file-history" / s.session_id,  # file backups used by /rewind
            home / "session-env" / s.session_id,
        ]
    return [p for p in dict.fromkeys(candidates) if p.exists()]


class PartialDelete(Exception):
    """Some of a session's files went to the Trash and some didn't."""

    def __init__(self, moved: list[Path], failed: list[tuple[Path, Exception]]):
        self.moved = moved
        self.failed = failed
        names = ", ".join(f"{p.name}: {exc}" for p, exc in failed)
        super().__init__(f"couldn't move {len(failed)} file(s) to the Trash: {names}")


def trash_session(s: Session, homes: Homes = CLAUDE_HOME) -> list[Path]:
    """Trash one session's files. Returns the paths moved.

    Checks for a live process now, not the state from when the session was scanned:
    it may have been resumed, or closed, since.
    """
    if s.session_id in live_sessions(homes):
        raise ValueError(f"session {s.session_id} is still running")
    moved, failed = [], []
    for p in session_files(s, homes):  # transcript first
        try:
            send2trash(p)
        except Exception as exc:  # keep going: leave as little behind as possible
            failed.append((p, exc))
        else:
            moved.append(p)
    if failed:
        raise PartialDelete(moved, failed)
    return moved
