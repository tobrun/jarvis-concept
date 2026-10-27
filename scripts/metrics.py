"""Weekly outcome metrics: the table JARVIS.md section 13 reads to judge whether Jarvis is getting better.

Usage:
    uv run scripts/metrics.py week <YYYY-Www>

Reads state/outcomes.jsonl, state/denied_calls.jsonl (minus the run.py canary's own denials),
state/coverage.json, state/claim_checks.jsonl, state/prep_checks.jsonl, and replay candidate pairs
(eval/replays/<name>.candidates.jsonl against state/candidates/<name>.jsonl).
Writes state/metrics/<YYYY-Www>.json and prints it.
A record belongs to week W when its `at` falls in ISO week W in the owner's timezone.
A rate with nothing to divide by is null, never a perfect score.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter
from datetime import date, datetime
from pathlib import Path
from typing import Any

from common import parse_ts, read_json, read_jsonl, repo_root, write_json

MISS_TYPES = {"bump", "late_reply", "stale_crossing", "missed_date"}


def week_monday(week: str) -> date:
    """The Monday of an ISO week written as YYYY-Www."""
    return date.fromisoformat(f"{week}-1")


def iso_week(moment: datetime) -> str:
    year, number, _ = moment.isocalendar()
    return f"{year}-W{number:02d}"


def in_week(record: dict[str, Any], week: str) -> bool:
    at = record.get("at")
    return bool(at) and iso_week(parse_ts(at)) == week


def is_probe(denial: dict[str, Any]) -> bool:
    """A denial the harness provoked on purpose: the preflight canary that proves the hooks are loaded."""
    return str(denial.get("run_id") or "").endswith("-canary")


def surfaced_before(outcome: dict[str, Any]) -> bool:
    """True when the item was surfaced at some moment earlier than the outcome itself."""
    at = parse_ts(outcome["at"])
    return any(parse_ts(ts) < at for ts in outcome.get("surfaced_at") or [])


def is_miss(outcome: dict[str, Any]) -> bool:
    if outcome.get("type") == "prep_gap":
        return True
    return outcome.get("type") in MISS_TYPES and not surfaced_before(outcome)


def bump_recall(outcomes: list[dict[str, Any]]) -> float | None:
    """Bumps that Jarvis surfaced before the principal got bumped, over all bumps; null with no bumps."""
    bumps = [o for o in outcomes if o.get("type") == "bump"]
    if not bumps:
        return None
    return sum(1 for o in bumps if surfaced_before(o)) / len(bumps)


def misses_by_cause(outcomes: list[dict[str, Any]]) -> dict[str, int]:
    return dict(Counter(o.get("cause_class") or "unknown" for o in outcomes if is_miss(o)))


def noise_rate(outcomes: list[dict[str, Any]]) -> float | None:
    """Ignored surfaced items over all surfaced items that got a reaction."""
    acted = sum(1 for o in outcomes if o.get("type") == "acted")
    ignored = sum(1 for o in outcomes if o.get("type") == "ignored")
    if acted + ignored == 0:
        return None
    return ignored / (acted + ignored)


def scored_reactions(outcomes: list[dict[str, Any]]) -> list[tuple[float, float]]:
    """(surfacing score, acted=1 or ignored=0) for every reaction that carries a score."""
    return [
        (float(o["score"]), 1.0 if o["type"] == "acted" else 0.0)
        for o in outcomes
        if o.get("type") in ("acted", "ignored") and o.get("score") is not None
    ]


def brier(outcomes: list[dict[str, Any]]) -> float | None:
    """Brier score of the surfacing score against acted=1, ignored=0."""
    pairs = scored_reactions(outcomes)
    if not pairs:
        return None
    return sum((score - actual) ** 2 for score, actual in pairs) / len(pairs)


def drift(original: list[dict[str, Any]], replayed: list[dict[str, Any]]) -> float | None:
    """Median absolute score difference over the raw ids both candidate lists share."""
    before = {c["raw_id"]: float(c["score"]) for c in original}
    after = {c["raw_id"]: float(c["score"]) for c in replayed}
    shared = before.keys() & after.keys()
    if not shared:
        return None
    return statistics.median(abs(before[raw_id] - after[raw_id]) for raw_id in shared)


def week_coverage(runs: list[dict[str, Any]], week: str) -> float | None:
    """One source's mean of read/available over the week; null when no run of the week has a known total."""
    ratios = [
        r["read"] / r["available"] for r in runs if in_week(r, week) and r.get("available") not in (None, 0)
    ]
    return sum(ratios) / len(ratios) if ratios else None


def worst_coverage(coverage: dict[str, list[dict[str, Any]]], week: str) -> float | None:
    """The lowest per-source mean of read/available over the week; sources with no known total are skipped."""
    means = []
    for runs in coverage.values():
        mean = week_coverage(runs, week)
        if mean is not None:
            means.append(mean)
    return min(means) if means else None


def share(records: list[dict[str, Any]], field: str) -> float | None:
    if not records:
        return None
    return sum(1 for r in records if r.get(field)) / len(records)


def replay_drift(root: Path) -> float | None:
    """Median of the per-replay drifts for every replay whose original candidate list is still on disk."""
    drifts = []
    for replayed_path in sorted((root / "eval" / "replays").glob("*.candidates.jsonl")):
        name = replayed_path.name.removesuffix(".candidates.jsonl")
        original_path = root / "state" / "candidates" / f"{name}.jsonl"
        if not original_path.exists():
            continue
        value = drift(read_jsonl(original_path), read_jsonl(replayed_path))
        if value is not None:
            drifts.append(value)
    return statistics.median(drifts) if drifts else None


def week_records(path: Path, week: str) -> list[dict[str, Any]]:
    return [record for record in read_jsonl(path) if in_week(record, week)]


def bump_count(outcomes: list[dict[str, Any]]) -> int:
    return sum(1 for o in outcomes if o.get("type") == "bump")


def week_metrics(root: Path, week: str) -> dict[str, Any]:
    state = root / "state"
    outcomes = week_records(state / "outcomes.jsonl", week)
    denied = [d for d in week_records(state / "denied_calls.jsonl", week) if not is_probe(d)]
    claims = week_records(state / "claim_checks.jsonl", week)
    preps = week_records(state / "prep_checks.jsonl", week)
    by_cause = misses_by_cause(outcomes)
    return {
        "week": week,
        "bumps": bump_count(outcomes),
        "bump_recall": bump_recall(outcomes),
        "miss_count": sum(by_cause.values()),
        "misses_by_cause": by_cause,
        "noise_rate": noise_rate(outcomes),
        "brier": brier(outcomes),
        "worst_coverage": worst_coverage(read_json(state / "coverage.json", {}), week),
        "claim_verification": share(claims, "supported"),
        "prep_hit_rate": share(preps, "appeared"),
        "guard_denials": len(denied),
        "drift": replay_drift(root),
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[0] != "week":
        print(__doc__, file=sys.stderr)
        return 2
    week = argv[1]
    try:
        week_monday(week)
    except ValueError:
        print(f"not an ISO week: {week} (expected YYYY-Www)", file=sys.stderr)
        return 2
    root = repo_root()
    result = week_metrics(root, week)
    write_json(root / "state" / "metrics" / f"{week}.json", result)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
