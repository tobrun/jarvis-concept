import json

import metrics
from conftest import run_script


def bump(at, surfaced_at=(), cause="not_candidate"):
    return {
        "type": "bump",
        "raw_ids": ["slack:1"],
        "at": at,
        "surfaced_at": list(surfaced_at),
        "cause_class": cause,
    }


def test_bump_recall_counts_bumps_surfaced_before_the_bump():
    outcomes = [
        bump("2026-10-27T10:00:00+01:00", ["2026-10-26T08:00:00+01:00"]),
        bump("2026-10-27T10:00:00+01:00", ["2026-10-27T08:00:00+01:00"]),
        bump("2026-10-27T10:00:00+01:00", ["2026-10-27T13:00:00+01:00"]),
    ]
    assert round(metrics.bump_recall(outcomes), 3) == 0.667


def test_bump_recall_with_no_bumps_is_null_not_perfect():
    outcomes = [{"type": "acted", "raw_ids": ["slack:1"], "at": "2026-10-27T10:00:00+01:00", "score": 0.9}]
    assert metrics.bump_recall(outcomes) is None


def test_noise_rate_is_ignored_over_surfaced_items_with_a_reaction():
    outcomes = [{"type": "acted", "score": 0.8}] * 3 + [{"type": "ignored", "score": 0.7}]
    assert metrics.noise_rate(outcomes) == 0.25


def test_calibration_brier_score_for_one_acted_and_one_ignored():
    outcomes = [{"type": "acted", "score": 0.9}, {"type": "ignored", "score": 0.2}]
    assert round(metrics.brier(outcomes), 6) == 0.025


def test_drift_is_the_median_score_change_over_shared_raw_ids():
    original = [
        {"raw_id": "a", "score": 0.5},
        {"raw_id": "b", "score": 0.4},
        {"raw_id": "c", "score": 0.9},
        {"raw_id": "only-original", "score": 0.1},
    ]
    replayed = [
        {"raw_id": "a", "score": 0.6},
        {"raw_id": "b", "score": 0.7},
        {"raw_id": "c", "score": 0.7},
        {"raw_id": "only-replay", "score": 0.9},
    ]
    assert round(metrics.drift(original, replayed), 6) == 0.2


def write_jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_week_metrics_from_outcomes_and_denials_on_disk(repo):
    write_jsonl(
        repo / "state" / "outcomes.jsonl",
        [
            # 23:30 UTC on Sunday is 00:30 Monday in Amsterdam: week 44.
            bump("2026-10-25T23:30:00+00:00", ["2026-10-25T20:00:00+00:00"]),
            bump("2026-10-28T10:00:00+01:00", cause="scored_low"),
            {
                "type": "prep_gap",
                "raw_ids": ["cal:1"],
                "at": "2026-10-29T09:00:00+01:00",
                "cause_class": "prep",
            },
            {"type": "acted", "raw_ids": ["slack:2"], "at": "2026-10-28T11:00:00+01:00", "score": 0.9},
            {"type": "ignored", "raw_ids": ["slack:3"], "at": "2026-10-28T11:00:00+01:00", "score": 0.2},
            # Week 43 and week 45: not counted.
            bump("2026-10-25T22:30:00+01:00", cause="not_fetched"),
            bump("2026-11-02T09:00:00+01:00", cause="not_fetched"),
        ],
    )
    write_jsonl(
        repo / "state" / "denied_calls.jsonl",
        [
            {
                "at": "2026-10-27T08:01:00+01:00",
                "mode": "morning",
                "run_id": "r1",
                "tool": "Bash",
                "reason": "not allowed",
                "input_sha256": "x",
            },
            {
                "at": "2026-10-30T18:01:00+01:00",
                "mode": "eod",
                "run_id": "r2",
                "tool": "Write",
                "reason": "outside write scope",
                "input_sha256": "y",
            },
            # The preflight canary's own denial proves the hooks work; it is not a guard denial of a run.
            {
                "at": "2026-10-27T08:00:30+01:00",
                "mode": "morning",
                "run_id": "2026-10-27T0800-morning-canary",
                "tool": "WebFetch",
                "reason": "not allowed",
                "input_sha256": "c",
            },
            {
                "at": "2026-11-03T08:01:00+01:00",
                "mode": "morning",
                "run_id": "r3",
                "tool": "Bash",
                "reason": "not allowed",
                "input_sha256": "z",
            },
        ],
    )

    result = run_script(repo, "metrics.py", "week", "2026-W44")

    assert result.returncode == 0, result.stderr
    written = json.loads((repo / "state" / "metrics" / "2026-W44.json").read_text())
    assert json.loads(result.stdout) == written
    assert written["week"] == "2026-W44"
    assert written["bumps"] == 2
    assert written["bump_recall"] == 0.5
    assert written["miss_count"] == 2
    assert written["misses_by_cause"] == {"scored_low": 1, "prep": 1}
    assert written["noise_rate"] == 0.5
    assert round(written["brier"], 6) == 0.025
    assert written["guard_denials"] == 2
    assert written["worst_coverage"] is None
    assert written["drift"] is None


def test_rates_with_nothing_to_divide_by_are_null():
    assert metrics.noise_rate([bump("2026-10-27T10:00:00+01:00")]) is None
    assert metrics.brier([{"type": "acted", "score": None}, {"type": "bump"}]) is None
    assert metrics.drift([{"raw_id": "a", "score": 0.5}], [{"raw_id": "b", "score": 0.5}]) is None
    assert metrics.share([], "supported") is None


def test_share_counts_records_whose_field_is_truthy():
    assert (
        metrics.share([{"supported": True}, {"supported": False}, {}, {"supported": True}], "supported")
        == 0.5
    )


def coverage_run(at, read, available=100):
    return {"run_id": "r", "at": at, "available": available, "read": read}


def test_worst_coverage_is_the_lowest_source_mean_of_the_week_skipping_unknown_totals():
    coverage = {
        "slack": [
            coverage_run("2026-10-27T08:00:00+01:00", 90),
            coverage_run("2026-10-28T08:00:00+01:00", 70),
            # Week 45: not counted.
            coverage_run("2026-11-02T08:00:00+01:00", 0),
        ],
        "gmail": [
            coverage_run("2026-10-27T08:00:00+01:00", 60),
            coverage_run("2026-10-28T08:00:00+01:00", 5, 0),
        ],
        "calendar": [coverage_run("2026-10-27T08:00:00+01:00", 3, None)],
    }

    assert metrics.worst_coverage(coverage, "2026-W44") == 0.6
    assert metrics.worst_coverage({"calendar": coverage["calendar"]}, "2026-W44") is None


def test_replay_drift_is_the_median_over_replays_whose_original_is_still_on_disk(tmp_path):
    replays = tmp_path / "eval" / "replays"
    originals = tmp_path / "state" / "candidates"
    for name, before, after in [
        ("2026-10-26-morning", 0.5, 0.6),
        ("2026-10-27-morning", 0.5, 0.8),
        ("2026-10-28-morning", 0.5, 0.9),
    ]:
        write_jsonl(originals / f"{name}.jsonl", [{"raw_id": "a", "score": before}])
        write_jsonl(replays / f"{name}.candidates.jsonl", [{"raw_id": "a", "score": after}])
    # No raw id in common: no drift to contribute.
    write_jsonl(originals / "2026-10-29-morning.jsonl", [{"raw_id": "b", "score": 0.5}])
    write_jsonl(replays / "2026-10-29-morning.candidates.jsonl", [{"raw_id": "c", "score": 0.5}])
    # The original candidate list is gone: skipped.
    write_jsonl(replays / "2026-10-30-morning.candidates.jsonl", [{"raw_id": "a", "score": 0.0}])

    assert round(metrics.replay_drift(tmp_path), 6) == 0.3
    (originals / "2026-10-28-morning.jsonl").unlink()
    assert round(metrics.replay_drift(tmp_path), 6) == 0.2


def test_replay_drift_without_replays_is_null(tmp_path):
    assert metrics.replay_drift(tmp_path) is None


def test_week_cli_rejects_a_malformed_week_and_a_missing_argument(repo):
    malformed = run_script(repo, "metrics.py", "week", "2026-44")
    missing = run_script(repo, "metrics.py", "week")

    assert malformed.returncode == 2
    assert "not an ISO week: 2026-44 (expected YYYY-Www)" in malformed.stderr
    assert missing.returncode == 2
    assert "Usage:" in missing.stderr
    assert not (repo / "state" / "metrics").exists()
