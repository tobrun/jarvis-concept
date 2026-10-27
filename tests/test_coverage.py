import json

from conftest import run_script
from source_coverage import assess

LIMITS = {"coverage_degraded_fraction": 0.5, "coverage_history_runs": 10, "coverage_min_history": 3}


def entry(read, available=100):
    return {"run_id": "r", "at": "2026-10-26T08:00:00+01:00", "available": available, "read": read}


def test_ratio_of_0_4_against_median_0_9_is_degraded():
    verdict = assess([entry(90), entry(85), entry(95), entry(40)], LIMITS)
    assert verdict == {"degraded": True, "current": 0.4, "median": 0.9}


def test_ratio_of_0_5_against_median_0_9_is_not_degraded():
    verdict = assess([entry(90), entry(85), entry(95), entry(50)], LIMITS)
    assert verdict == {"degraded": False, "current": 0.5, "median": 0.9}


def test_null_available_falls_back_to_read_counts_and_10_against_median_40_is_degraded():
    entries = [entry(40, None), entry(35, None), entry(45, None), entry(10, None)]
    assert assess(entries, LIMITS) == {"degraded": True, "current": 10.0, "median": 40.0}


def test_fewer_than_three_history_points_is_never_degraded():
    verdict = assess([entry(90), entry(90), entry(5)], LIMITS)
    assert verdict["degraded"] is False


def test_recorded_runs_are_checked_from_the_coverage_file(repo):
    env = {"JARVIS_NOW": "2026-10-26T08:00:00+01:00", "JARVIS_RUN_ID": "morning-2026-10-26"}
    for read in ("90", "85", "95", "40"):
        assert run_script(repo, "source_coverage.py", "record", "slack", "100", read, env=env).returncode == 0

    result = run_script(repo, "source_coverage.py", "check", env=env)

    assert json.loads(result.stdout) == {"slack": {"degraded": True, "current": 0.4, "median": 0.9}}
    stored = json.loads((repo / "state" / "coverage.json").read_text())
    assert stored["slack"][0]["run_id"] == "morning-2026-10-26"


def test_record_with_a_non_integer_count_exits_2_and_writes_nothing(repo):
    for available, read in (("many", "3"), ("100", "null")):
        result = run_script(repo, "source_coverage.py", "record", "slack", available, read)

        assert result.returncode == 2
        assert "available must be an integer or null, read an integer" in result.stderr
    assert not (repo / "state" / "coverage.json").exists()


def test_record_with_null_available_stores_null(repo):
    assert run_script(repo, "source_coverage.py", "record", "calendar", "null", "7").returncode == 0

    stored = json.loads((repo / "state" / "coverage.json").read_text())
    assert stored["calendar"][0]["available"] is None
    assert stored["calendar"][0]["run_id"] == "manual"


def test_unknown_command_prints_usage_and_exits_2(repo):
    for args in ((), ("record", "slack"), ("check", "extra")):
        result = run_script(repo, "source_coverage.py", *args)

        assert result.returncode == 2
        assert "Usage:" in result.stderr
