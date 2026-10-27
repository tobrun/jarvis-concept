"""Replay and eval plumbing: run lookup, the copy, the summary, and the CLI's errors (scripts/replay.py)."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import common
import pytest
import replay
import yaml


def git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@e", *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout


@pytest.fixture
def history(tmp_path: Path, rendered: Path) -> Path:
    """A git repo whose one commit holds a finished and a running morning run, the rendered capture map, and
    no reports."""
    root = tmp_path / "history"
    (root / "state").mkdir(parents=True)
    (root / "config").mkdir()
    shutil.copy2(rendered / "config" / "sources.yaml", root / "config" / "sources.yaml")
    finished = {"run_id": "r1", "mode": "morning", "slot_date": "2026-10-26", "finished": "x"}
    running = {"run_id": "r2", "mode": "midday", "slot_date": "2026-10-26", "finished": None}
    common.write_json(root / "state" / "runs.json", {"runs": [finished, running]})
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "morning 2026-10-26 08:04")
    return root


def test_run_commit_is_found_by_its_subject_and_a_missing_one_is_an_error(history):
    head = git(history, "rev-parse", "HEAD").strip()
    assert replay.find_run_commit(history, "2026-10-26", "morning") == head
    with pytest.raises(replay.ReplayError, match="no commit `eod 2026-10-26 <time>` found"):
        replay.find_run_commit(history, "2026-10-26", "eod")


def test_git_failure_outside_a_repo_is_a_replay_error(tmp_path):
    with pytest.raises(replay.ReplayError, match="git log"):
        replay.find_run_commit(tmp_path, "2026-10-26", "morning")


def test_run_record_needs_a_finished_run_of_that_slot(history):
    assert replay.run_record(history, "HEAD", "2026-10-26", "morning")["run_id"] == "r1"
    with pytest.raises(replay.ReplayError, match="no finished midday run for 2026-10-26"):
        replay.run_record(history, "HEAD", "2026-10-26", "midday")


def test_extract_creates_every_directory_even_when_the_commit_has_none(history, tmp_path):
    dest = tmp_path / "copy"
    replay.extract(history, "HEAD", ("memory", "reports"), dest)
    assert (dest / "memory").is_dir() and (dest / "reports").is_dir()
    assert not any(dest.rglob("*.*"))


def test_report_pair_is_empty_for_a_report_missing_on_either_side(history, tmp_path):
    assert replay.report_pair(history, "HEAD", tmp_path, "2026-10-26", "morning") == ("", "")


def test_cited_ids_come_from_the_report_and_the_surfaced_candidates(tmp_path):
    (tmp_path / "reports").mkdir()
    (tmp_path / "reports" / "2026-10-26-morning.md").write_text("Reply to Ana [slack:C1:1729.0001]\n")
    candidates = tmp_path / "state" / "candidates" / "2026-10-26-morning.jsonl"
    common.append_jsonl(candidates, {"raw_id": "slack:C2:1.1", "decision": "surface"})
    common.append_jsonl(candidates, {"raw_id": "slack:C3:1.1", "decision": "suppress"})
    assert replay.cited_ids(tmp_path, "2026-10-26", "morning") == {"slack:C1:1729.0001", "slack:C2:1.1"}


def test_live_guard_without_a_pin_stops_the_replay(repo):
    with pytest.raises(replay.ReplayError, match="guard preflight failed"):
        replay.check_live_guard(repo)


def test_a_missing_claude_binary_is_a_replay_error(repo):
    config_path = repo / "config" / "jarvis.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["claude_bin"] = str(repo / "nope")
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    original = {"run_id": "2026-10-26T0804-morning", "started": "2026-10-26T08:04:00+01:00"}
    now = common.parse_ts(original["started"])
    with pytest.raises(replay.ReplayError, match="could not start"):
        replay.replay_session(repo, repo, original, "2026-10-26", "morning", now)


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (["replay", "2026-10-26", "dream"], "only report runs are replayed, not dream"),
        (["replay", "26-10-2026", "morning"], "Invalid isoformat string"),
        (["eval", "2026-W44"], "no golden week at"),
        (["eval", "--accept"], "no eval/last.json to accept"),
    ],
)
def test_cli_errors_exit_1_with_the_reason(repo, capsys, argv, message):
    assert replay.main(argv) == 1
    assert message in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["replay", "2026-10-26"], ["eval", "2026-W44", "extra"]])
def test_cli_without_a_known_command_prints_the_usage(repo, capsys, argv):
    assert replay.main(argv) == 2
    assert "Usage:" in capsys.readouterr().err


def test_the_copy_applies_the_live_tombstones_to_every_raw_line_and_carries_the_registry(history, tmp_path):
    common.write_json(history / "state" / "guard.sha256", {})
    entry = {"raw_id": "slack:C1:1729.0001", "content_sha256": "abc", "reason": "forget", "at": "x"}
    common.append_jsonl(history / "state" / "tombstones.jsonl", entry)
    golden = tmp_path / "golden-raw"
    line = {
        "raw_id": "slack:call:aaaa",
        "source": "slack",
        "input": {"channel_id": "C1"},
        "payload": {
            "messages": [{"ts": "1729.0001", "text": "hr matter"}, {"ts": "1729.0002", "text": "hi"}]
        },
        "fetched_at": "2026-10-26T08:01:00+01:00",
    }
    common.append_jsonl(golden / "slack" / "2026-10-26.jsonl", line)

    copy = replay.build_copy(history, "HEAD", golden, common.parse_ts("2026-10-26T09:00:00+01:00"))
    try:
        (copied,) = common.read_jsonl(copy / "raw" / "slack" / "2026-10-26.jsonl")
        assert copied["payload"]["messages"] == [
            {"tombstone": True, "content_sha256": "abc", "reason": "forget"},
            {"ts": "1729.0002", "text": "hi"},
        ]
        assert common.read_jsonl(copy / "state" / "tombstones.jsonl") == [entry]
    finally:
        shutil.rmtree(copy, ignore_errors=True)
