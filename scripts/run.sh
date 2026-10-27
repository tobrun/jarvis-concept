#!/bin/sh
# Scheduler entry for a scheduled run: `run.sh <mode>`. launchd, systemd, and cron have no shell profile or
# aliases, so everything is absolute; run.py takes claude_bin and claude_config_dir from config/jarvis.yaml.
# JARVIS_NOW is a replay and test clock only; a scheduled run always uses the real time.
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
unset JARVIS_NOW JARVIS_MODE JARVIS_RUN_ID JARVIS_UNSENT
JARVIS_ROOT="$ROOT"
export JARVIS_ROOT
exec "$ROOT/.venv/bin/python" "$ROOT/scripts/run.py" "$@"
