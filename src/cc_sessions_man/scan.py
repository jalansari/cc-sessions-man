"""Read Claude Code's on-disk state: live session files and saved transcripts."""

import hashlib
import json
import os
import tempfile
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path

from .accounts import (
    DEFAULT_CLAUDE_HOME,
    ConfigDir,
    config_dir,
    discover,
    owner_of,
    project_roots,
    with_discovered,
)


def claude_home_from_env() -> Path:
    """Claude Code's config dir: $CLAUDE_CONFIG_DIR if set and non-empty, else
    ~/.claude.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    # Absolute, because resuming changes into the session's folder before running
    # claude.
    return Path(env).expanduser().resolve() if env else DEFAULT_CLAUDE_HOME


def claude_env(home: Path) -> dict[str, str]:
    """Environment for running `claude` against `home`, so it sees the same sessions."""
    env = dict(os.environ)
    if home.expanduser().resolve() == DEFAULT_CLAUDE_HOME.resolve():
        env.pop("CLAUDE_CONFIG_DIR", None)
    else:
        env["CLAUDE_CONFIG_DIR"] = str(home.expanduser().resolve())
    return env


def home_arg(value: str) -> Path:
    """argparse type for --claude-home."""
    return Path(value).expanduser().resolve()


CLAUDE_HOME = claude_home_from_env()
CACHE_DIR = (
    Path(os.environ.get("XDG_CACHE_HOME", "").strip() or Path.home() / ".cache")
    / "cc-sessions-man"
)
SCAN_CACHE_VERSION = 4  # bump when Session fields or parsing change

# One config folder (an explicit --claude-home), or several: paths or ConfigDirs.
Homes = str | Path | Iterable[str | Path | ConfigDir]


def config_dirs(homes: Homes | None) -> list[ConfigDir]:
    """The config folders to read: `homes`, or every one found when it's None."""
    if homes is None:
        return discover()
    if isinstance(homes, (str, Path)):
        return [config_dir(homes)]
    return [h if isinstance(h, ConfigDir) else config_dir(h) for h in homes]


def session_home(s) -> Path:
    """The config folder to resume a session with: the one it belongs to."""
    return Path(s.config_dir) if getattr(s, "config_dir", "") else CLAUDE_HOME


def home_paths(homes: Homes) -> list[Path]:
    if isinstance(homes, (str, Path)):
        homes = [homes]
    return [
        h.path if isinstance(h, ConfigDir) else Path(h).expanduser().resolve()
        for h in homes
    ]


# Structural states, decided without a model.
RUNNING = "running"
NO_REPLY = "interrupted-no-reply"  # ended on the user's prompt
MID_TASK = "interrupted-mid-task"  # ended on a tool call or tool result
ENDED = "ended"  # ended on an assistant text reply; needs classifying


@dataclass
class Session:
    session_id: str
    path: Path
    mtime: float
    cwd: str = ""
    title: str = ""
    started: str = ""  # ISO timestamp of the first message
    last_timestamp: str = ""
    state: str = ENDED
    live_status: str = ""  # busy / idle, from the live session file
    last_user_prompt: str = ""
    last_assistant_text: str = ""
    context_tokens: int = (
        0  # context size at Claude's last reply (input + cached input tokens)
    )
    model: str = ""  # model ID of Claude's last reply, e.g. claude-opus-5-5
    # From the last session-owner entry; older sessions have none.
    account_uuid: str = ""
    config_dir: str = ""  # the config folder it belongs to; set by scan, never cached


def _proc_start(pid: int) -> str | None:
    """When a process started, in clock ticks since boot (field 22 of /proc/<pid>/stat).

    None when /proc isn't available (not Linux) or the process has gone.
    """
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name (field 2) is in parentheses and may contain spaces.
    fields = stat[stat.rindex(")") + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def live_sessions(homes: Homes = CLAUDE_HOME) -> dict[str, dict]:
    """Sessions with a running process in any of the config folders, keyed by session
    ID. Each entry's "configDir" is the folder it's running under.
    """
    live = {}
    for home in home_paths(homes):
        for sid, d in _live_in(home).items():
            live[sid] = {**d, "configDir": str(home)}
    return live


def _live_in(home: Path) -> dict[str, dict]:
    live = {}
    for f in (home / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
        except OSError, ValueError:
            continue
        if not isinstance(d, dict) or not isinstance(d.get("sessionId"), str):
            continue
        pid = d.get("pid")
        if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
            continue
        try:
            os.kill(pid, 0)
        except PermissionError:
            # exists, but owned by another user (shared config dir, sudo): still running
            pass
        except OSError:
            continue  # no such process
        # Claude records its process start time. If the PID now belongs to a process
        # that started at a different time, Claude died and the OS reused the PID.
        recorded = d.get("procStart")
        if (
            recorded is not None
            and (actual := _proc_start(pid)) is not None
            and str(recorded) != actual
        ):
            continue
        live[d["sessionId"]] = d
    return live


def _str(value) -> str:
    """A transcript field as a string; anything else (null, numbers, objects) becomes
    "".
    """
    return value if isinstance(value, str) else ""


def _context_tokens(message) -> int:
    """Tokens in the context a reply was generated from: fresh, cache-written and
    cache-read input.
    """
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return 0
    fields = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    return sum(
        v
        for f in fields
        if isinstance(v := usage.get(f), int) and not isinstance(v, bool)
    )


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            _str(b.get("text"))
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _is_tool_result(content) -> bool:
    return (
        isinstance(content, list)
        and bool(content)
        and all(isinstance(b, dict) and b.get("type") == "tool_result" for b in content)
    )


def _is_local_command(text: str) -> bool:
    # Slash-command echoes and their output, not real prompts.
    return text.lstrip().startswith(
        ("<command-name>", "<local-command", "<command-message>")
    )


def parse_transcript(path: Path) -> Session | None:
    """Parse one transcript. Returns None if it holds no conversation."""
    s = Session(session_id=path.stem, path=path, mtime=path.stat().st_mtime)
    last_kind = None
    last_prompt_fallback = ""
    with path.open(errors="replace") as fh:
        for line in fh:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if not isinstance(
                e, dict
            ):  # valid JSON but not an entry, e.g. a truncated write
                continue
            t = e.get("type")
            if t == "session-owner":
                # Written each time the session starts or resumes: the last one is
                # the account that used it last.
                s.account_uuid = _str(e.get("accountUuid")) or s.account_uuid
            elif t == "custom-title":
                s.title = _str(e.get("customTitle")) or s.title
            elif t == "ai-title" and not s.title:
                s.title = _str(e.get("aiTitle"))
            elif t == "summary" and not s.title:
                s.title = _str(e.get("summary"))
            elif t == "last-prompt":
                last_prompt_fallback = _str(e.get("lastPrompt")) or last_prompt_fallback
            if (
                t not in ("user", "assistant")
                or e.get("isSidechain")
                or e.get("isMeta")
            ):
                continue
            s.cwd = _str(e.get("cwd")) or s.cwd
            message = e.get("message")
            content = message.get("content") if isinstance(message, dict) else None
            if t == "assistant" and (tokens := _context_tokens(message)):
                s.context_tokens = (
                    tokens  # the latest reply's context is the session's current size
                )
            # Claude Code writes "<synthetic>" on placeholder replies it makes itself
            # (errors, interrupts).
            model = _str(message.get("model")) if isinstance(message, dict) else ""
            if t == "assistant" and model and model != "<synthetic>":
                s.model = model  # the latest, since /model can switch partway
            if t == "user":
                if _is_tool_result(content):
                    last_kind = "tool_result"
                else:
                    text = _text(content)
                    if not text.strip() or _is_local_command(text):
                        continue
                    last_kind = "user"
                    s.last_user_prompt = text
            else:
                blocks = content if isinstance(content, list) else []
                if any(
                    isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks
                ):
                    last_kind = "tool_use"
                else:
                    text = _text(content)
                    if not text.strip():
                        continue  # thinking-only chunk
                    last_kind = "assistant"
                    s.last_assistant_text = text
            s.last_timestamp = _str(e.get("timestamp")) or s.last_timestamp
            s.started = s.started or s.last_timestamp
    if last_kind is None:
        return None
    if not s.title:
        prompt = (s.last_user_prompt or last_prompt_fallback).strip()
        s.title = prompt.splitlines()[0][:70] if prompt else ""
    s.state = {"user": NO_REPLY, "assistant": ENDED}.get(last_kind, MID_TASK)
    return s


def _to_cache(s: Session | None) -> dict | None:
    if s is None:
        return None
    d = asdict(s)
    d["path"] = str(s.path)
    # Keep the cache small; classify only looks at the start of the prompt and end of
    # the reply.
    d["last_user_prompt"] = s.last_user_prompt[:2000]
    d["last_assistant_text"] = s.last_assistant_text[-4000:]
    return d


def _from_cache(d: dict | None) -> Session | None:
    if d is None:
        return None
    return Session(**{**d, "path": Path(d["path"])})


def _load_scan_cache(path: Path) -> dict:
    """Cached entries by transcript path; {} if the file is missing, stale or
    malformed.
    """
    try:
        data = json.loads(path.read_text())
    except OSError, ValueError:
        return {}
    if not isinstance(data, dict) or data.get("version") != SCAN_CACHE_VERSION:
        return {}
    entries = data.get("entries")
    if not isinstance(entries, dict):
        return {}
    # Drop malformed entries so those transcripts are simply parsed again.
    return {
        k: v
        for k, v in entries.items()
        if isinstance(v, dict)
        and {"mtime", "size", "session"} <= v.keys()
        and (v["session"] is None or isinstance(v["session"], dict))
    }


def _save_scan_cache(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # A unique temp file per save: the UI and the CLI may save the same home's cache at
    # once, and a shared temp name would let them interleave or replace() each other's.
    with tempfile.NamedTemporaryFile(
        "w", dir=path.parent, prefix=path.name, suffix=".tmp", delete=False
    ) as fh:
        fh.write(json.dumps({"version": SCAN_CACHE_VERSION, "entries": entries}))
    Path(fh.name).replace(path)


_DEFAULT = object()


def scan_cache_path(homes: Homes | None) -> Path:
    """One scan cache per set of projects folders, so switching --claude-home doesn't
    evict the other.
    """
    roots = sorted(str(root) for root, _ in project_roots(config_dirs(homes)))
    digest = hashlib.sha256("\n".join(roots).encode()).hexdigest()[:12]
    return (
        CACHE_DIR / f"scan-{digest}.json"
    )  # CACHE_DIR looked up now, so tests can redirect it


def scan(homes: Homes | None = None, cache_path=_DEFAULT) -> list[Session]:
    """All main-thread sessions, newest first. Subagent transcripts are skipped.

    `homes` is one config folder, several, or None for every one found. Each real
    projects folder is read once, so folders sharing one through a symlink don't list
    its sessions twice. Each session's config_dir is the folder it's running under, or
    else the one logged in with the account in its transcript, or else the folder
    owning its projects folder.

    Parsed transcripts are cached by path (default: one file per set of projects
    folders), and only re-read when their size or mtime changes. Pass cache_path=None
    to parse everything.
    """
    dirs = config_dirs(homes)
    if cache_path is _DEFAULT:
        cache_path = scan_cache_path(dirs)
    # Running sessions and accounts come from every folder found, even when only one
    # is read: a session in its projects may be open under another account.
    known = with_discovered(dirs)
    live = live_sessions(known)
    old = _load_scan_cache(cache_path) if cache_path else {}
    entries = {}
    changed = False
    sessions: dict[str, Session] = {}
    for root, owner in project_roots(dirs):
        for path in root.glob("*/*.jsonl"):
            if (s := _scan_one(path, old, entries)) is _GONE:
                changed = True
                continue
            if entries[str(path)] is not old.get(str(path)):
                changed = True
            if s is None:
                continue
            s.config_dir = str(owner_of(s.account_uuid, owner, known))
            if s.session_id in live:  # live state is never cached
                s.state = RUNNING
                # Only take string values: a null or odd field mustn't replace the
                # transcript's cwd.
                s.live_status = _str(live[s.session_id].get("status"))
                s.cwd = _str(live[s.session_id].get("cwd")) or s.cwd
                s.config_dir = live[s.session_id]["configDir"]
            # A copied config folder (a backup, say) holds the same sessions again:
            # list each once, from the most recently written transcript.
            if (seen := sessions.get(s.session_id)) is None or s.mtime > seen.mtime:
                sessions[s.session_id] = s
    if cache_path and (changed or entries.keys() != old.keys()):
        _save_scan_cache(cache_path, entries)
    return sorted(sessions.values(), key=lambda s: s.last_timestamp, reverse=True)


_GONE = object()


def _scan_one(path: Path, old: dict, entries: dict):
    """One transcript, from the cache if it's unchanged. Records its cache entry in
    `entries`; returns the Session, None if it holds nothing to show, or _GONE if it
    was deleted mid-scan.
    """
    key = str(path)
    try:
        st = path.stat()
        hit = old.get(key)
        if hit and hit["mtime"] == st.st_mtime and hit["size"] == st.st_size:
            s = _from_cache(hit["session"])
        else:
            try:
                s = parse_transcript(path)
            except FileNotFoundError:
                raise
            except Exception:
                # A transcript in a shape we don't understand: skip it rather than
                # let one file hide every session. Remembered as "nothing to show"
                # until the file changes, so it isn't re-read (and the cache
                # rewritten) every scan.
                s = None
            hit = {"mtime": st.st_mtime, "size": st.st_size, "session": _to_cache(s)}
    except FileNotFoundError:
        # Deleted between glob() and reading it (another viewer, or Claude Code
        # cleanup).
        return _GONE
    entries[key] = hit
    return s
