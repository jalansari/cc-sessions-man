"""Read Claude Code's on-disk state: live session files and saved transcripts."""

import hashlib
import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

DEFAULT_CLAUDE_HOME = Path.home() / ".claude"


def claude_home_from_env() -> Path:
    """Claude Code's config dir: $CLAUDE_CONFIG_DIR if set and non-empty, else ~/.claude."""
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    # Absolute, because resuming changes into the session's folder before running claude.
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
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", "").strip() or Path.home() / ".cache") / "cc-sessions-man"
SCAN_CACHE_VERSION = 1  # bump when Session fields or parsing change

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


def live_sessions(home: Path = CLAUDE_HOME) -> dict[str, dict]:
    """Sessions with a running process, keyed by session ID."""
    live = {}
    for f in (home / "sessions").glob("*.json"):
        try:
            d = json.loads(f.read_text())
            os.kill(d["pid"], 0)
        except (OSError, ValueError, KeyError):
            continue
        live[d["sessionId"]] = d
    return live


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
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
    return text.lstrip().startswith(("<command-name>", "<local-command", "<command-message>"))


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
            t = e.get("type")
            if t == "custom-title":
                s.title = e.get("customTitle") or s.title
            elif t == "ai-title" and not s.title:
                s.title = e.get("aiTitle") or ""
            elif t == "summary" and not s.title:
                s.title = e.get("summary") or ""
            elif t == "last-prompt":
                last_prompt_fallback = e.get("lastPrompt") or last_prompt_fallback
            if t not in ("user", "assistant") or e.get("isSidechain") or e.get("isMeta"):
                continue
            s.cwd = e.get("cwd") or s.cwd
            content = e.get("message", {}).get("content")
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
                if any(isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks):
                    last_kind = "tool_use"
                else:
                    text = _text(content)
                    if not text.strip():
                        continue  # thinking-only chunk
                    last_kind = "assistant"
                    s.last_assistant_text = text
            s.last_timestamp = e.get("timestamp") or s.last_timestamp
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
    # Keep the cache small; classify only looks at the start of the prompt and end of the reply.
    d["last_user_prompt"] = s.last_user_prompt[:2000]
    d["last_assistant_text"] = s.last_assistant_text[-4000:]
    return d


def _from_cache(d: dict | None) -> Session | None:
    if d is None:
        return None
    return Session(**{**d, "path": Path(d["path"])})


def _load_scan_cache(path: Path) -> dict:
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {}
    return data.get("entries", {}) if data.get("version") == SCAN_CACHE_VERSION else {}


def _save_scan_cache(path: Path, entries: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": SCAN_CACHE_VERSION, "entries": entries}))
    tmp.replace(path)


_DEFAULT = object()


def scan_cache_path(home: Path) -> Path:
    """One scan cache per Claude home, so switching --claude-home doesn't evict the other."""
    digest = hashlib.sha256(str(home.expanduser().resolve()).encode()).hexdigest()[:12]
    return CACHE_DIR / f"scan-{digest}.json"  # CACHE_DIR looked up now, so tests can redirect it


def scan(home: Path = CLAUDE_HOME, cache_path=_DEFAULT) -> list[Session]:
    """All main-thread sessions, newest first. Subagent transcripts are skipped.

    Parsed transcripts are cached by path (default: one file per home), and only re-read
    when their size or mtime changes. Pass cache_path=None to parse everything.
    """
    if cache_path is _DEFAULT:
        cache_path = scan_cache_path(home)
    live = live_sessions(home)
    old = _load_scan_cache(cache_path) if cache_path else {}
    entries = {}
    changed = False
    sessions = []
    for path in (home / "projects").glob("*/*.jsonl"):
        key = str(path)
        try:
            st = path.stat()
            hit = old.get(key)
            if hit and hit["mtime"] == st.st_mtime and hit["size"] == st.st_size:
                s = _from_cache(hit["session"])
            else:
                s = parse_transcript(path)
                hit = {"mtime": st.st_mtime, "size": st.st_size, "session": _to_cache(s)}
                changed = True
        except FileNotFoundError:
            # Deleted between glob() and reading it (another viewer, or Claude Code cleanup).
            changed = True
            continue
        entries[key] = hit
        if s is None:
            continue
        if s.session_id in live:  # live state is never cached
            s.state = RUNNING
            s.live_status = live[s.session_id].get("status", "")
            s.cwd = live[s.session_id].get("cwd", s.cwd)
        sessions.append(s)
    if cache_path and (changed or entries.keys() != old.keys()):
        _save_scan_cache(cache_path, entries)
    sessions.sort(key=lambda s: s.last_timestamp, reverse=True)
    return sessions
