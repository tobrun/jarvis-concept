"""Run planning: collapse of late report runs, lookback, weekly, holiday, and resume (scripts/schedule.py)."""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import common
import pytest
import schedule
from conftest import run_script

SLOTS = {
    "morning": "08:00",
    "midday": "13:00",
    "eod": "18:00",
    "consolidate_minute": 30,
    "consolidate_hours": list(range(8, 19)),
    "dream": "02:00",
}
# A weekday holiday, as config/holidays.md would list it.
HOLIDAYS = {date(2026, 11, 11)}


def at(text: str) -> datetime:
    return common.parse_ts(text)


def ok_run(run_id: str, mode: str, slot_date: str, started: str, finished: str) -> dict:
    return {
        "run_id": run_id,
        "mode": mode,
        "slot_date": slot_date,
        "started": started,
        "finished": finished,
        "status": "ok",
    }


# Friday 2026-10-23 eod, before the switch to winter time on Sunday 2026-10-25.
FRIDAY_EOD = ok_run(
    "2026-10-23T1800-eod", "eod", "2026-10-23", "2026-10-23T18:00:00+02:00", "2026-10-23T18:20:00+02:00"
)


def test_morning_on_time_runs_as_morning_with_lookback_24h_before_last_run() -> None:
    result = schedule.plan("morning", at("2026-10-26T08:05:00+01:00"), [FRIDAY_EOD], SLOTS, HOLIDAYS)

    assert result["action"] == "run"
    assert result["mode"] == "morning"
    assert result["run_id"] == "2026-10-26T0805-morning"
    assert result["slot"] == "2026-10-26T08:00:00+01:00"
    assert result["late"] is False
    assert result["late_minutes"] == 0
    assert result["lookback_start"] == "2026-10-22T18:20:00+02:00"
    assert result["first_run"] is False
    assert result["include_morning"] is False
    assert result["resume_run_id"] is None


def test_morning_fired_at_0845_is_late_by_45_minutes() -> None:
    result = schedule.plan("morning", at("2026-10-26T08:45:00+01:00"), [FRIDAY_EOD], SLOTS, HOLIDAYS)

    assert result["mode"] == "morning"
    assert result["late"] is True
    assert result["late_minutes"] == 45


def test_morning_fired_after_midday_collapses_to_midday_and_carries_the_missed_morning() -> None:
    result = schedule.plan("morning", at("2026-10-26T13:20:00+01:00"), [FRIDAY_EOD], SLOTS, HOLIDAYS)

    assert result["action"] == "run"
    assert result["mode"] == "midday"
    assert result["requested"] == "morning"
    assert result["slot"] == "2026-10-26T13:00:00+01:00"
    assert result["late"] is False
    assert result["late_minutes"] == 0
    assert result["include_morning"] is True


def test_morning_fired_at_1340_collapses_to_midday_late_by_40_minutes() -> None:
    result = schedule.plan("morning", at("2026-10-26T13:40:00+01:00"), [FRIDAY_EOD], SLOTS, HOLIDAYS)

    assert result["mode"] == "midday"
    assert result["late"] is True
    assert result["late_minutes"] == 40


def test_collapsed_run_is_skipped_when_midday_already_finished_today() -> None:
    midday = ok_run(
        "2026-10-26T1300-midday",
        "midday",
        "2026-10-26",
        "2026-10-26T13:00:00+01:00",
        "2026-10-26T13:15:00+01:00",
    )

    result = schedule.plan("morning", at("2026-10-26T13:20:00+01:00"), [FRIDAY_EOD, midday], SLOTS, HOLIDAYS)

    assert result["action"] == "skip"
    assert result["mode"] == "midday"


def test_first_run_without_history_looks_back_30_days() -> None:
    result = schedule.plan("morning", at("2026-10-26T08:05:00+01:00"), [], SLOTS, HOLIDAYS)

    assert result["first_run"] is True
    assert result["lookback_start"] == "2026-09-26T08:05:00+02:00"


def test_eod_on_friday_is_weekly_and_thursday_is_not() -> None:
    friday = schedule.plan("eod", at("2026-10-30T18:05:00+01:00"), [FRIDAY_EOD], SLOTS, HOLIDAYS)
    thursday = schedule.plan("eod", at("2026-10-29T18:05:00+01:00"), [FRIDAY_EOD], SLOTS, HOLIDAYS)

    assert friday["weekly"] is True
    assert thursday["weekly"] is False


def test_eod_on_thursday_before_a_friday_holiday_is_weekly(tmp_path: Path) -> None:
    listed = tmp_path / "holidays.md"
    listed.write_text("# Holidays\n\n- 2026-10-30 Company day off\n")
    holidays = common.read_holidays(listed)

    result = schedule.plan("eod", at("2026-10-29T18:05:00+01:00"), [FRIDAY_EOD], SLOTS, holidays)

    assert result["weekly"] is True
    assert result["holiday"] is False


def test_eod_on_a_holiday_runs_with_the_holiday_flag() -> None:
    result = schedule.plan("eod", at("2026-11-11T18:05:00+01:00"), [FRIDAY_EOD], SLOTS, HOLIDAYS)

    assert result["action"] == "run"
    assert result["holiday"] is True
    assert result["weekly"] is False


def test_gap_of_three_days_looks_back_from_24h_before_the_last_run() -> None:
    thursday_eod = ok_run(
        "2026-10-22T1800-eod", "eod", "2026-10-22", "2026-10-22T18:00:00+02:00", "2026-10-22T18:30:00+02:00"
    )

    result = schedule.plan("morning", at("2026-10-26T08:05:00+01:00"), [thursday_eod], SLOTS, HOLIDAYS)

    assert result["lookback_start"] == "2026-10-21T18:30:00+02:00"
    assert result["first_run"] is False


def test_unfinished_run_with_raw_lines_is_resumed(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    (raw / "slack").mkdir(parents=True)
    line = {"raw_id": "r1", "source": "slack", "run_id": "2026-10-26T0800-morning", "payload": {}}
    (raw / "slack" / "2026-10-26.jsonl").write_text(json.dumps(line) + "\n")
    interrupted = {
        "run_id": "2026-10-26T0800-morning",
        "mode": "morning",
        "slot_date": "2026-10-26",
        "started": "2026-10-26T08:00:00+01:00",
        "finished": None,
        "status": "running",
    }
    nothing_fetched = {
        **interrupted,
        "run_id": "2026-10-26T0810-morning",
        "started": "2026-10-26T08:10:00+01:00",
    }
    runs = [FRIDAY_EOD, interrupted, nothing_fetched]

    with_raw = schedule.runs_with_raw_lines(raw, ["2026-10-26T0800-morning", "2026-10-26T0810-morning"])
    result = schedule.plan("morning", at("2026-10-26T08:30:00+01:00"), runs, SLOTS, HOLIDAYS, with_raw)

    assert with_raw == {"2026-10-26T0800-morning"}
    assert result["resume_run_id"] == "2026-10-26T0800-morning"


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        schedule.plan("weekly", at("2026-10-26T08:05:00+01:00"), [], SLOTS, HOLIDAYS)


def test_plan_start_finish_cli_records_the_run_and_skips_a_repeat(repo: Path) -> None:
    env = {"JARVIS_NOW": "2026-10-26T08:05:00+01:00"}
    planned = run_script(repo, "schedule.py", "plan", "morning", env=env)
    assert planned.returncode == 0, planned.stderr
    plan_file = repo / "state" / "plan.json"
    plan_file.write_text(planned.stdout)

    assert run_script(repo, "schedule.py", "start", str(plan_file), env=env).returncode == 0
    finished = run_script(
        repo,
        "schedule.py",
        "finish",
        "2026-10-26T0805-morning",
        "ok",
        env={"JARVIS_NOW": "2026-10-26T08:30:00"},
    )
    assert finished.returncode == 0, finished.stderr
    again = run_script(repo, "schedule.py", "plan", "morning", env={"JARVIS_NOW": "2026-10-26T09:00:00"})

    runs = json.loads((repo / "state" / "runs.json").read_text())["runs"]
    assert runs == [
        {
            "run_id": "2026-10-26T0805-morning",
            "mode": "morning",
            "slot_date": "2026-10-26",
            "started": "2026-10-26T08:05:00+01:00",
            "finished": "2026-10-26T08:30:00+01:00",
            "status": "ok",
        }
    ]
    assert json.loads(again.stdout)["action"] == "skip"
    assert json.loads(again.stdout)["lookback_start"] == "2026-10-25T08:30:00+01:00"


def test_finish_of_an_unknown_run_fails(repo: Path) -> None:
    result = run_script(repo, "schedule.py", "finish", "2026-10-26T0805-morning", "ok")

    assert result.returncode == 2
    assert "no run with id" in result.stderr


def test_morning_fired_before_its_slot_stays_morning() -> None:
    result = schedule.plan("morning", at("2026-10-26T07:10:00+01:00"), [FRIDAY_EOD], SLOTS, HOLIDAYS)

    assert result["mode"] == "morning"
    assert result["slot"] == "2026-10-26T08:00:00+01:00"
    assert result["reason"] == "morning slot 08:00"


def test_dream_runs_at_its_own_slot_and_looks_back_to_the_last_dream() -> None:
    dream = ok_run(
        "2026-10-27T0200-dream",
        "dream",
        "2026-10-27",
        "2026-10-27T02:00:00+01:00",
        "2026-10-27T02:40:00+01:00",
    )

    result = schedule.plan("dream", at("2026-10-28T02:05:00+01:00"), [FRIDAY_EOD, dream], SLOTS, HOLIDAYS)
    first = schedule.plan("dream", at("2026-10-28T02:05:00+01:00"), [FRIDAY_EOD], SLOTS, HOLIDAYS)

    assert result["mode"] == "dream"
    assert result["slot"] == f"2026-10-28T{SLOTS['dream']}:00+01:00"
    assert result["lookback_start"] == "2026-10-27T02:40:00+01:00"
    assert first["lookback_start"] == "2026-10-27T02:05:00+01:00"


def test_raw_scan_stops_once_every_run_is_found(tmp_path: Path) -> None:
    for source in ("github", "slack"):
        (tmp_path / source).mkdir()
        (tmp_path / source / "2026-10-26.jsonl").write_text(json.dumps({"run_id": "r1"}) + "\n")

    assert schedule.runs_with_raw_lines(tmp_path, ["r1"]) == {"r1"}
    assert schedule.runs_with_raw_lines(tmp_path, ["r1", "r2"]) == {"r1"}
    assert schedule.runs_with_raw_lines(tmp_path / "missing", ["r1"]) == set()


def test_finishing_a_run_as_running_is_rejected() -> None:
    with pytest.raises(ValueError, match="not a final status"):
        schedule.finish_run([FRIDAY_EOD], FRIDAY_EOD["run_id"], "running", at("2026-10-26T08:00:00+01:00"))


@pytest.mark.parametrize("argv", [[], ["plan"], ["finish", "r1"], ["resume", "r1"]])
def test_cli_without_a_known_command_prints_the_usage(repo: Path, argv: list[str]) -> None:
    result = run_script(repo, "schedule.py", *argv)

    assert result.returncode == 2
    assert "Usage:" in result.stderr
