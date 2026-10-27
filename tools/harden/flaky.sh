#!/bin/sh
# Flakiness check: run the given pytest targets 5 times, each in a fresh random order; every run must pass.
# Usage: tools/harden/flaky.sh [pytest args...]
set -u
fails=0
for seed in 11 23 37 41 53; do
    if ! uv run --with pytest-randomly==3.16.0 pytest -q -p randomly -p no:cacheprovider -p "randomly" \
        --randomly-seed="$seed" "$@" >/dev/null 2>&1; then
        echo "flaky: run with --randomly-seed=$seed failed"
        fails=$((fails + 1))
    fi
done
[ "$fails" -eq 0 ] && echo "flaky: 5 of 5 shuffled runs passed"
exit "$fails"
