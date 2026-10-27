import json
from datetime import date

import pytest
from append import FIELDS, LedgerError, ledger_path, validate
from conftest import run_script

VALID_OUTCOME = {
    "type": "late_reply",
    "raw_ids": ["slack:C01:1730000000.000100"],
    "surfaced_at": ["2026-10-26T08:30:00+01:00"],
}


def test_outcome_missing_raw_ids_is_rejected_naming_the_field():
    record = {"type": "late_reply", "surfaced_at": []}

    with pytest.raises(LedgerError, match="missing field raw_ids"):
        validate("outcomes", record)


def test_unknown_ledger_name_is_rejected():
    with pytest.raises(LedgerError, match="unknown ledger: diary"):
        validate("diary", VALID_OUTCOME)


def test_unknown_ledger_from_the_cli_exits_1_and_writes_nothing(repo, tmp_path):
    source = tmp_path / "record.json"
    source.write_text(json.dumps(VALID_OUTCOME))

    result = run_script(repo, "append.py", "diary", "--file", str(source))

    assert result.returncode == 1
    assert "unknown ledger: diary" in result.stderr
    assert list((repo / "state").iterdir()) == []


def test_valid_outcome_appended_twice_keeps_the_earlier_line_byte_identical(repo, tmp_path):
    source = tmp_path / "record.json"
    source.write_text(json.dumps(VALID_OUTCOME))
    ledger = repo / "state" / "outcomes.jsonl"
    env = {"JARVIS_NOW": "2026-10-26T09:15:00+01:00"}

    first = run_script(repo, "append.py", "outcomes", "--file", str(source), env=env)
    after_first = ledger.read_bytes()
    second = run_script(repo, "append.py", "outcomes", "--file", str(source), env=env)

    assert first.returncode == 0, first.stderr
    assert second.returncode == 0, second.stderr
    lines = ledger.read_bytes().splitlines(keepends=True)
    assert len(lines) == 2
    assert lines[0] == after_first
    assert json.loads(lines[0])["at"] == "2026-10-26T09:15:00+01:00"


def test_candidate_lands_in_the_dated_per_mode_ledger(repo, tmp_path):
    source = tmp_path / "candidate.json"
    candidate = {
        "raw_id": "gmail:18c2",
        "score": 0.72,
        "features": {"from_manager": True},
        "reason": "manager asked a direct question",
        "decision": "surface",
        "mode": "morning",
    }
    source.write_text(json.dumps(candidate))

    result = run_script(
        repo, "append.py", "candidates", "--file", str(source), env={"JARVIS_NOW": "2026-10-26T08:00:00"}
    )

    assert result.returncode == 0, result.stderr
    assert (repo / "state" / "candidates" / "2026-10-26-morning.jsonl").exists()


def test_prep_check_without_appeared_is_rejected_naming_the_field():
    with pytest.raises(LedgerError, match="missing field appeared"):
        validate("prep_checks", {"meeting_raw_id": "gcal:abc", "item": "Q4 roadmap doc"})


def test_record_that_is_not_an_object_is_rejected():
    with pytest.raises(LedgerError, match="outcomes: record must be a JSON object"):
        validate("outcomes", [VALID_OUTCOME])


def test_present_field_with_an_invalid_value_is_rejected_naming_the_field_and_value():
    with pytest.raises(LedgerError, match='outcomes: invalid field type: "promoted"'):
        validate("outcomes", {**VALID_OUTCOME, "type": "promoted"})
    with pytest.raises(LedgerError, match=r"candidates: invalid field score: 1\.5"):
        validate(
            "candidates",
            {
                "raw_id": "x",
                "score": 1.5,
                "features": {},
                "reason": "r",
                "decision": "surface",
                "mode": "eod",
            },
        )


def test_valid_records_pass():
    validate("outcomes", VALID_OUTCOME)
    validate("report_messages", {"message_id": "1730000000.000100", "raw_id": None, "kind": "report"})


TRAVERSAL_CANDIDATE = {
    "raw_id": "slack:C01:1730000000.000100",
    "score": 0.9,
    "features": {},
    "reason": "forged",
    "decision": "surface",
    "mode": "x/../../../raw/slack/2026-10-26",
}


def _files(repo):
    return sorted(p for p in repo.rglob("*") if p.is_file() and not {".git", "__pycache__"} & set(p.parts))


def test_candidate_mode_that_traverses_out_of_the_ledger_exits_1_and_writes_nothing(repo, tmp_path):
    source = tmp_path / "candidate.json"
    source.write_text(json.dumps(TRAVERSAL_CANDIDATE))
    before = _files(repo)

    result = run_script(
        repo, "append.py", "candidates", "--file", str(source), env={"JARVIS_NOW": "2026-10-26T08:00:00"}
    )

    assert result.returncode == 1
    assert "candidates: invalid field mode" in result.stderr
    assert not (repo / "raw" / "slack" / "2026-10-26.jsonl").exists()
    assert _files(repo) == before


@pytest.mark.parametrize("mode", ["consolidate", "replay", "Morning", "morning/..", "../morning", "", 7])
def test_candidate_mode_outside_the_report_modes_is_rejected_naming_the_field(mode):
    with pytest.raises(LedgerError, match="candidates: invalid field mode"):
        validate("candidates", {**TRAVERSAL_CANDIDATE, "mode": mode})


@pytest.mark.parametrize("mode", ["morning", "midday", "eod"])
def test_candidate_in_every_report_mode_passes(mode):
    validate("candidates", {**TRAVERSAL_CANDIDATE, "mode": mode})


def test_ledger_path_refuses_a_target_outside_its_ledger_directory(tmp_path):
    record = {**TRAVERSAL_CANDIDATE}

    with pytest.raises(LedgerError, match="candidates: path escapes state/candidates"):
        ledger_path(tmp_path, "candidates", record, date(2026, 10, 26))


def test_ledger_path_for_every_ledger_stays_inside_its_directory(tmp_path):
    for ledger in FIELDS:
        path = ledger_path(tmp_path, ledger, {"mode": "morning"}, date(2026, 10, 26))
        assert path.resolve().is_relative_to(tmp_path.resolve())


def test_unhashable_enum_value_is_rejected_naming_the_field_instead_of_crashing():
    with pytest.raises(LedgerError, match="candidates: invalid field mode"):
        validate("candidates", {**TRAVERSAL_CANDIDATE, "mode": ["morning"]})
    with pytest.raises(LedgerError, match="candidates: invalid field decision"):
        validate("candidates", {**TRAVERSAL_CANDIDATE, "mode": "morning", "decision": {"surface": 1}})
