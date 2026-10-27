import json
import os
from pathlib import Path

import capture
import common
import pytest
from conftest import REPO, run_script

SLACK_READ = "mcp__plugin_enterprise-search_slack__slack_read_channel"
NOW = "2026-10-26T09:15:00+01:00"


def slack_event(text: str = "deploy is done") -> dict:
    return {
        "session_id": "s1",
        "transcript_path": "/tmp/t.jsonl",
        "cwd": "/tmp",
        "hook_event_name": "PostToolUse",
        "tool_name": SLACK_READ,
        "tool_input": {"channel_id": "C1", "limit": 50},
        "tool_response": [
            {"type": "text", "text": json.dumps({"messages": [{"ts": "1729.0001", "text": text}]})}
        ],
    }


@pytest.fixture
def sources(rendered: Path) -> dict:
    """The capture map rendered from the example profile."""
    return common.load_yaml(rendered / "config" / "sources.yaml")


def test_exact_slack_read_tool_name_maps_to_slack(tmp_path, sources):
    assert capture.source_for(SLACK_READ, {"channel_id": "C1"}, sources, tmp_path) == "slack"


@pytest.mark.parametrize(
    ("tool", "source"),
    [
        ("mcp__claude_ai_Slack__slack_read_channel", "slack"),
        ("mcp__claude_ai_Slack__slack_read_thread", "slack"),
        ("mcp__claude_ai_Atlassian__getJiraIssue", "jira"),
        ("mcp__claude_ai_Atlassian__getConfluencePage", "confluence"),
        ("mcp__claude_ai_Atlassian_Rovo__getJiraIssue", "jira"),
        ("mcp__claude_ai_Google_Calendar__list_events", "calendar"),
        ("mcp__claude_ai_Gmail__get_thread", "gmail"),
    ],
)
def test_headless_connector_read_tool_maps_to_its_source(tmp_path, sources, tool, source):
    assert capture.source_for(tool, {}, sources, tmp_path) == source


def test_unknown_tool_has_no_source(tmp_path, sources):
    config = sources
    assert (
        capture.source_for("mcp__plugin_enterprise-search_slack__slack_send_message", {}, config, tmp_path)
        is None
    )
    assert capture.source_for("Bash", {"command": "gh api repos/o/r/pulls"}, config, tmp_path) is None
    assert capture.source_for("Read", {"file_path": str(tmp_path / "memory/x.md")}, config, tmp_path) is None


def test_gh_read_bash_call_maps_to_github(tmp_path, sources):
    command = {"command": "uv run scripts/gh_read.py repos/o/r/pulls"}
    assert capture.source_for("Bash", command, sources, tmp_path) == "github"


def test_unknown_tool_call_writes_nothing(repo):
    event = {**slack_event(), "tool_name": "mcp__claude_ai_Gmail__send_message"}
    result = run_script(repo, "capture.py", stdin=event, env={"JARVIS_NOW": NOW})
    assert result.returncode == 0
    assert list((repo / "raw").rglob("*.jsonl")) == []
    assert not (repo / "state" / "capture_errors.jsonl").exists()


def test_slack_read_lands_verbatim_in_the_days_slack_file(repo):
    event = slack_event()
    result = run_script(
        repo, "capture.py", stdin=event, env={"JARVIS_NOW": NOW, "JARVIS_RUN_ID": "morning-2026-10-26"}
    )
    assert result.returncode == 0
    lines = common.read_jsonl(repo / "raw" / "slack" / "2026-10-26.jsonl")
    assert len(lines) == 1
    line = lines[0]
    assert line["payload"] == event["tool_response"]
    assert line["input"] == {"channel_id": "C1", "limit": 50}
    assert line["tool"] == SLACK_READ
    assert line["source"] == "slack"
    assert line["run_id"] == "morning-2026-10-26"
    assert line["fetched_at"] == "2026-10-26T09:15:00+01:00"
    assert line["raw_id"].startswith("slack:call:")
    assert len(line["raw_id"]) == len("slack:call:") + 16


def test_fetch_date_is_the_owner_timezone_date(repo):
    run_script(repo, "capture.py", stdin=slack_event(), env={"JARVIS_NOW": "2026-10-26T23:30:00Z"})
    assert (repo / "raw" / "slack" / "2026-10-27.jsonl").exists()


def test_two_identical_calls_are_two_lines(repo):
    for _ in range(2):
        run_script(repo, "capture.py", stdin=slack_event(), env={"JARVIS_NOW": NOW})
    lines = common.read_jsonl(repo / "raw" / "slack" / "2026-10-26.jsonl")
    assert len(lines) == 2
    assert lines[0]["raw_id"] == lines[1]["raw_id"]


def test_read_of_a_note_lands_as_a_notes_line(repo):
    (repo / "notes").mkdir()
    (repo / "notes" / "plan.md").write_text("# Plan\n")
    event = {
        "hook_event_name": "PostToolUse",
        "tool_name": "Read",
        "tool_input": {"file_path": str(repo / "notes" / "plan.md")},
        "tool_response": {
            "type": "text",
            "file": {"filePath": str(repo / "notes" / "plan.md"), "content": "# Plan\n"},
        },
    }
    run_script(repo, "capture.py", stdin=event, env={"JARVIS_NOW": NOW})
    lines = common.read_jsonl(repo / "raw" / "notes" / "2026-10-26.jsonl")
    assert [(line["source"], line["tool"], line["payload"]) for line in lines] == [
        ("notes", "Read", event["tool_response"])
    ]


def test_without_a_run_id_the_run_is_interactive_for_the_day(repo):
    run_script(repo, "capture.py", stdin=slack_event(), env={"JARVIS_NOW": NOW})
    (line,) = common.read_jsonl(repo / "raw" / "slack" / "2026-10-26.jsonl")
    assert line["run_id"] == "interactive-2026-10-26"


def test_malformed_hook_json_exits_zero_and_logs_the_error(repo):
    result = run_script(repo, "capture.py", stdin="{not json", env={"JARVIS_NOW": NOW})
    assert result.returncode == 0
    (error,) = common.read_jsonl(repo / "state" / "capture_errors.jsonl")
    assert error["at"] == "2026-10-26T09:15:00+01:00"
    assert error["tool"] is None
    assert "JSONDecodeError" in error["error"]
    assert list((repo / "raw").rglob("*.jsonl")) == []


def test_capture_sh_runs_capture_with_the_venv_python_and_exits_zero(repo):
    os.symlink(REPO / ".venv", repo / ".venv")
    result = run_script(repo, "capture.sh", stdin=slack_event(), env={"JARVIS_NOW": NOW})
    assert result.returncode == 0
    assert len(common.read_jsonl(repo / "raw" / "slack" / "2026-10-26.jsonl")) == 1
    broken = run_script(repo, "capture.sh", stdin="", env={"JARVIS_NOW": NOW})
    assert broken.returncode == 0


def test_capture_sh_exits_zero_even_without_a_venv(repo):
    assert not Path(repo / ".venv").exists()
    assert run_script(repo, "capture.sh", stdin=slack_event()).returncode == 0


def test_calls_outside_the_capture_map_have_no_source(tmp_path, sources):
    config = sources
    assert capture.source_for("Bash", "uv run scripts/gh_read.py user", config, tmp_path) is None
    assert (
        capture.source_for("Bash", {"command": "uv run scripts/gh_read.py 'user"}, config, tmp_path) is None
    )
    assert capture.source_for("Read", {}, config, tmp_path) is None
    assert capture.source_for("Bash", {"command": "uv run scripts/gh_read.py user"}, {}, tmp_path) is None


def test_relative_read_path_resolves_against_the_repo_root(tmp_path, sources):
    config = sources
    assert capture.source_for("Read", {"file_path": "notes/plan.md"}, config, tmp_path) == "notes"
    assert capture.source_for("Read", {"file_path": "notes/../memory/x.md"}, config, tmp_path) is None


def test_non_object_hook_input_exits_zero_and_logs_the_error(repo):
    result = run_script(repo, "capture.py", stdin="[]", env={"JARVIS_NOW": NOW})
    assert result.returncode == 0
    (error,) = common.read_jsonl(repo / "state" / "capture_errors.jsonl")
    assert error["error"] == "ValueError: hook input is not a JSON object"
    assert list((repo / "raw").rglob("*.jsonl")) == []
