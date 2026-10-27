import hashlib
import json
import re

import common
import pytest
import raw
from conftest import run_script

SLACK_READ = "mcp__plugin_enterprise-search_slack__slack_read_channel"
HEX64 = re.compile(r"^[0-9a-f]{64}$")


def slack_messages(*messages: dict, channel: str = "C1") -> list[dict]:
    return [{"type": "text", "text": json.dumps({"channel": channel, "messages": list(messages)})}]


def capture_slack(repo, payload, when: str, channel: str = "C1") -> None:
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": SLACK_READ,
        "tool_input": {"channel_id": channel},
        "tool_response": payload,
    }
    result = run_script(repo, "capture.py", stdin=event, env={"JARVIS_NOW": when})
    assert result.returncode == 0


def slack_lines(repo, day: str = "2026-10-26") -> list[dict]:
    return common.read_jsonl(repo / "raw" / "slack" / f"{day}.jsonl")


def decoded(line: dict) -> dict:
    return json.loads(line["payload"][0]["text"])


def test_resolve_returns_the_latest_line_holding_the_channel_and_ts(repo):
    older = slack_messages({"ts": "1729.0001", "text": "first fetch"})
    other_channel = slack_messages({"ts": "1729.0001", "text": "same ts elsewhere"}, channel="C2")
    newer = slack_messages({"ts": "1729.0001", "text": "edited later"})
    capture_slack(repo, older, "2026-10-25T09:00:00+02:00")
    capture_slack(repo, newer, "2026-10-26T09:00:00+01:00")
    capture_slack(repo, other_channel, "2026-10-26T10:00:00+01:00", channel="C2")

    result = run_script(repo, "raw.py", "resolve", "slack:C1:1729.0001")

    assert result.returncode == 0
    line = json.loads(result.stdout)
    assert line["payload"] == newer
    assert line["fetched_at"] == "2026-10-26T09:00:00+01:00"


def test_resolve_finds_a_call_by_its_exact_raw_id(repo):
    capture_slack(repo, "plain text answer", "2026-10-26T09:00:00+01:00")
    (line,) = slack_lines(repo)
    result = run_script(repo, "raw.py", "resolve", line["raw_id"])
    assert result.returncode == 0
    assert json.loads(result.stdout)["payload"] == "plain text answer"


def test_resolve_of_an_unknown_id_exits_one(repo):
    capture_slack(repo, slack_messages({"ts": "1729.0001", "text": "hi"}), "2026-10-26T09:00:00+01:00")
    for unknown in ("slack:C1:1729.9999", "jira:ABC-404", "slack:call:0000000000000000"):
        result = run_script(repo, "raw.py", "resolve", unknown)
        assert result.returncode == 1
        assert result.stdout == ""


def test_tombstone_on_a_json_payload_redacts_the_item_and_keeps_its_siblings(repo):
    payload = slack_messages({"ts": "1729.0001", "text": "hr matter"}, {"ts": "1729.0002", "text": "lunch?"})
    capture_slack(repo, payload, "2026-10-26T09:00:00+01:00")

    result = run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr")

    assert result.returncode == 0
    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    assert entry["raw_id"] == "slack:C1:1729.0001"
    assert entry["reason"] == "hr"
    assert HEX64.match(entry["content_sha256"])
    (line,) = slack_lines(repo)
    assert decoded(line) == {
        "channel": "C1",
        "messages": [
            {"tombstone": True, "content_sha256": entry["content_sha256"], "reason": "hr"},
            {"ts": "1729.0002", "text": "lunch?"},
        ],
    }
    assert "hr matter" not in (repo / "raw" / "slack" / "2026-10-26.jsonl").read_text()


def test_tombstoned_item_resolves_as_tombstoned_not_as_missing(repo):
    capture_slack(repo, slack_messages({"ts": "1729.0001", "text": "hr matter"}), "2026-10-26T09:00:00+01:00")
    run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "personal")

    assert raw.resolve(repo, "slack:C1:1729.0001").status == "tombstoned"
    assert raw.resolve(repo, "slack:C1:1729.0404").status == "not_found"
    assert raw.resolve(repo, "nonsense").status == "invalid"
    assert run_script(repo, "raw.py", "resolve", "slack:C1:1729.0001").returncode == 3


def test_tombstone_on_a_text_payload_tombstones_the_whole_call(repo):
    capture_slack(repo, "C1 1729.0001 <@U2>: hr matter in plain text", "2026-10-26T09:00:00+01:00")

    result = run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr")

    assert result.returncode == 0
    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    (line,) = slack_lines(repo)
    assert line["payload"] == {
        "tombstone": True,
        "scope": "call",
        "content_sha256": entry["content_sha256"],
        "reason": "hr",
    }


def test_recapture_of_a_tombstoned_item_is_written_already_redacted(repo):
    payload = slack_messages({"ts": "1729.0001", "text": "hr matter"}, {"ts": "1729.0002", "text": "lunch?"})
    capture_slack(repo, payload, "2026-10-26T09:00:00+01:00")
    run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr")
    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")

    capture_slack(repo, payload, "2026-10-27T09:00:00+01:00")

    (line,) = slack_lines(repo, "2026-10-27")
    assert decoded(line)["messages"] == [
        {"tombstone": True, "content_sha256": entry["content_sha256"], "reason": "hr"},
        {"ts": "1729.0002", "text": "lunch?"},
    ]
    assert "hr matter" not in (repo / "raw" / "slack" / "2026-10-27.jsonl").read_text()


def test_forget_tombstones_the_item_and_stages_a_retraction(repo):
    payload = slack_messages({"ts": "1729.0001", "text": "wrong fact"}, {"ts": "1729.0002", "text": "lunch?"})
    capture_slack(repo, payload, "2026-10-26T09:00:00+01:00")

    result = run_script(
        repo, "raw.py", "forget", "slack:C1:1729.0001", env={"JARVIS_NOW": "2026-10-26T11:00:00+01:00"}
    )

    assert result.returncode == 0
    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    assert (entry["raw_id"], entry["reason"]) == ("slack:C1:1729.0001", "forget")
    assert common.read_jsonl(repo / "state" / "retractions.jsonl") == [
        {"raw_id": "slack:C1:1729.0001", "at": "2026-10-26T11:00:00+01:00"}
    ]
    (line,) = slack_lines(repo)
    assert decoded(line)["messages"][0] == {
        "tombstone": True,
        "content_sha256": entry["content_sha256"],
        "reason": "forget",
    }


def test_tombstone_of_an_unknown_item_writes_nothing(repo):
    capture_slack(repo, slack_messages({"ts": "1729.0001", "text": "hi"}), "2026-10-26T09:00:00+01:00")
    before = (repo / "raw" / "slack" / "2026-10-26.jsonl").read_text()
    assert run_script(repo, "raw.py", "tombstone", "slack:C1:1729.9999", "--reason", "hr").returncode == 1
    assert not (repo / "state" / "tombstones.jsonl").exists()
    assert (repo / "raw" / "slack" / "2026-10-26.jsonl").read_text() == before


def test_github_item_resolves_by_repo_and_number(repo):
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": "uv run scripts/gh_read.py repos/o/r/pulls"},
        "tool_response": {
            "stdout": json.dumps([{"number": 12, "url": "https://api.github.com/repos/o/r/pulls/12"}]),
            "stderr": "",
        },
    }
    run_script(repo, "capture.py", stdin=event, env={"JARVIS_NOW": "2026-10-26T09:00:00+01:00"})
    assert raw.resolve(repo, "github:o/r#12").status == "found"
    assert raw.resolve(repo, "github:o/other#12").status == "not_found"


def test_slack_item_resolves_when_the_channel_is_only_in_the_call_input(repo):
    line = {
        "raw_id": "slack:call:bbbb",
        "source": "slack",
        "tool": "mcp__plugin_enterprise-search_slack__slack_read_channel",
        "input": {"channel_id": "C7"},
        "payload": {"messages": [{"ts": "1730.0005", "text": "hello"}]},
        "fetched_at": "2026-10-26T08:01:00+01:00",
        "run_id": "r1",
    }
    common.append_jsonl(repo / "raw" / "slack" / "2026-10-26.jsonl", line)
    assert raw.resolve(repo, "slack:C7:1730.0005").status == "found"


def test_a_second_tombstone_keeps_the_first_hash_and_does_not_widen_to_the_call(repo):
    payload = slack_messages(
        {"ts": "1729.0001", "text": "hr matter"}, {"ts": "1729.0002", "text": "re 1729.0001: noted"}
    )
    capture_slack(repo, payload, "2026-10-26T09:00:00+01:00")
    run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr")
    once = (repo / "raw" / "slack" / "2026-10-26.jsonl").read_text()

    result = run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "personal")

    assert result.returncode == 0
    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    assert json.loads(result.stdout) == entry
    assert entry["reason"] == "hr"
    assert (repo / "raw" / "slack" / "2026-10-26.jsonl").read_text() == once
    assert decoded(slack_lines(repo)[0])["messages"][1] == {"ts": "1729.0002", "text": "re 1729.0001: noted"}


def test_tombstone_keeps_unparsable_lines_of_a_raw_file_verbatim(repo):
    capture_slack(repo, slack_messages({"ts": "1729.0001", "text": "hr matter"}), "2026-10-26T09:00:00+01:00")
    path = repo / "raw" / "slack" / "2026-10-26.jsonl"
    path.write_text(path.read_text() + "{torn line\n")

    assert run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr").returncode == 0

    lines = path.read_text().splitlines()
    assert lines[1] == "{torn line"
    assert "hr matter" not in lines[0]


@pytest.mark.parametrize("text", ["[C1 1729.0001] hr matter", "{C1 1729.0001} hr matter"])
def test_tombstone_on_text_that_only_looks_like_json_tombstones_the_whole_call(repo, text):
    capture_slack(repo, [{"type": "text", "text": text}], "2026-10-26T09:00:00+01:00")

    assert run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr").returncode == 0

    (line,) = slack_lines(repo)
    assert line["payload"]["scope"] == "call"
    assert "hr matter" not in (repo / "raw" / "slack" / "2026-10-26.jsonl").read_text()


@pytest.mark.parametrize(
    "raw_id", ["nonsense", "Slack:C1:1729.0001", "slack:C1", "slack::1729.0001", "github:o/r", "github:#12"]
)
def test_malformed_ids_are_invalid_for_resolve_and_tombstone(repo, raw_id):
    assert raw.resolve(repo, raw_id).status == "invalid"
    assert run_script(repo, "raw.py", "resolve", raw_id).returncode == 2
    result = run_script(repo, "raw.py", "tombstone", raw_id, "--reason", "hr")
    assert result.returncode == 2
    assert f"invalid raw id: {raw_id}" in result.stderr
    assert not (repo / "state" / "tombstones.jsonl").exists()


@pytest.mark.parametrize(
    "argv",
    [
        ("tombstone", "slack:C1:1729.0001", "--reason", "boredom"),
        ("tombstone", "slack:C1:1729.0001"),
        ("forget",),
        ("purge", "slack:C1:1729.0001"),
    ],
)
def test_cli_usage_errors_exit_two_and_write_nothing(repo, argv):
    capture_slack(repo, slack_messages({"ts": "1729.0001", "text": "hr matter"}), "2026-10-26T09:00:00+01:00")
    result = run_script(repo, "raw.py", *argv)
    assert result.returncode == 2
    assert "Usage:" in result.stderr
    assert not (repo / "state" / "tombstones.jsonl").exists()


def test_apply_registry_skips_malformed_entries_and_other_sources():
    line = {
        "source": "slack",
        "input": {"channel_id": "C1"},
        "payload": {"messages": [{"ts": "1729.0001", "text": "hr"}]},
    }
    entries = [
        {"content_sha256": "abc"},
        {"raw_id": "nonsense"},
        {"raw_id": "jira:1729.0001", "content_sha256": "abc", "reason": "hr"},
    ]
    assert raw.apply_registry(line, entries) == line
    entries.append({"raw_id": "slack:call:0000", "content_sha256": "abc", "reason": "hr"})
    assert raw.apply_registry(line, entries) == line
    redacted = raw.apply_registry(line, [{"raw_id": "slack:C1:1729.0001"}])
    assert redacted["payload"] == {"messages": [{"tombstone": True, "content_sha256": "", "reason": ""}]}


def append_slack_line(repo, payload, tool_input: dict) -> None:
    line = {
        "raw_id": "slack:call:cccc",
        "source": "slack",
        "tool": "mcp__plugin_enterprise-search_slack__slack_search_public",
        "input": tool_input,
        "payload": payload,
        "fetched_at": "2026-10-26T08:01:00+01:00",
        "run_id": "r1",
    }
    common.append_jsonl(repo / "raw" / "slack" / "2026-10-26.jsonl", line)


def test_tombstone_hash_covers_only_the_matched_item_in_canonical_form(repo):
    item = {"ts": "1729.0001", "text": "hr matter, café"}
    capture_slack(
        repo, slack_messages(item, {"ts": "1729.0002", "text": "lunch?"}), "2026-10-26T09:00:00+01:00"
    )

    run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr")

    canonical = json.dumps([item], ensure_ascii=False, sort_keys=True)
    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    assert entry["content_sha256"] == hashlib.sha256(canonical.encode()).hexdigest()


def test_tombstone_in_a_multi_channel_payload_spares_the_same_ts_in_another_channel(repo):
    results = [
        {"channel": "C1", "messages": [{"ts": "1729.0001", "text": "hr matter"}]},
        {"channel": "C2", "messages": [{"ts": "1729.0001", "text": "keep me"}]},
    ]
    append_slack_line(repo, {"results": results}, {"query": "matter"})

    assert run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr").returncode == 0

    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    marker = {"tombstone": True, "content_sha256": entry["content_sha256"], "reason": "hr"}
    assert slack_lines(repo)[0]["payload"] == {
        "results": [
            {"channel": "C1", "messages": [marker]},
            {"channel": "C2", "messages": [{"ts": "1729.0001", "text": "keep me"}]},
        ]
    }


def test_tombstone_keeps_non_string_values_and_empty_texts_of_siblings(repo):
    sibling = {"ts": "1729.0002", "text": "", "reply_count": 2, "pinned": False, "edited": None, "score": 0.5}
    payload = {
        "messages": [{"ts": "1729.0001", "text": "hr matter"}, sibling, {"ts": "1729.0003", "text": "  "}]
    }
    append_slack_line(repo, payload, {"channel_id": "C1"})

    assert run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr").returncode == 0

    (line,) = slack_lines(repo)
    assert line["payload"]["messages"][1:] == [sibling, {"ts": "1729.0003", "text": "  "}]
    assert line["payload"]["messages"][0]["tombstone"] is True


def test_tombstone_keeps_non_ascii_text_of_rewritten_json_readable(repo):
    payload = slack_messages({"ts": "1729.0001", "text": "hr matter"}, {"ts": "1729.0002", "text": "café?"})
    capture_slack(repo, payload, "2026-10-26T09:00:00+01:00")

    run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr")

    (line,) = slack_lines(repo)
    assert '"text": "café?"' in line["payload"][0]["text"]


def test_tombstone_leaves_raw_files_without_the_item_untouched(repo):
    capture_slack(repo, slack_messages({"ts": "1729.0001", "text": "hr matter"}), "2026-10-26T09:00:00+01:00")
    other = repo / "raw" / "slack" / "2026-10-25.jsonl"
    other.write_text(json.dumps({"source": "slack", "payload": "unrelated"}) + "\n{torn")
    inode = other.stat().st_ino

    assert run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr").returncode == 0

    assert other.read_text() == json.dumps({"source": "slack", "payload": "unrelated"}) + "\n{torn"
    assert other.stat().st_ino == inode


def test_resolve_matches_non_ascii_id_parts(repo):
    line = {
        "raw_id": "notes:call:dddd",
        "source": "notes",
        "tool": "Read",
        "input": {"file_path": "notes/café.md"},
        "payload": "menu",
        "fetched_at": "2026-10-26T08:01:00+01:00",
        "run_id": "r1",
    }
    common.append_jsonl(repo / "raw" / "notes" / "2026-10-26.jsonl", line)
    result = raw.resolve(repo, "notes:notes/café.md")
    assert result.status == "found"
    assert result.line == line


def test_resolve_cli_reports_only_on_the_matching_stream(repo):
    capture_slack(repo, slack_messages({"ts": "1729.0001", "text": "hr matter"}), "2026-10-26T09:00:00+01:00")

    found = run_script(repo, "raw.py", "resolve", "slack:C1:1729.0001")
    assert (found.returncode, found.stderr) == (0, "")

    missing = run_script(repo, "raw.py", "resolve", "slack:C1:1729.0404")
    assert (missing.returncode, missing.stdout, missing.stderr) == (1, "", "not found: slack:C1:1729.0404\n")

    run_script(repo, "raw.py", "tombstone", "slack:C1:1729.0001", "--reason", "hr")
    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    tombstoned = run_script(repo, "raw.py", "resolve", "slack:C1:1729.0001")
    assert (tombstoned.returncode, tombstoned.stdout) == (3, "")
    assert json.loads(tombstoned.stderr) == entry


def test_main_returns_zero_after_a_tombstone(repo):
    capture_slack(repo, slack_messages({"ts": "1729.0001", "text": "hr matter"}), "2026-10-26T09:00:00+01:00")
    assert raw.main(["tombstone", "slack:C1:1729.0001", "--reason", "hr"]) == 0


def append_line(repo, source: str, payload, tool_input: dict, raw_id: str = "call:eeee") -> None:
    line = {
        "raw_id": f"{source}:{raw_id}",
        "source": source,
        "tool": "Bash",
        "input": tool_input,
        "payload": payload,
        "fetched_at": "2026-10-26T08:01:00+01:00",
        "run_id": "r1",
    }
    common.append_jsonl(repo / "raw" / source / "2026-10-26.jsonl", line)


def test_an_id_that_is_only_a_prefix_of_a_captured_id_does_not_resolve(repo):
    append_line(repo, "jira", {"issues": [{"key": "PLAT-123", "summary": "see PLAT-1234 and C12"}]}, {})
    append_line(repo, "slack", {"messages": [{"ts": "1729.00012", "text": "hi"}]}, {"channel_id": "C12"})

    assert raw.resolve(repo, "jira:PLAT-123").status == "found"
    assert raw.resolve(repo, "jira:PLAT-12").status == "not_found"
    assert raw.resolve(repo, "jira:LAT-123").status == "not_found"
    assert raw.resolve(repo, "slack:C12:1729.00012").status == "found"
    assert raw.resolve(repo, "slack:C1:1729.00012").status == "not_found"
    assert raw.resolve(repo, "slack:C12:1729.0001").status == "not_found"


def test_an_id_inside_text_resolves_as_a_whole_token(repo):
    text = "Ana in C1 at 1729.0001: PLAT-12.\nhttps://github.com/o/r/pull/5 (v1.5)"
    append_line(repo, "jira", [{"type": "text", "text": text}], {})
    append_line(repo, "slack", [{"type": "text", "text": text}], {})
    append_line(repo, "github", [{"type": "text", "text": text}], {})

    assert raw.resolve(repo, "jira:PLAT-12").status == "found"
    assert raw.resolve(repo, "jira:PLAT-1").status == "not_found"
    assert raw.resolve(repo, "slack:C1:1729.0001").status == "found"
    assert raw.resolve(repo, "slack:C1:1729.000").status == "not_found"
    assert raw.resolve(repo, "github:o/r#5").status == "found"
    assert raw.resolve(repo, "github:o/r#1").status == "not_found"
    assert raw.resolve(repo, "github:o/#5").status == "not_found"


def github_pulls(*numbers: int, repo_name: str = "example-org/x") -> dict:
    pulls = [
        {"number": n, "title": f"pr {n}", "url": f"https://api.github.com/repos/{repo_name}/pulls/{n}"}
        for n in numbers
    ]
    return {"stdout": json.dumps(pulls), "stderr": ""}


def test_tombstone_of_a_github_item_spares_lines_that_only_hold_its_digits(repo):
    append_line(
        repo, "github", github_pulls(5, 15), {"command": "gh_read.py repos/example-org/x/pulls"}, "call:1"
    )
    append_line(
        repo, "github", github_pulls(15, 25), {"command": "gh_read.py repos/example-org/x/pulls"}, "call:2"
    )
    append_line(
        repo, "github", github_pulls(5, repo_name="example-org/xy"), {"command": "gh_read.py"}, "call:3"
    )
    before = common.read_jsonl(repo / "raw" / "github" / "2026-10-26.jsonl")

    assert raw.tombstone(repo, "github:example-org/x#5", "hr").status == "tombstoned"

    after = common.read_jsonl(repo / "raw" / "github" / "2026-10-26.jsonl")
    assert after[1:] == before[1:]
    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    marker = {"tombstone": True, "content_sha256": entry["content_sha256"], "reason": "hr"}
    assert json.loads(after[0]["payload"]["stdout"]) == [marker, json.loads(github_pulls(15)["stdout"])[0]]
    later = {"source": "github", "input": {}, "payload": github_pulls(15)}
    assert raw.apply_registry(later, [entry]) == later


def test_tombstone_of_a_jira_key_spares_longer_keys_now_and_in_later_captures(repo):
    issues = {"issues": [{"key": "PLAT-12", "summary": "hr"}, {"key": "PLAT-123", "summary": "keep"}]}
    append_line(repo, "jira", issues, {"jql": "project = PLAT"}, "call:1")
    append_line(repo, "jira", {"issues": [{"key": "PLAT-120", "summary": "keep too"}]}, {}, "call:2")

    assert raw.tombstone(repo, "jira:PLAT-12", "hr").status == "tombstoned"

    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    marker = {"tombstone": True, "content_sha256": entry["content_sha256"], "reason": "hr"}
    first, second = common.read_jsonl(repo / "raw" / "jira" / "2026-10-26.jsonl")
    assert first["payload"] == {"issues": [marker, {"key": "PLAT-123", "summary": "keep"}]}
    assert second["payload"] == {"issues": [{"key": "PLAT-120", "summary": "keep too"}]}
    later = {"source": "jira", "input": {}, "payload": [{"type": "text", "text": "PLAT-121 and PLAT-1200"}]}
    assert raw.apply_registry(later, [entry]) == later


def test_tombstone_redacts_every_golden_copy_of_the_item(repo):
    payload = slack_messages({"ts": "1729.0001", "text": "hr matter"}, {"ts": "1729.0002", "text": "lunch?"})
    capture_slack(repo, payload, "2026-10-26T09:00:00+01:00")
    live = repo / "raw" / "slack" / "2026-10-26.jsonl"
    goldens = [repo / "eval" / "golden" / week / "raw" / "slack" / "2026-10-26.jsonl" for week in ("a", "b")]
    for golden in goldens:
        golden.parent.mkdir(parents=True)
        golden.write_text(live.read_text())

    assert run_script(repo, "raw.py", "forget", "slack:C1:1729.0001").returncode == 0

    for golden in goldens:
        assert golden.read_text() == live.read_text()
        assert "hr matter" not in golden.read_text()


def test_tombstone_of_an_item_held_only_in_a_golden_week_redacts_it_there(repo):
    golden = repo / "eval" / "golden" / "2026-W44" / "raw" / "slack" / "2026-10-26.jsonl"
    line = {
        "raw_id": "slack:call:ffff",
        "source": "slack",
        "input": {"channel_id": "C1"},
        "payload": {"messages": [{"ts": "1729.0001", "text": "hr matter"}]},
    }
    common.append_jsonl(golden, line)

    assert raw.tombstone(repo, "slack:C1:1729.0001", "hr").status == "tombstoned"

    assert "hr matter" not in golden.read_text()
    assert raw.resolve(repo, "slack:C1:1729.0001").status == "tombstoned"


def github_issue(number: int, repo_name: str = "example-org/x", **fields) -> dict:
    url = f"https://github.com/{repo_name}/issues/{number}"
    return {"number": number, "title": f"issue {number}", "html_url": url, **fields}


def test_a_github_id_matches_the_item_number_not_any_scalar_of_the_same_value(repo):
    unrelated = github_issue(7, comments=5, body="duplicate of #5, see example-org/x#5")
    append_line(repo, "github", {"stdout": json.dumps([unrelated]), "stderr": ""}, {"command": "gh_read.py"})
    before = (repo / "raw" / "github" / "2026-10-26.jsonl").read_text()

    assert raw.resolve(repo, "github:example-org/x#5").status == "not_found"
    assert raw.tombstone(repo, "github:example-org/x#5", "hr").status == "not_found"
    assert (repo / "raw" / "github" / "2026-10-26.jsonl").read_text() == before
    line = {"source": "github", "input": {}, "payload": {"stdout": json.dumps([unrelated])}}
    entry = {"raw_id": "github:example-org/x#5", "content_sha256": "abc", "reason": "hr"}
    assert raw.apply_registry(line, [entry]) == line


def test_a_github_id_matches_the_item_repo_not_a_mention_of_it(repo):
    other_repo = github_issue(5, repo_name="example-org/y", body="ported from example-org/x")
    append_line(repo, "github", {"stdout": json.dumps([other_repo])}, {"command": "gh_read.py"})

    assert raw.resolve(repo, "github:example-org/x#5").status == "not_found"
    assert raw.resolve(repo, "github:example-org/y#5").status == "found"


def test_tombstone_of_a_github_issue_redacts_only_that_issue(repo):
    issues = [github_issue(5, comments=7), github_issue(7, comments=5)]
    append_line(repo, "github", {"stdout": json.dumps(issues), "stderr": ""}, {"command": "gh_read.py"})

    assert raw.tombstone(repo, "github:example-org/x#5", "hr").status == "tombstoned"

    (line,) = common.read_jsonl(repo / "raw" / "github" / "2026-10-26.jsonl")
    (entry,) = common.read_jsonl(repo / "state" / "tombstones.jsonl")
    marker = {"tombstone": True, "content_sha256": entry["content_sha256"], "reason": "hr"}
    assert json.loads(line["payload"]["stdout"]) == [marker, issues[1]]


def test_a_github_item_without_a_repo_field_takes_the_repo_from_the_call_input(repo):
    issues = [{"number": 5, "title": "hr"}, {"number": 6, "title": "keep"}]
    command = {"command": "gh_read.py issue list -R example-org/x --json number,title"}
    append_line(repo, "github", {"stdout": json.dumps(issues)}, command, "call:1")
    append_line(
        repo, "github", {"stdout": json.dumps(issues)}, {"owner": "example-org", "repo": "x"}, "call:2"
    )
    append_line(
        repo, "github", {"stdout": json.dumps(issues)}, {"command": "gh_read.py -R example-org/xy"}, "call:3"
    )

    assert raw.resolve(repo, "github:example-org/x#5").line["raw_id"] == "github:call:2"
    assert raw.tombstone(repo, "github:example-org/x#5", "hr").status == "tombstoned"

    lines = common.read_jsonl(repo / "raw" / "github" / "2026-10-26.jsonl")
    assert [json.loads(line["payload"]["stdout"])[1] for line in lines] == [issues[1]] * 3
    assert [json.loads(line["payload"]["stdout"])[0] == issues[0] for line in lines] == [False, False, True]


def test_a_github_item_is_found_by_its_nested_repository_and_never_by_a_milestone_number(repo):
    milestone = {"number": 5, "url": "https://api.github.com/repos/example-org/x/milestones/5"}
    searched = [
        {"number": 7, "repository": {"nameWithOwner": "example-org/x"}, "milestone": milestone},
        {"number": 5, "repository": {"nameWithOwner": "example-org/y"}},
    ]
    append_line(
        repo, "github", {"stdout": json.dumps(searched)}, {"command": "gh_read.py search example-org/x"}
    )
    assert raw.resolve(repo, "github:example-org/x#5").status == "not_found"
    assert raw.resolve(repo, "github:example-org/y#5").status == "found"

    comment = {"id": 9, "body": "hr", "issue_url": "https://api.github.com/repos/example-org/x/issues/5"}
    append_line(repo, "github", {"full_name": "example-org/x", "comments": [comment]}, {}, "call:2")
    assert raw.resolve(repo, "github:example-org/x#5").line["raw_id"] == "github:call:2"


def test_a_text_block_that_names_a_github_item_is_tombstoned_whole(repo):
    text = "Release notes: fixed in example-org/x#5, unrelated example-org/x#7."
    append_line(repo, "github", [{"type": "text", "text": text}], {}, "call:1")
    append_line(
        repo,
        "github",
        [{"type": "text", "text": "#5 Fix the build"}],
        {"command": "repos/example-org/x"},
        "call:2",
    )
    append_line(
        repo,
        "github",
        [{"type": "text", "text": "#5 Fix the build"}],
        {"command": "repos/example-org/y"},
        "call:3",
    )

    assert raw.tombstone(repo, "github:example-org/x#5", "hr").status == "tombstoned"

    lines = common.read_jsonl(repo / "raw" / "github" / "2026-10-26.jsonl")
    assert [
        line["payload"].get("scope") if isinstance(line["payload"], dict) else None for line in lines
    ] == [
        "call",
        "call",
        None,
    ]


def test_forget_reaches_a_message_holding_a_unicode_line_separator(repo):
    for separator in ("\u2028", "\u2029", "\x85"):
        payload = [{"type": "text", "text": f"At 1729.0001 Ana wrote: first{separator}second"}]
        capture_slack(repo, payload, "2026-10-26T09:00:00+01:00", channel=f"C{ord(separator)}")
        raw_id = f"slack:C{ord(separator)}:1729.0001"
        assert raw.resolve(repo, raw_id).status == "found"
        assert raw.forget(repo, raw_id).status == "tombstoned"

    text = (repo / "raw" / "slack" / "2026-10-26.jsonl").read_text()
    assert "first" not in text
    assert len(text.split("\n")) == 4


# Id forms come from the adapters in config/sources.yaml


@pytest.mark.parametrize(
    ("template", "rest", "parts"),
    [
        ("slack:<channel>:<ts>", "C1:1729.0001", ("C1", "1729.0001")),
        ("github:<repo>#<number>", "example-org/x#5", ("example-org/x", "5")),
        ("jira:<issue>", "PLAT-1", ("PLAT-1",)),
        ("linear:<team>/<issue>", "ENG/ENG-12", ("ENG", "ENG-12")),
    ],
)
def test_id_pattern_splits_an_id_into_its_template_parts(template, rest, parts):
    assert raw.id_pattern(template).match(rest).groups() == parts


def test_ids_are_parsed_with_the_rendered_adapter_forms(repo):
    assert raw.parse_raw_id("slack:C1:1729.0001", repo).parts == ("C1", "1729.0001")
    github = raw.parse_raw_id("github:example-org/x#5", repo)
    assert (github.parts, github.match) == (("example-org/x", "5"), "github")
    with pytest.raises(ValueError, match="of its source's form"):
        raw.parse_raw_id("slack:C1", repo)


def test_a_source_no_adapter_declares_has_one_part(repo):
    rid = raw.parse_raw_id("linear:ENG-12", repo)
    assert (rid.parts, rid.match) == (("ENG-12",), "parts")


def test_an_edited_capture_map_is_read_again(repo):
    path = repo / "config" / "sources.yaml"
    path.write_text(path.read_text().replace("id: jira:<issue>", "id: jira:<project>/<issue>"))
    assert raw.parse_raw_id("jira:PLAT/PLAT-1", repo).parts == ("PLAT", "PLAT-1")
