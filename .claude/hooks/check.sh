#!/usr/bin/env bash
# PostToolUse hook: after Claude edits a Python file, format it, lint it and run the
# tests. A failure exits 2, which shows the output to Claude so it can fix it.
set -uo pipefail

file=$(python3 -I -c 'import json, sys; print(json.load(sys.stdin).get("tool_input", {}).get("file_path", ""))')
[[ "$file" == *.py ]] || exit 0
[[ -f "$file" ]] || exit 0

cd "${CLAUDE_PROJECT_DIR:-.}" || exit 0

uv run --quiet black --quiet "$file"
uv run --quiet isort --quiet "$file"

if ! out=$(uv run --quiet flake8 "$file" 2>&1); then
    echo "flake8 found problems in $file:" >&2
    echo "$out" >&2
    exit 2
fi

if ! out=$(uv run --quiet pytest -q -x 2>&1); then
    echo "Tests failed after editing $file:" >&2
    echo "$out" | tail -n 40 >&2
    exit 2
fi
