"""cc-sessions: list Claude Code sessions and whether each is done or waiting on you."""

import argparse
import json
import os
import shlex
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .classify import DEFAULT_BATCH_SIZE, DEFAULT_JOBS, LABELS, Cache, classify, error_labels
from .scan import CACHE_DIR, CLAUDE_HOME, DEFAULT_CLAUDE_HOME, ENDED, MID_TASK, NO_REPLY, RUNNING, home_arg, scan

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
    "action": "Needs your action",
    "decision": "Needs your decision",
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
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


def config_prefix(home: Path) -> str:
    """`CLAUDE_CONFIG_DIR=... ` for shell commands, or "" for the default ~/.claude."""
    if home.resolve() == DEFAULT_CLAUDE_HOME.resolve():
        return ""
    # Absolute: the command cd's into the session's folder first.
    return f"CLAUDE_CONFIG_DIR={shlex.quote(str(home.expanduser().resolve()))} "


def resume_command(home: Path, session_id: str, cwd: str) -> str:
    return f"cd {shlex.quote(cwd)} && {config_prefix(home)}claude --resume {session_id}"


def parse_args(argv=None):
    p = argparse.ArgumentParser(prog="cc-sessions", description=__doc__)
    p.add_argument("-n", "--limit", type=int, help="only the N most recent sessions")
    p.add_argument("--days", type=float, help="only sessions active in the last N days")
    p.add_argument(
        "-s", "--show", action="append", choices=list(GROUPS),
        help="only these groups (repeatable). Default: everything",
    )
    p.add_argument("--pending", action="store_true", help="shorthand for --show decision --show action --show interrupted")
    p.add_argument("--no-llm", action="store_true", help="skip the model; use cached labels only")
    p.add_argument("--refresh", action="store_true", help="ignore cached labels for the listed sessions and re-classify them")
    p.add_argument("--model", default="haiku", help="model alias or ID for classification (default: haiku)")
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE, help=f"sessions per claude call (default: {DEFAULT_BATCH_SIZE})")
    p.add_argument("-j", "--jobs", type=int, default=DEFAULT_JOBS, help=f"parallel claude calls (default: {DEFAULT_JOBS})")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.add_argument(
        "--claude-home", type=home_arg, default=CLAUDE_HOME,
        help=f"Claude config dir (default: $CLAUDE_CONFIG_DIR or ~/.claude; now {short_path(str(CLAUDE_HOME))})",
    )
    return p.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    sessions = scan(args.claude_home)
    if args.days is not None:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=args.days)).isoformat()
        sessions = [s for s in sessions if s.last_timestamp >= cutoff[:19]]
    if args.limit:
        sessions = sessions[: args.limit]

    cache = Cache(CACHE_PATH)
    if args.refresh:
        # Only forget the sessions this run will classify again; filters like -n or
        # --days must not throw away everyone else's labels.
        for s in sessions:
            cache.drop(s.session_id)
    if args.no_llm:
        labels = {s.session_id: h for s in sessions if s.state == ENDED and (h := cache.get(s))}
    else:
        def progress(msg):
            print(msg, file=sys.stderr)

        failed: dict[str, dict] = {}

        def on_batch(new, batch, err):
            if err:
                failed.update(error_labels(batch, err, skip=new))

        labels = classify(sessions, cache, args.model, args.batch_size, args.jobs, progress, on_batch)
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
                "last_activity": s.last_timestamp, "detail": d,
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
