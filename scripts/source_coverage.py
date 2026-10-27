"""Per-source read coverage: record what each run read, flag sources that read far less than usual.

Usage:
  source_coverage.py record <source> <available|null> <read>
      append one entry for this run
  source_coverage.py check
      prints JSON {source: {degraded, current, median}}

An entry's run_id is JARVIS_RUN_ID, or "manual" when it is unset.

state/coverage.json:
  {"<source>": [{"run_id": str, "at": ISO timestamp, "available": int | null, "read": int}, ...]}

`check` compares each source's latest entry against the median of up to coverage_history_runs entries
before it. The measure is read/available when the latest entry and every history entry have a positive
`available`; otherwise it is the plain `read` count. A source is degraded when its current measure is
below coverage_degraded_fraction times the median; fewer than coverage_min_history prior entries is
never degraded.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
from pathlib import Path
from typing import Any

import common


def coverage_path(root: Path) -> Path:
    return root / "state" / "coverage.json"


def _has_total(entry: dict[str, Any]) -> bool:
    return isinstance(entry.get("available"), int) and entry["available"] > 0


def assess(entries: list[dict[str, Any]], limits: dict[str, Any]) -> dict[str, Any]:
    """Degradation verdict for one source's entries, oldest first."""
    latest = entries[-1]
    history = entries[:-1][-int(limits["coverage_history_runs"]) :]
    use_ratio = _has_total(latest) and all(_has_total(entry) for entry in history)

    def measure(entry: dict[str, Any]) -> float:
        return entry["read"] / entry["available"] if use_ratio else float(entry["read"])

    current = measure(latest)
    if len(history) < int(limits["coverage_min_history"]):
        return {"degraded": False, "current": current, "median": None}
    median = statistics.median(measure(entry) for entry in history)
    degraded = current < float(limits["coverage_degraded_fraction"]) * median
    return {"degraded": degraded, "current": current, "median": median}


def check(coverage: dict[str, list[dict[str, Any]]], limits: dict[str, Any]) -> dict[str, Any]:
    return {source: assess(entries, limits) for source, entries in sorted(coverage.items()) if entries}


def record(root: Path, source: str, available: int | None, read: int) -> None:
    path = coverage_path(root)
    coverage = common.read_json(path, {})
    entry = {
        "run_id": os.environ.get("JARVIS_RUN_ID") or "manual",
        "at": common.now().isoformat(),
        "available": available,
        "read": read,
    }
    coverage.setdefault(source, []).append(entry)
    common.write_json(path, coverage)


def main(argv: list[str]) -> int:
    root = common.repo_root()
    if argv[:1] == ["record"] and len(argv) == 4:
        try:
            available = None if argv[2] == "null" else int(argv[2])
            read = int(argv[3])
        except ValueError:
            print("available must be an integer or null, read an integer", file=sys.stderr)
            return 2
        record(root, argv[1], available, read)
        return 0
    if argv == ["check"]:
        coverage = common.read_json(coverage_path(root), {})
        print(json.dumps(check(coverage, common.thresholds(root)), indent=2))
        return 0
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
