"""Append one validated JSON line to a named ledger; the only writer of the append-only ledgers.

Usage:
  append.py <ledger> --file <path to a JSON object file>

The record gets `at` (common.now() ISO) when it has none, and is appended under an exclusive lock.
An unknown ledger or a missing or invalid field exits 1 naming it, and nothing is written.
<date> below is common.now()'s date in the owner's timezone.

Ledgers, their files, and their required fields:
  outcomes         state/outcomes.jsonl
                   type in {bump, late_reply, stale_crossing, missed_date, prep_gap, acted, ignored},
                   raw_ids (non-empty list), surfaced_at (list)
  labels           curation/labels/<date>.jsonl
                   raw_id, prediction, label, reason
  candidates       state/candidates/<date>-<mode>.jsonl
                   raw_id, score (0..1), features, reason, decision in {surface, suppress},
                   mode in schedule.REPORT_MODES
  suppressed       state/suppressed/<date>.jsonl
                   raw_id, score, reason
  claim_checks     state/claim_checks.jsonl
                   claim_id, supported (bool)
  surfaced         state/surfaced.jsonl
                   key, raw_id
  report_messages  state/report_messages.jsonl
                   message_id (the delivered message's id: Slack ts, mail id, ...), raw_id (string or
                   null), kind in {report, item}
  prep_checks      state/prep_checks.jsonl
                   meeting_raw_id, item, appeared (bool)

A path that would resolve outside its ledger's directory is refused as well, whatever the record holds.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Collection
from datetime import date
from pathlib import Path
from typing import Any

import common
import schedule

OUTCOME_TYPES = {"bump", "late_reply", "stale_crossing", "missed_date", "prep_gap", "acted", "ignored"}

Check = Callable[[Any], bool]


class LedgerError(ValueError):
    """A record that cannot go into the named ledger; the message names the ledger or field."""


def _present(value: Any) -> bool:
    return value is not None and value != ""


def _one_of(choices: Collection[str]) -> Check:
    return lambda value: isinstance(value, str) and value in choices


def _is_list(value: Any) -> bool:
    return isinstance(value, list)


def _non_empty_list(value: Any) -> bool:
    return isinstance(value, list) and len(value) > 0


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def _unit_score(value: Any) -> bool:
    return _is_number(value) and 0 <= value <= 1


def _string_or_null(value: Any) -> bool:
    return value is None or isinstance(value, str)


def _any(value: Any) -> bool:
    return True


FIELDS: dict[str, dict[str, Check]] = {
    "outcomes": {"type": _one_of(OUTCOME_TYPES), "raw_ids": _non_empty_list, "surfaced_at": _is_list},
    "labels": {"raw_id": _present, "prediction": _present, "label": _present, "reason": _present},
    "candidates": {
        "raw_id": _present,
        "score": _unit_score,
        "features": _any,
        "reason": _present,
        "decision": _one_of({"surface", "suppress"}),
        "mode": _one_of(schedule.REPORT_MODES),
    },
    "suppressed": {"raw_id": _present, "score": _is_number, "reason": _present},
    "claim_checks": {"claim_id": _present, "supported": _is_bool},
    "surfaced": {"key": _present, "raw_id": _present},
    "report_messages": {
        "message_id": _present,
        "raw_id": _string_or_null,
        "kind": _one_of({"report", "item"}),
    },
    "prep_checks": {"meeting_raw_id": _present, "item": _present, "appeared": _is_bool},
}


def ledger_path(root: Path, ledger: str, record: dict[str, Any], today: date) -> Path:
    """The ledger's file; LedgerError when it would resolve outside the ledger's directory."""
    day = today.isoformat()
    paths = {
        "outcomes": ("state", "outcomes.jsonl"),
        "labels": ("curation/labels", f"{day}.jsonl"),
        "candidates": ("state/candidates", f"{day}-{record.get('mode')}.jsonl"),
        "suppressed": ("state/suppressed", f"{day}.jsonl"),
        "claim_checks": ("state", "claim_checks.jsonl"),
        "surfaced": ("state", "surfaced.jsonl"),
        "report_messages": ("state", "report_messages.jsonl"),
        "prep_checks": ("state", "prep_checks.jsonl"),
    }
    directory, name = paths[ledger]
    path = root / directory / name
    if path.resolve().parent != (root / directory).resolve():
        raise LedgerError(f"{ledger}: path escapes {directory}: {name}")
    return path


def validate(ledger: str, record: Any) -> None:
    """Raise LedgerError naming the unknown ledger or the first missing or invalid field."""
    if ledger not in FIELDS:
        raise LedgerError(f"unknown ledger: {ledger} (known: {', '.join(sorted(FIELDS))})")
    if not isinstance(record, dict):
        raise LedgerError(f"{ledger}: record must be a JSON object")
    for field, check in FIELDS[ledger].items():
        if field not in record:
            raise LedgerError(f"{ledger}: missing field {field}")
        if not check(record[field]):
            raise LedgerError(f"{ledger}: invalid field {field}: {json.dumps(record[field])}")


def stamped(record: dict[str, Any], moment: str) -> dict[str, Any]:
    """The record with `at` set to the given ISO moment unless it already has one."""
    return record if "at" in record else {**record, "at": moment}


def append(root: Path, ledger: str, record: Any) -> Path:
    validate(ledger, record)
    moment = common.now()
    path = ledger_path(root, ledger, record, moment.date())
    common.append_jsonl(path, stamped(record, moment.isoformat()))
    return path


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("ledger")
    parser.add_argument("--file", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        record = json.loads(args.file.read_text())
        path = append(common.repo_root(), args.ledger, record)
    except (OSError, json.JSONDecodeError, LedgerError) as error:
        print(f"append rejected: {error}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
