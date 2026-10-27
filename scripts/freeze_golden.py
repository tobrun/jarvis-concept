"""Freeze one real week as the golden week that every prompt or harness change is evaluated against.

Usage:
    uv run scripts/freeze_golden.py <YYYY-Www>

Run it after the week plus 5 business days, so the week's outcomes have had time to land.
Copies into eval/golden/<YYYY-Www>/:
    raw/<source>/<date>.jsonl       raw lines fetched on the 7 dates of the week (owner timezone), with the
                                    tombstone registry applied
    labels/<date>.jsonl             curation/labels files of those dates
    outcomes.jsonl                  outcomes citing a raw id of the week, or dated in the week plus 7 days
    manifest.json                   {week, start_commit, frozen_at}
start_commit is the newest commit made before the week's Monday 00:00 in the owner timezone: replay starts
the agent there.
A golden week is never overwritten; delete it by hand to refreeze.
Its raw copy stays under the tombstone registry: raw.py tombstone and forget redact it like raw/.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import raw
from common import now, parse_ts, read_jsonl, repo_root, tz, write_json


def week_dates(week: str) -> list[date]:
    monday = date.fromisoformat(f"{week}-1")
    return [monday + timedelta(days=offset) for offset in range(7)]


def week_start(week: str) -> datetime:
    """Monday 00:00 of the ISO week in the owner timezone."""
    monday = week_dates(week)[0]
    return datetime(monday.year, monday.month, monday.day, tzinfo=tz())


def start_commit(root: Path, before: datetime) -> str | None:
    """The newest commit whose committer date is before the given instant; None outside git or with none."""
    result = subprocess.run(
        ["git", "-C", str(root), "log", f"--before={before.isoformat()}", "-1", "--format=%H"],
        capture_output=True,
        text=True,
        check=False,
    )
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def keep_outcome(outcome: dict[str, Any], raw_ids: set[str], start: datetime) -> bool:
    if raw_ids.intersection(outcome.get("raw_ids") or []):
        return True
    at = outcome.get("at")
    return bool(at) and start <= parse_ts(at) < start + timedelta(days=14)


def _copy_raw(root: Path, staging: Path, names: set[str]) -> set[str]:
    """Copy the raw files named after the week's dates, tombstones applied; returns the raw ids they hold."""
    raw_ids: set[str] = set()
    raw_dir = root / "raw"
    registry = raw.registry(root)
    for path in sorted(raw_dir.glob("*/*.jsonl")) if raw_dir.exists() else []:
        if path.name in names:
            copy = staging / "raw" / path.parent.name / path.name
            copy.parent.mkdir(parents=True, exist_ok=True)
            copy.write_text(raw.apply_registry_text(path.read_text(), registry, root))
            raw_ids.update(line["raw_id"] for line in read_jsonl(path) if "raw_id" in line)
    return raw_ids


def _copy_labels(root: Path, staging: Path, names: set[str]) -> None:
    labels_dir = staging / "labels"
    labels_dir.mkdir()
    for name in sorted(names):
        source = root / "curation" / "labels" / name
        if source.exists():
            shutil.copy2(source, labels_dir / name)


def _copy_outcomes(root: Path, staging: Path, raw_ids: set[str], start: datetime) -> None:
    outcomes = [o for o in read_jsonl(root / "state" / "outcomes.jsonl") if keep_outcome(o, raw_ids, start)]
    (staging / "outcomes.jsonl").write_text(
        "".join(json.dumps(o, ensure_ascii=False, sort_keys=True) + "\n" for o in outcomes)
    )


def freeze(root: Path, week: str) -> dict[str, Any]:
    target = root / "eval" / "golden" / week
    if target.exists():
        raise FileExistsError(f"{target} already exists; a golden week is never overwritten")
    names = {f"{day.isoformat()}.jsonl" for day in week_dates(week)}
    staging = target.with_name(f".{week}.tmp")
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)

    raw_ids = _copy_raw(root, staging, names)
    _copy_labels(root, staging, names)
    start = week_start(week)
    _copy_outcomes(root, staging, raw_ids, start)

    manifest = {"week": week, "start_commit": start_commit(root, start), "frozen_at": now().isoformat()}
    write_json(staging / "manifest.json", manifest)
    staging.rename(target)
    return manifest


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    week = argv[0]
    try:
        week_dates(week)
    except ValueError:
        print(f"not an ISO week: {week} (expected YYYY-Www)", file=sys.stderr)
        return 2
    try:
        manifest = freeze(repo_root(), week)
    except FileExistsError as error:
        print(error, file=sys.stderr)
        return 1
    if manifest["start_commit"] is None:
        print("warning: no commit before the week starts; replay has no start commit", file=sys.stderr)
    print(json.dumps(manifest, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
