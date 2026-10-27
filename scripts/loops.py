"""Open loops in memory/loops/<id>.md: list the stale ones, snooze one.

Usage:
  loops.py stale                  prints a JSON list of stale loops, each with a `reason`
  loops.py snooze <id> [--days N] sets snoozed_until N business days ahead (default from thresholds)

A loop file is YAML frontmatter between `---` lines, then a free markdown body:
  id             the file stem
  type           waiting_on_me | waiting_on_other | decision | deadline | watch
  kind           null, or a source loop kind such as jira_in_progress or pr_review
  status         open | slipping | resolved | dropped
  last_movement  ISO timestamp of the last observed movement
  due            YYYY-MM-DD or null
  snoozed_until  YYYY-MM-DD or null; a loop is hidden while snoozed_until is after today
  surfaced_at    list of ISO timestamps
  raw_ids        list of raw ids

A loop with status open or slipping is stale when, per config/thresholds.md:
  kind <k>                calendar days since last_movement >= stale_<k>_days, for every kind with that key
                          (source adapters add their kinds and keys, e.g. jira_in_progress, pr_review)
  type waiting_on_me, waiting_on_other, decision (no kind)
                          business days since last_movement >= stale_waiting_business_days
  any loop                due <= today
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Collection
from datetime import date, datetime
from pathlib import Path
from typing import Any

import common
import yaml
from bizdays import add_business_days, business_days_between

WAITING_TYPES = {"waiting_on_me", "waiting_on_other", "decision"}
LIVE_STATUSES = {"open", "slipping"}


def split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    """The YAML frontmatter as a dict (dates kept as ISO strings) and the body after it."""
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return {}, text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return _meta("".join(lines[1:index])), "".join(lines[index + 1 :])
    return {}, text


def _meta(front: str) -> dict[str, Any]:
    meta = yaml.safe_load(front) or {}
    return {key: _plain(value) for key, value in meta.items()}


def _plain(value: Any) -> Any:
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def join_frontmatter(meta: dict[str, Any], body: str) -> str:
    front = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True, default_flow_style=False)
    return f"---\n{front}---\n{body}"


def stale_reason(
    loop: dict[str, Any], moment: datetime, limits: dict[str, Any], holidays: Collection[date]
) -> str | None:
    """Why the loop is stale at `moment`, or None when it is fine, closed, or snoozed."""
    today = moment.date()
    if _hidden(loop, today):
        return None
    due = loop.get("due")
    if due and date.fromisoformat(due) <= today:
        return f"due {due}"
    return idle_reason(loop, today, limits, holidays)


def _hidden(loop: dict[str, Any], today: date) -> bool:
    """Closed, or snoozed until after today."""
    snoozed = loop.get("snoozed_until")
    return loop.get("status") not in LIVE_STATUSES or bool(snoozed and date.fromisoformat(snoozed) > today)


def idle_reason(
    loop: dict[str, Any], today: date, limits: dict[str, Any], holidays: Collection[date]
) -> str | None:
    """Why the loop has gone without movement too long, or None; the threshold depends on kind and type."""
    if not loop.get("last_movement"):
        return None
    moved = common.parse_ts(loop["last_movement"]).date()
    kind = loop.get("kind")
    if kind and f"stale_{kind}_days" in limits:
        days = (today - moved).days
        limit = int(limits[f"stale_{kind}_days"])
        if days >= limit:
            return f"{kind} idle {days} calendar days (threshold {limit})"
        return None
    if loop.get("type") in WAITING_TYPES:
        days = business_days_between(moved, today, holidays)
        limit = int(limits["stale_waiting_business_days"])
        if days >= limit:
            return f"{loop['type']} idle {days} business days (threshold {limit})"
    return None


def stale_loops(
    loops: list[dict[str, Any]], moment: datetime, limits: dict[str, Any], holidays: Collection[date]
) -> list[dict[str, Any]]:
    found = []
    for loop in loops:
        reason = stale_reason(loop, moment, limits, holidays)
        if reason:
            found.append({**loop, "reason": reason})
    return found


def loops_dir(root: Path) -> Path:
    return root / "memory" / "loops"


def read_loops(root: Path) -> list[dict[str, Any]]:
    loops = []
    for path in sorted(loops_dir(root).glob("*.md")):
        meta, _ = split_frontmatter(path.read_text())
        loops.append({"id": path.stem, **meta})
    return loops


def snooze(root: Path, loop_id: str, days: int, today: date, holidays: Collection[date]) -> str:
    """Set snoozed_until on the loop file, keeping its body; returns the new date."""
    path = loops_dir(root) / f"{loop_id}.md"
    meta, body = split_frontmatter(path.read_text())
    until = add_business_days(today, days, holidays).isoformat()
    meta["snoozed_until"] = until
    path.write_text(join_frontmatter(meta, body))
    return until


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("stale")
    snooze_cmd = commands.add_parser("snooze")
    snooze_cmd.add_argument("id")
    snooze_cmd.add_argument("--days", type=int)
    args = parser.parse_args(argv)

    root = common.repo_root()
    limits = common.thresholds(root)
    holidays = common.holidays(root)
    moment = common.now()
    if args.command == "stale":
        print(json.dumps(stale_loops(read_loops(root), moment, limits, holidays), indent=2))
        return 0
    if not (loops_dir(root) / f"{args.id}.md").exists():
        print(f"no such loop: {args.id}", file=sys.stderr)
        return 1
    days = args.days if args.days is not None else int(limits["snooze_default_business_days"])
    print(snooze(root, args.id, days, moment.date(), holidays))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
