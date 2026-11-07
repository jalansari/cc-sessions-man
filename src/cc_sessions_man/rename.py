"""Rename a session the way Claude Code's /rename does."""

import json

from .scan import CLAUDE_HOME, Homes, Session, live_sessions


def rename_session(s: Session, title: str, homes: Homes = CLAUDE_HOME) -> None:
    """Set a session's title. Writes both places Claude Code reads it from:

    - a `custom-title` entry appended to the transcript (the last one wins), and
    - `<session-id>/custom-title.json` next to the transcript.
    """
    title = " ".join(title.split())
    if not title:
        raise ValueError("title is empty")
    if s.session_id in live_sessions(homes):  # checked now, not from the scan
        # The live process owns the transcript and may write its own title back.
        raise ValueError(f"session {s.session_id} is still running; use /rename in it")

    entry = {"type": "custom-title", "customTitle": title, "sessionId": s.session_id}
    with s.path.open("rb+") as fh:
        size = fh.seek(0, 2)
        needs_newline = False
        if size:
            fh.seek(-1, 2)
            needs_newline = fh.read(1) != b"\n"
        fh.write((b"\n" if needs_newline else b"") + json.dumps(entry).encode() + b"\n")

    sidecar = s.path.with_suffix("") / "custom-title.json"
    sidecar.parent.mkdir(exist_ok=True)
    sidecar.write_text(json.dumps({"customTitle": title}))
