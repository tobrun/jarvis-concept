"""Run planning for scheduled jobs: effective mode, late runs, lookback, weekly, holiday, and resume.

Usage:
  schedule.py plan <mode>                  print the plan for a requested mode as JSON
  schedule.py start <plan json file>       record the planned run as running in state/runs.json
  schedule.py finish <run_id> <status>     set finished=now and the final status of a run

Modes: morning, midday, eod (report modes), consolidate, dream.
Every decision is made in the owner's timezone (config/jarvis.yaml) through common.now(), which honors
JARVIS_NOW. The scheduler may fire on the host's local time; this script decides in the owner's zone, so a
host in another timezone still gets the right effective mode.

state/runs.json:
  {"runs": [{"run_id", "mode", "slot_date": "YYYY-MM-DD", "started": ISO, "finished": ISO | null,
             "status": "running" | "ok" | "failed" | "timeout" | "skipped"}]}
  A missing file means the first run.

`plan` output (JSON object):
  action           "run" or "skip"; skip when a report of the effective mode already finished ok today
  reason           one line explaining the action
  mode             the effective mode; report modes collapse to the latest report slot that has passed
  requested        the mode the scheduler (or a person) asked for
  run_id           "<YYYY-MM-DD>T<HHMM>-<mode>", date and time of now, mode the effective one
  slot             ISO time of the effective slot today
  late_minutes     minutes past the slot when more than 30, else 0
  late             true when now is more than 30 minutes past the effective slot
  lookback_start   ISO; report modes: latest `finished` of an ok report run minus 24 hours, or now minus
                   30 days on the first run; consolidate and dream: the last ok run of the same mode
                   finished, or now minus 24 hours
  first_run        true when no report run has ever finished ok
  weekly           eod on the last business day of its ISO week (a holiday on that day moves it earlier)
  holiday          today is not a working day or is listed in config/holidays.md; the run still happens,
                   the report goes unsent
  include_morning  effective mode is midday or eod and no morning run finished ok today
  resume_run_id    the latest run still marked running whose run_id appears in raw/**/*.jsonl, else null
"""

from __future__ import annotations

import json
import sys
from collections.abc import Collection, Iterable
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import common
from bizdays import is_business_day

REPORT_MODES = ("morning", "midday", "eod")
MEMORY_MODES = ("consolidate", "dream")
MODES = REPORT_MODES + MEMORY_MODES
STATUSES = ("running", "ok", "failed", "timeout", "skipped")
LATE_AFTER = timedelta(minutes=30)
FIRST_RUN_LOOKBACK = timedelta(days=30)
LOOKBACK_OVERLAP = timedelta(hours=24)
# How many arguments each command of main() takes.
ARGUMENT_COUNTS = {"plan": 1, "start": 1, "finish": 2}


def _clock(text: str) -> time:
    hour, minute = text.split(":")
    return time(int(hour), int(minute))


def _at(day: date, moment: time) -> datetime:
    return datetime.combine(day, moment, tzinfo=common.tz())


def report_slots(day: date, schedule: dict[str, Any]) -> dict[str, datetime]:
    return {mode: _at(day, _clock(schedule[mode])) for mode in REPORT_MODES}


def consolidate_slot(now: datetime, schedule: dict[str, Any]) -> datetime:
    """The latest consolidate slot today at or before now; the first one when none has passed yet."""
    minute = int(schedule["consolidate_minute"])
    slots = [_at(now.date(), time(int(hour), minute)) for hour in sorted(schedule["consolidate_hours"])]
    passed = [slot for slot in slots if slot <= now]
    return passed[-1] if passed else slots[0]


def effective_slot(requested: str, now: datetime, schedule: dict[str, Any]) -> tuple[str, datetime]:
    """The mode a run should act as and its slot today.

    A report mode collapses to the latest report slot that has passed, so a late morning wake at 13:20
    becomes midday. Fired before its own slot (a manual early run), it stays the requested mode.
    """
    if requested in REPORT_MODES:
        slots = report_slots(now.date(), schedule)
        if now < slots[requested]:
            return requested, slots[requested]
        mode = max((m for m in REPORT_MODES if slots[m] <= now), key=lambda m: slots[m])
        return mode, slots[mode]
    if requested == "consolidate":
        return requested, consolidate_slot(now, schedule)
    return requested, _at(now.date(), _clock(schedule[requested]))


def is_last_business_day_of_week(day: date, holidays: Collection[date]) -> bool:
    if not is_business_day(day, holidays):
        return False
    later = (day + timedelta(days=n) for n in range(1, 7 - day.weekday()))
    return not any(is_business_day(other, holidays) for other in later)


def _ok_today(runs: Iterable[dict[str, Any]], mode: str, day: date) -> bool:
    return any(
        run.get("mode") == mode and run.get("slot_date") == day.isoformat() and run.get("status") == "ok"
        for run in runs
    )


def _latest_finished(runs: Iterable[dict[str, Any]], modes: Collection[str]) -> datetime | None:
    finished = [
        common.parse_ts(run["finished"])
        for run in runs
        if run.get("mode") in modes and run.get("status") == "ok" and run.get("finished")
    ]
    return max(finished, default=None)


def resume_candidate(runs: Iterable[dict[str, Any]], runs_with_raw: Collection[str]) -> str | None:
    """The latest run started and never finished that left raw lines behind."""
    open_runs = [
        run
        for run in runs
        if run.get("status") == "running" and not run.get("finished") and run.get("run_id") in runs_with_raw
    ]
    if not open_runs:
        return None
    return max(open_runs, key=lambda run: common.parse_ts(run["started"]))["run_id"]


def _lookback(runs: list[dict[str, Any]], mode: str, now: datetime) -> datetime:
    if mode in REPORT_MODES:
        last_report = _latest_finished(runs, REPORT_MODES)
        return now - FIRST_RUN_LOOKBACK if last_report is None else last_report - LOOKBACK_OVERLAP
    last_same = _latest_finished(runs, (mode,))
    return now - LOOKBACK_OVERLAP if last_same is None else last_same


def _action(
    runs: list[dict[str, Any]], requested: str, mode: str, slot: datetime, today: date
) -> tuple[str, str]:
    if mode in REPORT_MODES and _ok_today(runs, mode, today):
        return "skip", f"{mode} already finished ok for {today.isoformat()}"
    if mode != requested:
        return "run", f"{requested} collapsed to {mode}, the latest passed slot"
    return "run", f"{mode} slot {slot.strftime('%H:%M')}"


def plan(
    requested: str,
    now: datetime,
    runs: list[dict[str, Any]],
    schedule: dict[str, Any],
    holidays: Collection[date],
    runs_with_raw: Collection[str] = (),
) -> dict[str, Any]:
    """Decide what a run fired for `requested` at `now` should do, given the run history."""
    if requested not in MODES:
        raise ValueError(f"unknown mode: {requested}")
    today = now.date()
    mode, slot = effective_slot(requested, now, schedule)
    overdue = now - slot
    late = overdue > LATE_AFTER
    late_minutes = int(overdue.total_seconds() // 60) if late else 0
    action, reason = _action(runs, requested, mode, slot, today)
    if late:
        reason += f", late by {late_minutes} minutes"

    return {
        "action": action,
        "reason": reason,
        "mode": mode,
        "requested": requested,
        "run_id": f"{today.isoformat()}T{now.strftime('%H%M')}-{mode}",
        "slot": slot.isoformat(),
        "late_minutes": late_minutes,
        "late": late,
        "lookback_start": _lookback(runs, mode, now).isoformat(),
        "first_run": _latest_finished(runs, REPORT_MODES) is None,
        "weekly": mode == "eod" and is_last_business_day_of_week(today, holidays),
        "holiday": not is_business_day(today, holidays),
        "include_morning": mode in ("midday", "eod") and not _ok_today(runs, "morning", today),
        "resume_run_id": resume_candidate(runs, runs_with_raw),
    }


def runs_with_raw_lines(raw_dir: Path, run_ids: Collection[str]) -> set[str]:
    """Which of `run_ids` have at least one line in raw/**/*.jsonl."""
    wanted = set(run_ids)
    found: set[str] = set()
    if not wanted or not raw_dir.exists():
        return found
    for path in sorted(raw_dir.rglob("*.jsonl")):
        found |= wanted & {record.get("run_id") for record in common.read_jsonl(path)}
        if found == wanted:
            break
    return found


def start_run(runs: list[dict[str, Any]], planned: dict[str, Any], now: datetime) -> list[dict[str, Any]]:
    entry = {
        "run_id": planned["run_id"],
        "mode": planned["mode"],
        "slot_date": common.parse_ts(planned["slot"]).date().isoformat(),
        "started": now.isoformat(),
        "finished": None,
        "status": "running",
    }
    return [*runs, entry]


def finish_run(runs: list[dict[str, Any]], run_id: str, status: str, now: datetime) -> list[dict[str, Any]]:
    if status not in STATUSES or status == "running":
        raise ValueError(f"not a final status: {status}")
    matches = [run for run in runs if run.get("run_id") == run_id]
    if not matches:
        raise KeyError(f"no run with id {run_id}")
    matches[-1].update(finished=now.isoformat(), status=status)
    return runs


def runs_path(root: Path) -> Path:
    return root / "state" / "runs.json"


def plan_now(
    root: Path, requested: str, runs: list[dict[str, Any]], schedule: dict[str, Any]
) -> dict[str, Any]:
    """The plan for `requested` at common.now(); open runs that left raw lines are resume candidates."""
    running = [run["run_id"] for run in runs if run.get("status") == "running"]
    with_raw = runs_with_raw_lines(root / "raw", running)
    return plan(requested, common.now(), runs, schedule, common.holidays(root), with_raw)


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    root = common.repo_root()
    path = runs_path(root)
    runs = common.read_json(path, {"runs": []}).get("runs", [])
    command, args = argv[0], argv[1:]
    if len(args) != ARGUMENT_COUNTS.get(command):
        print(__doc__, file=sys.stderr)
        return 2
    try:
        if command == "plan":
            result = plan_now(root, args[0], runs, common.jarvis_config(root)["schedule"])
            print(json.dumps(result, indent=2, sort_keys=True))
        elif command == "start":
            planned = json.loads(Path(args[0]).read_text())
            common.write_json(path, {"runs": start_run(runs, planned, common.now())})
        else:
            common.write_json(path, {"runs": finish_run(runs, args[0], args[1], common.now())})
    except (ValueError, KeyError) as error:
        print(f"schedule.py: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
