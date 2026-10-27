#!/bin/sh
# One-time setup, run by hand from a shell in a fresh clone: dependencies, the eval gate hooks, the generated
# config (a skeleton until the bootstrap interview writes a profile), the index database when the profile
# picks Postgres, and the guard pin. It installs no scheduled job and starts no database server.
set -eu
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
uv sync --all-extras
git config core.hooksPath scripts/hooks
if [ ! -f config/jarvis.yaml ]; then
    .venv/bin/python scripts/bootstrap.py render
fi
if grep -q '^index: {backend: postgres}' config/jarvis.yaml; then
    sh scripts/db_setup.sh
fi
if [ -f state/guard.sha256 ]; then
    .venv/bin/python scripts/guard_pin.py verify
else
    .venv/bin/python scripts/guard_pin.py pin
fi
.venv/bin/python scripts/guard_pin.py self-test
if grep -q '^bootstrapped: true' config/jarvis.yaml; then
    echo "setup: done. Check the install with: uv run scripts/doctor.py"
else
    echo "setup: done. Next: scripts/jarvis bootstrap"
fi
