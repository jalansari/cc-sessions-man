"""Move a session to another working folder, so `claude --resume` finds it there."""

import json
import os
import tempfile
from pathlib import Path

from .scan import CLAUDE_HOME, Session, live_sessions

# Claude Code cuts longer project folder names and adds a hash of the full path.
PROJECT_NAME_MAX = 200


def _java_hash(text: str) -> int:
    """Claude Code's string hash: 31 * h + code unit over UTF-16, as a signed 32-bit
    int.
    """
    h = 0
    units = text.encode("utf-16-le")
    for i in range(0, len(units), 2):
        h = (h * 31 + int.from_bytes(units[i : i + 2], "little")) & 0xFFFFFFFF
    return h - (1 << 32) if h >= 1 << 31 else h


def _base36(n: int) -> str:
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    out = ""
    while True:
        n, r = divmod(n, 36)
        out = digits[r] + out
        if not n:
            return out


def project_dir_name(path: str) -> str:
    """The folder under projects/ that Claude Code keeps a working folder's sessions
    in.

    Every character that isn't an ASCII letter or digit becomes "-" (one per UTF-16 code
    unit, as in JavaScript), and names over 200 characters are cut and given a hash
    suffix.
    """
    name = "".join(
        c if c.isascii() and c.isalnum() else "-" * (2 if ord(c) > 0xFFFF else 1)
        for c in path
    )
    if len(name) <= PROJECT_NAME_MAX:
        return name
    return f"{name[:PROJECT_NAME_MAX]}-{_base36(abs(_java_hash(path)))}"


def _launch_dir(cwds: list[str], project_name: str) -> str | None:
    """The folder the session was started in: the one its project folder is named after.

    Later entries can record subfolders the session changed into. Falls back to the
    first.
    """
    return next(
        (c for c in cwds if project_dir_name(c) == project_name),
        cwds[0] if cwds else None,
    )


def _encode(entry: dict) -> bytes:
    """A rewritten line: compact, like Claude Code's own JSON.stringify.

    A string holding an unpaired surrogate (text cut mid-emoji) can't be written as
    UTF-8, so such a line keeps its non-ASCII characters escaped instead; it parses the
    same.
    """
    try:
        return (
            json.dumps(entry, ensure_ascii=False, separators=(",", ":")).encode()
            + b"\n"
        )
    except UnicodeEncodeError:
        return json.dumps(entry, separators=(",", ":")).encode() + b"\n"


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_temp(target: Path, lines: list[bytes]) -> Path:
    """Write `lines` to a new temporary file next to `target`, flushed to disk."""
    with tempfile.NamedTemporaryFile(
        "wb", dir=target.parent, prefix=target.name, suffix=".tmp", delete=False
    ) as fh:
        try:
            fh.writelines(lines)
            fh.flush()
            os.fsync(fh.fileno())
        except BaseException:
            Path(fh.name).unlink(
                missing_ok=True
            )  # e.g. disk full: don't leave a partial copy
            raise
    return Path(fh.name)


def move_session(s: Session, dest: str | Path, home: Path = CLAUDE_HOME) -> Path:
    """Move a session to the working folder `dest`. Returns the new transcript path.

    - Every `cwd` in the transcript is rewritten. A folder already in `dest` stays. The
      launch folder becomes `dest`; a subfolder of it becomes the same subfolder of
      `dest` if that exists, otherwise `dest`. Anything else becomes `dest`. Message
      text, including paths in it, is left alone.
    - The transcript and its `<session-id>/` folder (subagents, tool output, title) move
      to `projects/<dest's project name>/`, where Claude Code keeps `dest`'s sessions.

    The new transcript is on disk before the original is removed, and if any step fails
    the earlier ones are undone, so the session is always complete in one place.
    """
    dest = Path(dest).expanduser().resolve()  # like process.cwd(): symlinks resolved
    if not dest.is_dir():
        raise ValueError(f"{dest} is not a folder")
    if s.session_id in live_sessions(home):  # checked now, not from the scan
        raise ValueError(f"session {s.session_id} is still running; exit it first")

    read_stat = s.path.stat()
    lines = s.path.read_bytes().splitlines(keepends=True)
    entries = []
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            e = None
        entries.append(e if isinstance(e, dict) else None)
    cwds = [
        e["cwd"] for e in entries if e and isinstance(e.get("cwd"), str) and e["cwd"]
    ]
    root = _launch_dir(cwds, s.path.parent.name)

    def moved(cwd: str) -> str:
        if Path(cwd).is_relative_to(dest) and Path(cwd).is_dir():
            return (
                cwd  # already in dest, e.g. moving /repo to /repo/sub keeps /repo/sub/x
            )
        if root and Path(cwd).is_relative_to(root) and cwd != root:
            sub = dest / Path(cwd).relative_to(root)
            if sub.is_dir():
                return str(sub)
        return str(dest)

    out = []
    for line, e in zip(lines, entries):
        if (
            e
            and isinstance(e.get("cwd"), str)
            and e["cwd"]
            and (new := moved(e["cwd"])) != e["cwd"]
        ):
            e["cwd"] = new
            line = _encode(e)  # untouched lines stay byte for byte
        out.append(line)

    target_dir = home / "projects" / project_dir_name(str(dest))
    target = target_dir / s.path.name
    in_place = target == s.path
    if in_place and out == lines:
        raise ValueError(f"the session is already in {dest}")
    old_folder, new_folder = s.path.with_suffix(""), target.with_suffix("")
    if not in_place and (target.exists() or new_folder.exists()):
        raise ValueError(f"{target_dir.name} already has a session {s.session_id}")

    target_dir.mkdir(parents=True, exist_ok=True)
    temp = _write_temp(target, out)
    done = []  # steps to undo, newest last
    try:
        # A session resumed elsewhere since the read would lose what it appended.
        now = s.path.stat()
        if (now.st_size, now.st_mtime_ns) != (
            read_stat.st_size,
            read_stat.st_mtime_ns,
        ) or s.session_id in live_sessions(home):
            raise ValueError(
                f"session {s.session_id} changed during the move; nothing was moved"
            )
        os.replace(temp, target)
        if not in_place:
            done.append(lambda: os.unlink(target))
            if old_folder.exists():
                # A rename, never a copy: all-or-nothing, so a failure can't leave a
                # half-moved folder.
                os.rename(old_folder, new_folder)
                done.append(lambda: os.rename(new_folder, old_folder))
            _fsync_dir(
                target_dir
            )  # the new entries are on disk before the original goes
            os.unlink(s.path)  # last: once the original is gone, nothing may be undone
    except BaseException:
        temp.unlink(missing_ok=True)
        for undo in reversed(done):
            undo()
        raise
    if not in_place:
        try:
            _fsync_dir(s.path.parent)
        except OSError:
            pass  # only makes the removal durable; the move itself is complete
    return target
