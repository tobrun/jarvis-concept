#!/bin/sh
# PostToolUse hook entry: run capture.py with the repo's venv Python. Capture can never block a tool call.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
"$ROOT/.venv/bin/python" "$ROOT/scripts/capture.py" || true
exit 0
