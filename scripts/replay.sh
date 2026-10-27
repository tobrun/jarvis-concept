#!/bin/sh
# Replay and eval entry: `replay.sh replay <date> <mode>`, `replay.sh eval <YYYY-Www>`, `replay.sh eval --accept`.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/replay.py" "$@"
