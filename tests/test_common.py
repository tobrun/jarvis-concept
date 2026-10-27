import multiprocessing
from datetime import date, datetime, timedelta

import common


def test_jarvis_now_fixes_the_clock(monkeypatch):
    monkeypatch.setenv("JARVIS_NOW", "2026-11-02T09:00:00+01:00")
    assert common.now() == datetime.fromisoformat("2026-11-02T09:00:00+01:00")


def test_jarvis_now_without_offset_is_owner_time(monkeypatch):
    monkeypatch.setenv("JARVIS_NOW", "2026-07-01T09:00:00")
    assert common.now().utcoffset() == timedelta(hours=2)
    assert common.now().hour == 9


def test_thresholds_are_read_as_numbers(tmp_path):
    path = tmp_path / "thresholds.md"
    path.write_text("# Thresholds\n\nProse line.\nsurface_threshold: 0.6\nstale_pr_review_days: 3\n")
    values = common.read_kv(path)
    assert values == {"surface_threshold": 0.6, "stale_pr_review_days": 3}


def test_malformed_threshold_line_is_skipped_and_others_read(tmp_path):
    path = tmp_path / "thresholds.md"
    path.write_text("surface_threshold 0.6\nsnooze_default_business_days: 3\n")
    assert common.read_kv(path) == {"snooze_default_business_days": 3}


def test_holiday_line_with_a_trailing_name_parses_the_date(tmp_path):
    path = tmp_path / "holidays.md"
    path.write_text("# 2026\n\n- 2026-11-11 Armistice Day\n2026-12-25 Christmas\n")
    assert common.read_holidays(path) == {date(2026, 11, 11), date(2026, 12, 25)}


def _append_many(path: str, writer: int) -> None:
    from pathlib import Path

    for index in range(200):
        common.append_jsonl(Path(path), {"writer": writer, "index": index, "pad": "x" * 500})


def test_concurrent_appenders_never_interleave_lines(tmp_path):
    path = tmp_path / "ledger.jsonl"
    workers = [multiprocessing.Process(target=_append_many, args=(str(path), n)) for n in (1, 2)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    records = common.read_jsonl(path)
    assert len(records) == 400
    assert sum(1 for r in records if r["writer"] == 1) == 200


def test_append_after_the_file_is_replaced_lands_in_the_new_file(tmp_path):
    path = tmp_path / "raw.jsonl"
    common.append_jsonl(path, {"n": 1})
    replacement = tmp_path / "raw.jsonl.new"
    replacement.write_text('{"n": 1, "redacted": true}\n')
    replacement.replace(path)
    common.append_jsonl(path, {"n": 2})
    assert common.read_jsonl(path) == [{"n": 1, "redacted": True}, {"n": 2}]


def test_read_jsonl_skips_blank_and_corrupt_lines_and_reads_a_missing_file_as_empty(tmp_path, capsys):
    path = tmp_path / "ledger.jsonl"
    assert common.read_jsonl(path) == []
    path.write_text('{"n": 1}\n\n   \n{"n": 2, "trunc\n{"n": 3}\n')
    assert common.read_jsonl(path) == [{"n": 1}, {"n": 3}]
    assert "skipping unreadable line" in capsys.readouterr().err


def test_read_jsonl_keeps_a_record_holding_a_unicode_line_separator(tmp_path, capsys):
    path = tmp_path / "ledger.jsonl"
    records = [
        {"text": f"first{separator}second"} for separator in ("\u2028", "\u2029", "\x85", "\x0b\x0c\x1c")
    ]
    for record in records:
        common.append_jsonl(path, record)
    assert common.read_jsonl(path) == records
    assert common.jsonl_lines(path.read_text()) == path.read_text().removesuffix("\n").split("\n")
    assert common.jsonl_lines("") == []
    assert capsys.readouterr().err == ""
