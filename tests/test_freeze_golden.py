import json
import os
import subprocess

import common
from conftest import run_script


def git(root, *args, date=None):
    env = {**os.environ}
    if date:
        env.update(GIT_AUTHOR_DATE=date, GIT_COMMITTER_DATE=date)
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, env=env, check=True
    )
    return result.stdout.strip()


def raw_line(raw_id):
    return json.dumps({"raw_id": raw_id, "source": "slack", "payload": {}}) + "\n"


def test_freezing_a_week_copies_only_its_raw_dates_and_records_the_start_commit(repo):
    git(repo, "init", "-q")
    git(repo, "config", "user.email", "test@example.com")
    git(repo, "config", "user.name", "Test")
    (repo / "notes.txt").write_text("one\n")
    git(repo, "add", "notes.txt")
    # Amsterdam is on CET (+01:00) from 2026-10-25, so W44 starts at 2026-10-26T00:00+01:00.
    git(repo, "commit", "-q", "-m", "before the week", date="2026-10-25T23:30:00+01:00")
    before_week = git(repo, "rev-parse", "HEAD")
    (repo / "notes.txt").write_text("two\n")
    git(repo, "commit", "-q", "-am", "inside the week", date="2026-10-26T00:30:00+01:00")

    slack = repo / "raw" / "slack"
    slack.mkdir()
    for day, raw_id in [
        ("2026-10-25", "slack:w43"),
        ("2026-10-26", "slack:mon"),
        ("2026-11-01", "slack:sun"),
        ("2026-11-02", "slack:w45"),
    ]:
        (slack / f"{day}.jsonl").write_text(raw_line(raw_id))
    labels = repo / "curation" / "labels"
    labels.mkdir()
    (labels / "2026-10-28.jsonl").write_text('{"raw_id": "slack:mon", "label": "should_surface"}\n')
    (labels / "2026-11-03.jsonl").write_text('{"raw_id": "slack:w45", "label": "noise"}\n')
    (repo / "state" / "outcomes.jsonl").write_text(
        "".join(
            json.dumps(o) + "\n"
            for o in [
                {"type": "bump", "raw_ids": ["slack:mon"], "at": "2026-11-20T10:00:00+01:00"},
                {"type": "acted", "raw_ids": ["gmail:x"], "at": "2026-11-06T10:00:00+01:00"},
                {"type": "ignored", "raw_ids": ["gmail:y"], "at": "2026-11-10T10:00:00+01:00"},
                {"type": "ignored", "raw_ids": ["gmail:z"], "at": "2026-10-25T10:00:00+01:00"},
            ]
        )
    )

    result = run_script(repo, "freeze_golden.py", "2026-W44", env={"JARVIS_NOW": "2026-11-09T09:00:00+01:00"})

    assert result.returncode == 0, result.stderr
    golden = repo / "eval" / "golden" / "2026-W44"
    copied = sorted(str(p.relative_to(golden)) for p in golden.rglob("*") if p.is_file())
    assert copied == [
        "labels/2026-10-28.jsonl",
        "manifest.json",
        "outcomes.jsonl",
        "raw/slack/2026-10-26.jsonl",
        "raw/slack/2026-11-01.jsonl",
    ]
    manifest = json.loads((golden / "manifest.json").read_text())
    assert manifest == {
        "week": "2026-W44",
        "start_commit": before_week,
        "frozen_at": "2026-11-09T09:00:00+01:00",
    }
    kept = [json.loads(line)["raw_ids"][0] for line in (golden / "outcomes.jsonl").read_text().splitlines()]
    assert kept == ["slack:mon", "gmail:x"]


def test_freezing_outside_git_warns_of_no_start_commit_and_a_second_freeze_is_refused(repo):
    env = {"JARVIS_NOW": "2026-11-09T09:00:00+01:00"}

    first = run_script(repo, "freeze_golden.py", "2026-W44", env=env)
    second = run_script(repo, "freeze_golden.py", "2026-W44", env=env)

    assert first.returncode == 0, first.stderr
    assert "warning: no commit before the week starts" in first.stderr
    assert json.loads(first.stdout)["start_commit"] is None
    golden = repo / "eval" / "golden" / "2026-W44"
    assert (golden / "outcomes.jsonl").read_text() == ""
    assert second.returncode == 1
    assert "already exists; a golden week is never overwritten" in second.stderr


def test_freeze_cli_rejects_a_malformed_week_and_a_missing_argument(repo):
    malformed = run_script(repo, "freeze_golden.py", "2026-44")
    missing = run_script(repo, "freeze_golden.py")

    assert malformed.returncode == 2
    assert "not an ISO week: 2026-44 (expected YYYY-Www)" in malformed.stderr
    assert missing.returncode == 2
    assert "Usage:" in missing.stderr
    assert not (repo / "eval" / "golden").exists()


def hr_line():
    payload = {"messages": [{"ts": "1729.0001", "text": "hr matter"}, {"ts": "1729.0002", "text": "lunch?"}]}
    return {"raw_id": "slack:call:aaaa", "source": "slack", "input": {"channel_id": "C1"}, "payload": payload}


def test_forgetting_an_item_after_the_freeze_redacts_the_golden_copy(repo):
    common.append_jsonl(repo / "raw" / "slack" / "2026-10-26.jsonl", hr_line())
    env = {"JARVIS_NOW": "2026-11-09T09:00:00+01:00"}
    assert run_script(repo, "freeze_golden.py", "2026-W44", env=env).returncode == 0
    golden = repo / "eval" / "golden" / "2026-W44" / "raw" / "slack" / "2026-10-26.jsonl"
    assert "hr matter" in golden.read_text()

    assert run_script(repo, "raw.py", "forget", "slack:C1:1729.0001", env=env).returncode == 0

    assert "hr matter" not in golden.read_text()
    assert golden.read_text() == (repo / "raw" / "slack" / "2026-10-26.jsonl").read_text()
    assert "lunch?" in golden.read_text()


def test_freezing_applies_the_tombstone_registry_to_every_copied_line(repo):
    raw_file = repo / "raw" / "slack" / "2026-10-26.jsonl"
    common.append_jsonl(raw_file, hr_line())
    raw_file.write_text(raw_file.read_text() + "{torn\n")
    entry = {"raw_id": "slack:C1:1729.0001", "content_sha256": "abc", "reason": "hr", "at": "x"}
    common.append_jsonl(repo / "state" / "tombstones.jsonl", entry)

    result = run_script(repo, "freeze_golden.py", "2026-W44", env={"JARVIS_NOW": "2026-11-09T09:00:00+01:00"})

    assert result.returncode == 0, result.stderr
    golden = repo / "eval" / "golden" / "2026-W44" / "raw" / "slack" / "2026-10-26.jsonl"
    first, torn = golden.read_text().splitlines()
    assert json.loads(first)["payload"]["messages"] == [
        {"tombstone": True, "content_sha256": "abc", "reason": "hr"},
        {"ts": "1729.0002", "text": "lunch?"},
    ]
    assert torn == "{torn"
