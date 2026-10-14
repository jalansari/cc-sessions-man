"""cc-sessions: list Claude Code sessions and whether each is done or waiting on you."""

import argparse
import math
import json
import os
import shlex
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .classify import DEFAULT_BATCH_SIZE, DEFAULT_JOBS, Cache, classify, error_labels
from .scan import CACHE_DIR, CLAUDE_HOME, DEFAULT_CLAUDE_HOME, MID_TASK, NO_REPLY, RUNNING, Session, home_arg, scan
from .search import DEFAULT_SEARCH_BUDGET, DEFAULT_SEARCH_MODEL, search

CACHE_PATH = CACHE_DIR / "labels.json"

# Status rank: lower sorts first. Gaps leave room for new statuses; Done stays last.
STATUS_RANK = {
    "running": 0,
    "action": 1,
    "decision": 2,
    "interrupted": 3,
    "optional": 4,
    "unclassified": 5,
    "error": 6,
    "done": 99,
}

# Display labels, in rank order.
GROUPS = {
    "running": "Running",
    "action": "Needs action",
    "decision": "Needs decision",
    "interrupted": "Interrupted",
    "optional": "Offered extras",
    "unclassified": "Ended (not classified)",
    "error": "Classification failed",
    "done": "Done",
}
assert list(GROUPS) == sorted(STATUS_RANK, key=STATUS_RANK.get)


def group_of(state: str, label: str | None) -> str:
    if state == RUNNING:
        return "running"
    if state in (NO_REPLY, MID_TASK):
        return "interrupted"
    return label or "unclassified"


def detail_of(s, label_info: dict | None) -> str:
    if s.state == RUNNING:
        return s.live_status
    if s.state == NO_REPLY:
        return "your last prompt got no reply"
    if s.state == MID_TASK:
        return "stopped partway through a tool call"
    return (label_info or {}).get("pending", "")


def short_path(p: str) -> str:
    """Abbreviate the home folder to ~, only for the folder itself or paths inside it."""
    home = str(Path.home())
    if p == home or p.startswith(home + os.sep):
        return "~" + p[len(home):]
    return p


def config_prefix(home: Path) -> str:
    """`CLAUDE_CONFIG_DIR=... ` for shell commands, or "" for the default ~/.claude."""
    if home.resolve() == DEFAULT_CLAUDE_HOME.resolve():
        return ""
    # Absolute: the command cd's into the session's folder first.
    return f"CLAUDE_CONFIG_DIR={shlex.quote(str(home.expanduser().resolve()))} "


def resume_command(home: Path, session_id: str, cwd: str) -> str:
    return f"cd {shlex.quote(cwd)} && {config_prefix(home)}claude --resume {session_id}"


def number_arg(kind, minimum, *, inclusive: bool = True, allow_zero: bool = False):
    """Make an argparse type for a finite number of at least (or above) `minimum`.

    With allow_zero, 0 is also accepted (e.g. "0 turns it off").
    """
    noun = "whole number" if kind is int else "number"
    if inclusive:
        rule = f"{minimum:g} or more"
    else:
        rule = f"more than {minimum:g}"
    if allow_zero:
        rule = f"0 (off) or at least {minimum:g}" if inclusive else f"0 (off) or {rule}"

    def parse(value: str):
        try:
            n = kind(value)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{value!r} is not a {noun}") from None
        ok = math.isfinite(n) and (n >= minimum if inclusive else n > minimum)
        if not (ok or (allow_zero and n == 0)):
            raise argparse.ArgumentTypeError(f"must be {rule}, got {value}")
        return n

    parse.__name__ = f"{noun} ({rule})"  # argparse shows this name in some errors
    return parse


positive_int = number_arg(int, 1)
positive_float = number_arg(float, 0, inclusive=False)
# The UI's auto-refresh: off, or at least a second apart so it can't spin the CPU.
refresh_interval_arg = number_arg(float, 1, allow_zero=True)


def add_common_args(p: argparse.ArgumentParser) -> None:
    """Options shared by cc-sessions and cc-sessions-ui."""
    p.add_argument("--no-llm", action="store_true", help="don't call the model; use cached labels only")
    p.add_argument("--model", default="haiku", help="model alias or ID for classification (default: haiku)")
    p.add_argument("--batch-size", type=positive_int, default=DEFAULT_BATCH_SIZE, help=f"sessions per claude call (default: {DEFAULT_BATCH_SIZE})")
    p.add_argument("-j", "--jobs", type=positive_int, default=DEFAULT_JOBS, help=f"parallel claude calls (default: {DEFAULT_JOBS})")
    p.add_argument(
        "--claude-home", type=home_arg, default=CLAUDE_HOME,
        help=f"Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude; now {short_path(str(CLAUDE_HOME))})",
    )


def add_search_args(p: argparse.ArgumentParser) -> None:
    """AI search options, shared by cc-sessions and cc-sessions-ui."""
    p.add_argument(
        "--search-model", default=DEFAULT_SEARCH_MODEL,
        help=f"model alias or ID for AI search (default: {DEFAULT_SEARCH_MODEL})",
    )
    p.add_argument(
        "--search-budget", type=positive_float, default=DEFAULT_SEARCH_BUDGET, metavar="USD",
        help=f"stop an AI search that costs more than USD (default: {DEFAULT_SEARCH_BUDGET:g}). "
        "Checked between steps, so a search can go over by one step",
    )


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="cc-sessions", description=__doc__)
    p.add_argument("-n", "--limit", type=positive_int, help="only the N most recent sessions")
    p.add_argument("--days", type=positive_float, help="only sessions active in the last N days")
    p.add_argument(
        "-s", "--show", action="append", choices=list(GROUPS),
        help="only these groups (repeatable). Default: everything",
    )
    p.add_argument("--pending", action="store_true", help="shorthand for --show decision --show action --show interrupted")
    p.add_argument("--refresh", action="store_true", help="ignore cached labels for the listed sessions and re-classify them")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.add_argument(
        "--search", metavar="QUERY",
        help="AI search: ask claude to look through the transcripts for sessions matching QUERY, "
        "and list them most relevant first. With -n or --days, only those sessions can match",
    )
    add_common_args(p)
    add_search_args(p)
    return p.parse_args(argv)


def print_search(args, sessions: list[Session]) -> int:
    print(f"Searching {len(sessions)} sessions with {args.search_model}…", file=sys.stderr)
    try:
        result = search(sessions, args.search, args.search_model, args.claude_home, args.search_budget)
    except (RuntimeError, ValueError) as exc:
        print(f"AI search failed: {exc}", file=sys.stderr)
        return 1
    by_id = {s.session_id: s for s in sessions}
    found = [(by_id[m.session_id], m.reason) for m in result.matches]
    if args.json:
        print(json.dumps([
            {
                "session_id": s.session_id, "title": s.title, "cwd": s.cwd, "last_activity": s.last_timestamp,
                "reason": reason, "resume": resume_command(args.claude_home, s.session_id, s.cwd),
            }
            for s, reason in found
        ], indent=2))
    else:
        for s, reason in found:
            print(f"{s.last_timestamp[:10]}  {s.session_id}  {short_path(s.cwd)}")
            print(f"    {s.title}")
            print(f"    {reason}")
        print(f"\n{len(found)} found  (${result.cost_usd:.2f})", file=sys.stderr)
    if result.unknown:
        print(f"Ignored {len(result.unknown)} session IDs outside the sessions searched.", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    args = parse_args(argv)
    sessions = scan(args.claude_home)
    if args.days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
        sessions = [s for s in sessions if s.last_timestamp >= cutoff[:19]]
    if args.limit is not None:
        sessions = sessions[: args.limit]
    if args.search is not None:
        return print_search(args, sessions)

    cache = Cache(CACHE_PATH)
    labels, todo = cache.partition(sessions)
    if args.refresh:
        # Send every listed finished session again, but keep the old labels until new ones
        # arrive: classify() overwrites on success, and a failed batch must not lose them.
        # Sessions outside -n / --days aren't listed, so they're never touched.
        todo = [s for s in sessions if s.session_id in labels] + todo
    if not args.no_llm:
        def progress(msg):
            print(msg, file=sys.stderr)

        failed: dict[str, dict] = {}

        def on_batch(new, batch, err):
            if err:
                failed.update(error_labels(batch, err, skip=new))

        labels.update(classify(todo, cache, args.model, args.batch_size, args.jobs, progress, on_batch))
        labels.update(failed)  # shown as "Classification failed"; not cached, so retried next run

    show = set(args.show or [])
    if args.pending:
        show |= {"decision", "action", "interrupted"}

    rows = []
    for s in sessions:
        info = labels.get(s.session_id)
        g = group_of(s.state, info and info["label"])
        if show and g not in show:
            continue
        rows.append((g, s, detail_of(s, info)))

    if args.json:
        print(json.dumps([
            {
                "group": g, "status_rank": STATUS_RANK[g], "session_id": s.session_id, "title": s.title, "cwd": s.cwd,
                "last_activity": s.last_timestamp, "detail": d, "context_tokens": s.context_tokens,
                "model": s.model,
                "resume": resume_command(args.claude_home, s.session_id, s.cwd),
            }
            for g, s, d in rows
        ], indent=2))
        return 0

    for g, label in GROUPS.items():
        members = [(s, d) for gg, s, d in rows if gg == g]
        if not members:
            continue
        print(f"\n== {label} ({len(members)}) ==")
        for s, d in members:
            print(f"{s.last_timestamp[:16].replace('T', ' ')}  {s.session_id}  {short_path(s.cwd)}")
            print(f"    {s.title}")
            if d:
                print(f"    → {d}")
    counts = Counter(g for g, _, _ in rows)
    print("\n" + ", ".join(f"{GROUPS[g]}: {counts[g]}" for g in GROUPS if counts[g]))
    print(f"Resume one with: cd <dir> && {config_prefix(args.claude_home)}claude --resume <session-id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
