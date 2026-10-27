import guard
import pytest

SEND = "mcp__plugin_enterprise-search_slack__slack_send_message"
MODES = ["morning", "midday", "eod", "consolidate", "dream", "interactive", "curate", "replay", "eval"]


def test_morning_rules_carry_the_exact_send_tool_and_report_writes_and_no_jira_write(repo):
    rules = guard.permission_rules(repo, "morning")
    assert SEND in rules
    assert "Edit(reports/**)" in rules and not [r for r in rules if r.startswith("Write(")]
    jira_writes = ("transitionJiraIssue", "editJiraIssue", "createJiraIssue", "addCommentToJiraIssue")
    assert not any(name in rule for rule in rules for name in jira_writes)


def test_replay_rules_have_no_mcp_tools(repo):
    assert not [rule for rule in guard.permission_rules(repo, "replay") if rule.startswith("mcp__")]


@pytest.mark.parametrize("mode", MODES)
def test_guard_pin_pin_is_denied_in_every_mode_but_dev(repo, mode):
    call = {"tool_name": "Bash", "tool_input": {"command": "uv run scripts/guard_pin.py pin"}}
    with pytest.raises(guard.Denied):
        guard.decide(repo, mode, call)


def test_guard_pin_pin_is_allowed_in_dev(repo):
    guard.decide(
        repo, "dev", {"tool_name": "Bash", "tool_input": {"command": "uv run scripts/guard_pin.py pin"}}
    )
