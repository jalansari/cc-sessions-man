"""AI search: a headless `claude -p` call that greps the transcripts to answer a
query.
"""

import json
import signal
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .headless import claude_command, stop
from .scan import CLAUDE_HOME, Session

DEFAULT_SEARCH_MODEL = "sonnet"
# A cap on one search's cost. claude checks it between turns, so a search can overshoot
# it by one turn: the first turn alone (the session index) costs about $0.12.
DEFAULT_SEARCH_BUDGET = 1.0
SEARCH_TIMEOUT = 300
MAX_MATCHES = 20

SCHEMA = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "session_id": {"type": "string"},
                    "reason": {"type": "string"},
                },
                "required": ["session_id", "reason"],
            },
        },
    },
    "required": ["matches"],
}

SYSTEM_PROMPT = """You search Claude Code session transcripts to find the sessions \
that match a user's query.

The folder {projects} holds one folder per project. Each \
`<project>/<session-id>.jsonl` file there is one session's transcript: JSON lines with \
`type` (user, assistant, ...), `timestamp`, `cwd` and `message.content`. Slash \
commands the user ran appear as `<command-name>/name</command-name>` in user entries. \
Ignore the `<session-id>/` subfolders (subagent transcripts and tool output).

Search with Grep over `*/*.jsonl` in that folder first, and read only small parts of \
files when you need to. Transcripts are large: never read a whole file.

Judge relevance from what the matching lines say (use Grep's content output with a \
little context), not from how often words appear: for a topic, prefer sessions where \
it was the main subject, designed or decided, over passing mentions.
Return the matching sessions' IDs (the file name without .jsonl), most relevant first, \
each with a one-sentence reason saying what the session did about the query (never a \
count of matches), and its date when the query is about time.
If the query asks for the first, last, earliest or latest, compare timestamps and \
return just that session.
Only sessions in the index you're given count: ignore files whose ID isn't in it (it \
may cover only some sessions).
Return at most {max_matches}, and an empty list if nothing matches. Only return IDs of \
files you found; never invent one."""


@dataclass
class Match:
    session_id: str
    reason: str


@dataclass
class SearchResult:
    matches: list[Match]
    # IDs the model returned that weren't searched
    unknown: list[str] = field(default_factory=list)
    cost_usd: float = 0.0


class SearchStopped(RuntimeError):
    """claude was killed by a signal: stopped by us, or by something else (e.g. out of
    memory).
    """


def build_prompt(sessions: list[Session], query: str) -> str:
    """The query plus an index of the sessions, so the model can use titles, folders and
    dates.
    """
    index = "\n".join(
        f"{s.session_id} | {s.started[:16]} | {s.last_timestamp[:16]} | {s.cwd} | "
        f"{' '.join(s.title.split())[:80]}"
        for s in sessions
    )
    return (
        f"Query: {query}\n\nIndex of sessions "
        f"(id | started | last activity | folder | title):\n{index}"
    )


def call_claude(
    prompt: str,
    model: str,
    projects: Path,
    budget: float,
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> dict:
    """One headless search call with read-only tools over `projects`. Returns claude's
    JSON result.

    `on_start` gets the process as soon as it starts, so the caller can terminate it.
    """
    cmd = claude_command(
        model,
        SYSTEM_PROMPT.format(projects=projects, max_matches=MAX_MATCHES),
        SCHEMA,
        "Read,Grep,Glob",  # read-only: it can look, never change anything
        "--add-dir",
        str(projects),  # the only folder those tools can reach
        "--max-budget-usd",
        str(budget),
    )
    # Run from an empty dir: no project CLAUDE.md, and nothing else for the tools to
    # reach. Its own process group, so stopping it also stops the ripgrep its Grep runs.
    with tempfile.TemporaryDirectory() as cwd:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=cwd,
            start_new_session=True,
        )
        if on_start:
            on_start(proc)
        try:
            stdout, stderr = proc.communicate(prompt, timeout=SEARCH_TIMEOUT)
        except subprocess.TimeoutExpired:
            stop(proc, signal.SIGKILL)
            proc.communicate()
            raise RuntimeError(
                f"The search took longer than {SEARCH_TIMEOUT // 60} minutes "
                "and was stopped."
            )
    if proc.returncode < 0:
        raise SearchStopped(
            f"claude was stopped (signal {-proc.returncode}) before it finished."
        )
    try:
        out = json.loads(stdout)
    except ValueError:
        raise RuntimeError(
            stderr.strip()
            or stdout.strip()
            or f"claude exited with code {proc.returncode}"
        )
    if out.get("subtype") == "error_max_budget_usd":
        raise RuntimeError(
            f"The search reached its ${budget:g} budget "
            f"(spent ${out.get('total_cost_usd', 0):.2f}) before "
            "finishing. Try a more specific query, or raise the budget "
            "with --search-budget."
        )
    if out.get("is_error") or proc.returncode != 0:
        raise RuntimeError(
            out.get("result")
            or stderr.strip()
            or f"claude stopped early ({out.get('subtype')})"
        )
    return out


def _run_search(
    prompt: str,
    model: str,
    projects: Path,
    budget: float,
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> tuple[list[dict], float]:
    """The model's matches and the call's cost. Tests replace this."""
    out = call_claude(prompt, model, projects, budget, on_start)
    data = out.get("structured_output")
    if not isinstance(data, dict) or not isinstance(data.get("matches"), list):
        raise RuntimeError("claude didn't return any search results")
    return data["matches"], float(out.get("total_cost_usd") or 0)


def search(
    sessions: list[Session],
    query: str,
    model: str = DEFAULT_SEARCH_MODEL,
    home: Path = CLAUDE_HOME,
    budget: float = DEFAULT_SEARCH_BUDGET,
    on_start: Callable[[subprocess.Popen], None] | None = None,
) -> SearchResult:
    """Ask claude which of `sessions` match `query`, most relevant first.

    IDs that aren't among `sessions` are dropped (and listed in `unknown`), as are
    repeats.
    """
    query = " ".join(query.split())
    if not query:
        raise ValueError("the search is empty")
    raw, cost = _run_search(
        build_prompt(sessions, query), model, home / "projects", budget, on_start
    )
    known = {s.session_id for s in sessions}
    result = SearchResult([], cost_usd=cost)
    seen = set()
    for item in raw:
        if not isinstance(item, dict) or not isinstance(
            sid := item.get("session_id"), str
        ):
            continue
        sid = sid.strip().removesuffix(".jsonl")
        if sid not in known:
            result.unknown.append(sid)
        elif sid not in seen:
            seen.add(sid)
            reason = item.get("reason")
            result.matches.append(
                Match(sid, " ".join(reason.split()) if isinstance(reason, str) else "")
            )
    return result
