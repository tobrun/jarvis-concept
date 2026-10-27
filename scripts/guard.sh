#!/bin/sh
# PreToolUse hook entry. Claude Code blocks a call only on exit 2 and lets every other failure through,
# so any outcome but a clean allow from the Python guard becomes exit 2 here.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT/.venv/bin/python" "$ROOT/scripts/guard.py" "$@"
status=$?
if [ "$status" -eq 0 ]; then
    exit 0
fi
exit 2
