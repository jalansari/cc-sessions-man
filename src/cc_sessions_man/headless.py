"""The isolated headless `claude -p` command that classification and AI search both
run.
"""

import json
import os
import signal
import subprocess


def claude_command(
    model: str, system_prompt: str, schema: dict, tools: str = "", *extra: str
) -> list[str]:
    """`claude -p` with nothing of the user's loaded: no hooks, settings, MCP servers or
    session file.

    `tools` names the only tools it can use ("" for none). Run it from an empty folder
    so no project CLAUDE.md loads. In -p mode nobody can approve a permission prompt, so
    file tools are refused outside the working folder and any `--add-dir` (checked with
    Read, Grep and Glob on files in /tmp and in home); dropping the user's settings
    means none can widen that.
    """
    return [
        "claude",
        "-p",
        "--model",
        model,
        # our calls never appear as sessions (or in later searches)
        "--no-session-persistence",
        "--tools",
        tools,
        "--strict-mcp-config",
        "--setting-sources",
        "",  # skip user/project hooks, permissions and other settings
        "--output-format",
        "json",
        "--system-prompt",
        system_prompt,
        "--json-schema",
        json.dumps(schema),
        *extra,
    ]


def stop(proc: subprocess.Popen, sig: int = signal.SIGTERM) -> None:
    """Stop a claude started with start_new_session=True, with the tools it ran (e.g.
    ripgrep).
    """
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError, PermissionError:
        pass  # already gone
