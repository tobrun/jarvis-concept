"""scripts/doctor.py: reading the headless init event and judging the connectors a profile needs."""

from __future__ import annotations

import json

import adapters
import doctor
import pytest
from conftest import REPO


def init_event(servers: dict[str, str], tools: list[str]) -> str:
    event = {
        "type": "system",
        "subtype": "init",
        "tools": ["Read", *tools],
        "mcp_servers": [{"name": name, "status": status} for name, status in servers.items()],
    }
    return "\n".join(["not json", json.dumps({"type": "assistant"}), json.dumps(event)])


@pytest.mark.parametrize(
    ("name", "prefix"),
    [
        ("claude.ai Google Calendar", "claude_ai_Google_Calendar"),
        ("plugin:enterprise-search:slack", "plugin_enterprise-search_slack"),
        ("wispr", "wispr"),
    ],
)
def test_server_names_map_to_their_tool_prefix(name, prefix):
    assert doctor.tool_prefix(name) == prefix


def test_init_event_lists_servers_with_status_and_their_tools():
    stream = init_event(
        {"claude.ai Gmail": "connected", "claude.ai Slack": "pending"},
        ["mcp__claude_ai_Gmail__get_thread", "mcp__claude_ai_Gmail__send_message"],
    )
    assert doctor.parse_init(stream) == {
        "servers": [
            {
                "name": "claude.ai Gmail",
                "prefix": "claude_ai_Gmail",
                "status": "connected",
                "tools": ["get_thread", "send_message"],
            },
            {"name": "claude.ai Slack", "prefix": "claude_ai_Slack", "status": "pending", "tools": []},
        ]
    }


def test_a_stream_without_init_is_an_error():
    with pytest.raises(RuntimeError, match="no init event"):
        doctor.parse_init('{"type": "result"}')


def test_merge_keeps_the_best_status_and_every_tool():
    first = {"servers": [{"name": "s", "prefix": "s", "status": "pending", "tools": []}]}
    second = {"servers": [{"name": "s", "prefix": "s", "status": "connected", "tools": ["a"]}]}
    assert doctor.merge(first, second)["servers"] == [
        {"name": "s", "prefix": "s", "status": "connected", "tools": ["a"]}
    ]


def judge(seen: dict) -> list[tuple[str, str]]:
    profile = {
        "principal": {"name": "Ada Example", "role": "r", "emails": ["ada@example.com"]},
        "timezone": "UTC",
        "sources": {"gmail": {}},
        "delivery": {"adapter": "gmail-to-self"},
    }
    report = doctor.Report()
    doctor.check_servers(adapters.load(REPO, profile), seen, report)
    return report.lines


def server(status: str, tools: list[str]) -> dict:
    return {
        "servers": [
            {"name": "claude.ai Gmail", "prefix": "claude_ai_Gmail", "status": status, "tools": tools}
        ]
    }


def test_connected_server_with_every_tool_passes():
    tools = ["get_thread", "get_message", "search_threads", "list_labels", "send_message"]
    assert [level for level, _ in judge(server("connected", tools))] == ["ok", "ok"]


def test_missing_server_or_one_needing_auth_fails_and_a_missing_tool_warns():
    assert judge({"servers": []})[0][0] == "FAIL"
    level, text = judge(server("needs-auth", []))[0]
    assert level == "FAIL" and "/mcp" in text
    level, text = judge(server("connected", ["get_thread"]))[0]
    assert level == "warn" and "lacks get_message" in text
