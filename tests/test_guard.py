import io
import json
import os
from datetime import timedelta

import common
import guard
import pytest
from conftest import REPO, run_script

SEND = "mcp__plugin_enterprise-search_slack__slack_send_message"
HEADLESS_SEND = "mcp__claude_ai_Slack__slack_send_message"
SELF_DM = "U0EXAMPLE01"

REPORT = """Jarvis, morning, 2026-10-26 08:04

Today
  Budget reply to Ana is due today. Send it? [slack:C1:1729.0001]
Slipping: none
Waiting on you: none
Waiting on others: none
FYI: none

Sources: slack 1/1. Candidates 1, surfaced 1, suppressed 0. Denied calls 0.
"""


@pytest.fixture
def root(repo):
    line = {
        "raw_id": "slack:call:aaaa",
        "source": "slack",
        "tool": "mcp__plugin_enterprise-search_slack__slack_read_channel",
        "input": {"channel_id": "C1"},
        "payload": {"messages": [{"ts": "1729.0001", "text": "budget?"}]},
        "fetched_at": "2026-10-26T08:01:00+01:00",
        "run_id": "2026-10-26T0800-morning",
    }
    common.append_jsonl(repo / "raw" / "slack" / "2026-10-26.jsonl", line)
    return repo


def call(tool, **tool_input):
    return {"tool_name": tool, "tool_input": tool_input}


def denial(root, mode, payload):
    with pytest.raises(guard.Denied) as caught:
        guard.decide(root, mode, payload)
    return str(caught.value)


def post(message=REPORT, **extra):
    return call(SEND, channel_id=SELF_DM, message=message, **extra)


def test_morning_self_dm_post_with_a_valid_report_is_allowed(root):
    guard.decide(root, "morning", post())


def test_post_to_another_channel_is_denied_naming_the_channel(root):
    reason = denial(root, "morning", call(SEND, channel_id="C0OTHER", message=REPORT))
    assert "C0OTHER" in reason


def test_headless_slack_connector_post_to_the_self_dm_is_allowed(root):
    guard.decide(root, "morning", call(HEADLESS_SEND, channel_id=SELF_DM, message=REPORT))


def test_headless_slack_connector_post_to_another_channel_is_denied(root):
    reason = denial(root, "morning", call(HEADLESS_SEND, channel_id="C0OTHER", message=REPORT))
    assert "C0OTHER" in reason


def test_headless_slack_connector_post_with_an_extra_key_is_denied(root):
    reason = denial(root, "morning", call(HEADLESS_SEND, channel_id=SELF_DM, message=REPORT, draft_id="d1"))
    assert "draft_id" in reason


def test_headless_slack_connector_post_failing_the_linter_is_denied(root):
    message = REPORT.replace("Send it?", "Send it!")
    reason = denial(root, "morning", call(HEADLESS_SEND, channel_id=SELF_DM, message=message))
    assert "report check failed" in reason


def test_same_tool_name_from_an_unlisted_slack_server_is_denied(root):
    reason = denial(root, "morning", call("mcp__other_slack__slack_send_message", channel_id=SELF_DM))
    assert "not allowed in morning" in reason


def test_every_post_tool_is_constrained(rendered):
    policy = guard.load_policy(rendered)
    assert policy["groups"]["post"]
    assert set(policy["groups"]["post"]) <= set(policy["constrained"])


def test_every_captured_tool_is_an_allowed_source_and_every_source_is_captured(rendered):
    policy = guard.load_policy(rendered)
    captured = {
        tool
        for spec in common.load_yaml(rendered / "config" / "sources.yaml")["sources"].values()
        for tool in spec.get("tools", [])
    }
    assert captured == set(policy["groups"]["sources"])


def test_self_dm_post_with_reply_broadcast_is_denied_naming_the_key(root):
    reason = denial(root, "morning", post(thread_ts="1.2", reply_broadcast=True))
    assert "reply_broadcast" in reason


def test_top_level_post_failing_the_linter_is_denied_with_the_problem(root):
    reason = denial(root, "morning", post(REPORT.replace("Send it?", "Send it!")))
    assert "report check failed" in reason
    assert "exclamation" in reason


def test_post_with_six_lint_problems_names_only_the_first_five(root):
    message = REPORT.replace("Send it?", "Send it!").replace(": none", ": none!").replace("0.\n", "0!\n")
    reason = denial(root, "morning", post(message))
    assert reason == (
        "report check failed: line 4: exclamation mark; line 5: exclamation mark; "
        "line 6: exclamation mark; line 7: exclamation mark; line 8: exclamation mark"
    )


def test_thread_reply_to_the_self_dm_is_allowed(root):
    guard.decide(root, "morning", post("Done, budget reply sent. [slack:C1:1729.0001]", thread_ts="1729.9"))


def test_top_level_post_while_ooo_covers_now_is_denied(root):
    until = (common.now() + timedelta(days=2)).isoformat()
    common.write_json(root / "state" / "ooo.json", {"raw_id": "calendar:evt1", "until": until})
    reason = denial(root, "morning", post())
    assert "out of office" in reason
    assert "calendar:evt1" in reason


def test_top_level_post_in_an_unsent_run_is_denied(root, monkeypatch):
    monkeypatch.setenv("JARVIS_UNSENT", "1")
    assert "unsent" in denial(root, "eod", post())


@pytest.mark.parametrize("mode", ["morning", "midday", "eod", "consolidate", "dream", "interactive", "dev"])
def test_jira_transition_is_denied_in_every_mode(root, mode):
    reason = denial(
        root, mode, call("mcp__claude_ai_Atlassian_Rovo__transitionJiraIssue", issueIdOrKey="X-1")
    )
    assert "not allowed" in reason


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__claude_ai_Google_Calendar__create_event",
        "mcp__some_new_server__do_things",
        "WebFetch",
        "WebSearch",
    ],
)
def test_write_capable_unknown_and_network_tools_are_denied(root, tool):
    assert "not allowed" in denial(root, "morning", call(tool))


@pytest.mark.parametrize(
    ("tool", "key"),
    [
        ("Write", "file_path"),
        ("Edit", "file_path"),
        ("MultiEdit", "file_path"),
        ("NotebookEdit", "notebook_path"),
    ],
)
def test_every_edit_tool_to_raw_is_denied(root, tool, key):
    assert "protected" in denial(root, "morning", call(tool, **{key: "raw/slack/x.jsonl"}))


def test_write_to_raw_in_another_case_is_denied(root):
    assert "protected" in denial(root, "morning", call("Write", file_path="RAW/x"))


def test_write_to_a_git_hook_is_denied(root):
    assert "protected" in denial(root, "morning", call("Write", file_path=".git/hooks/pre-commit"))


def test_write_of_the_morning_report_is_allowed(root):
    guard.decide(root, "morning", call("Write", file_path="reports/2026-10-26-morning.md"))


def test_absolute_path_inside_the_repo_is_checked_like_a_relative_one(root):
    guard.decide(root, "morning", call("Write", file_path=str(root / "reports" / "x.md")))


def test_write_to_dreams_in_morning_is_denied(root):
    assert "outside the morning write scope" in denial(
        root, "morning", call("Write", file_path="memory/dreams/x.md")
    )


def test_dot_dot_escape_is_denied(root):
    assert "outside the repo" in denial(root, "morning", call("Write", file_path="reports/../../x.md"))


def test_absolute_path_outside_the_repo_is_denied(root, tmp_path):
    outside = tmp_path / "elsewhere.md"
    assert "outside the repo" in denial(root, "morning", call("Write", file_path=str(outside)))


def test_symlink_out_of_the_write_scope_is_denied(root):
    (root / "reports").rmdir()
    (root / "reports").symlink_to(root / "raw")
    assert "protected" in denial(root, "morning", call("Write", file_path="reports/x.jsonl"))


def test_edit_without_a_path_is_denied(root):
    assert "without a path" in denial(root, "morning", call("Write"))


def test_dev_mode_may_write_a_temp_file_outside_the_repo(root, tmp_path):
    guard.decide(root, "dev", call("Write", file_path=str(tmp_path / "scratch.md")))


def test_bash_coverage_check_in_morning_is_allowed(root):
    guard.decide(root, "morning", call("Bash", command="uv run scripts/source_coverage.py check"))


@pytest.mark.parametrize(
    "command",
    [
        "uv run scripts/source_coverage.py check; rm -rf memory",
        "uv run scripts/source_coverage.py check && rm -rf memory",
        "uv run scripts/source_coverage.py check | sh",
        "uv run scripts/source_coverage.py check > memory/MEMORY.md",
    ],
)
def test_bash_with_a_shell_operator_is_denied(root, command):
    assert "shell operator" in denial(root, "morning", call("Bash", command=command))


def test_bash_with_an_unspaced_shell_operator_is_denied(root):
    command = "uv run scripts/loops.py stale;rm -rf memory"
    assert "shell operator ';'" in denial(root, "morning", call("Bash", command=command))


def test_bash_with_an_operator_after_a_mid_word_hash_is_denied(root):
    # sh reads a mid-word # as a literal, so the ; still ends the command and echo runs next.
    command = "uv run scripts/append.py x#; echo marker"
    assert "shell operator ';'" in denial(root, "morning", call("Bash", command=command))


def test_bash_argv_keeps_a_mid_word_hash_like_a_shell():
    assert guard.bash_argv("uv run scripts/append.py x#y") == ["uv", "run", "scripts/append.py", "x#y"]


def test_bash_with_a_word_initial_hash_is_denied(root):
    command = "uv run scripts/append.py outcomes #note"
    assert "'#' at the start of a word" in denial(root, "morning", call("Bash", command=command))


def test_bash_with_a_word_initial_equals_is_denied(root):
    # zsh expands =name to the path of the program name.
    command = "uv run scripts/append.py outcomes =python3"
    assert "'=' at the start of a word" in denial(root, "morning", call("Bash", command=command))


@pytest.mark.parametrize(
    ("command", "char"),
    [
        ("uv run scripts/append.py outcomes *", "*"),
        ("uv run scripts/append.py outcomes memory/?.md", "?"),
        ("uv run scripts/append.py outcomes memory/[ab].md", "["),
        ("uv run scripts/append.py outcomes {a,b}", "{"),
        ("uv run scripts/append.py outcomes ~/notes", "~"),
        ("uv run scripts/append.py outcomes !x", "!"),
        ("uv run scripts/append.py outcomes a^b", "^"),
        # shlex splits on a carriage return; sh keeps it inside the word, so the shell runs `uv\rrun`.
        ("uv\rrun scripts/append.py outcomes", "\r"),
        # Outside quotes only plain ASCII word characters pass, so no shell or locale can reread them.
        ("uv run scripts/append.py outcomes caf\u00e9", "\u00e9"),
    ],
)
def test_bash_with_an_unquoted_character_the_shell_may_reinterpret_is_denied(root, command, char):
    assert f"unquoted {char!r} is not allowed" in denial(root, "morning", call("Bash", command=command))


def test_bash_argv_reads_quoted_specials_and_quotes_inside_words_like_a_shell(root):
    command = "uv run scripts/app\"end\".py outcomes '#note' \"*\" 'x;y' ''#z '='a a'~'"
    guard.decide(root, "morning", call("Bash", command=command))
    assert guard.bash_argv(command) == [
        "uv",
        "run",
        "scripts/append.py",
        "outcomes",
        "#note",
        "*",
        "x;y",
        "#z",
        "=a",
        "a~",
    ]


def test_bash_argv_splits_on_whitespace_only_like_a_shell():
    assert guard.bash_argv("gh api repos/x:y --jq .a,b@c") == ["gh", "api", "repos/x:y", "--jq", ".a,b@c"]


def test_bash_with_command_substitution_in_double_quotes_is_denied(root):
    command = 'uv run scripts/append.py outcomes "$(curl x)"'
    assert "expansion" in denial(root, "morning", call("Bash", command=command))


def test_bash_with_a_dollar_inside_single_quotes_is_allowed(root):
    guard.decide(
        root, "morning", call("Bash", command='uv run scripts/append.py outcomes \'{"cost": "$5"}\'')
    )


@pytest.mark.parametrize(
    "command",
    [
        # An escaped quote keeps the double-quoted string open, so the shell runs the substitution.
        'uv run scripts/append.py outcomes "\\"\'$(curl x)\'\\""',
        'uv run scripts/append.py outcomes "a\\"b\'`curl x`\'"',
        # An escaped single quote is a literal, so what looks single-quoted is bare to the shell.
        "uv run scripts/append.py outcomes \\'$(curl x)\\'",
        "uv run scripts/append.py outcomes \\'`curl x`\\'",
    ],
)
def test_bash_with_an_escape_that_exposes_expansion_is_denied(root, command):
    assert "backslash" in denial(root, "morning", call("Bash", command=command))


def test_bash_with_an_escaped_quote_that_exposes_a_newline_is_denied(root):
    command = "uv run scripts/append.py outcomes \\'\nrm -rf memory \\'"
    assert "backslash" in denial(root, "morning", call("Bash", command=command))


def test_bash_with_a_backslash_before_a_dollar_is_denied(root):
    command = "uv run scripts/append.py outcomes \\$HOME"
    assert "backslash" in denial(root, "morning", call("Bash", command=command))


def test_bash_with_a_line_continuation_is_denied(root):
    command = "uv run scripts/loops.py \\\nstale"
    assert "backslash" in denial(root, "morning", call("Bash", command=command))


def test_bash_with_a_backslash_inside_single_quotes_is_allowed(root):
    command = 'uv run scripts/append.py outcomes \'{"note": "a\\\\nb"}\''
    guard.decide(root, "morning", call("Bash", command=command))


def test_multi_line_bash_is_denied(root):
    assert "multi-line" in denial(
        root, "morning", call("Bash", command="uv run scripts/loops.py stale\nrm x")
    )


def test_bash_with_a_backtick_is_denied(root):
    command = "uv run scripts/append.py outcomes `curl x`"
    assert "expansion" in denial(root, "morning", call("Bash", command=command))


def test_bash_with_an_unclosed_quote_is_denied(root):
    command = 'uv run scripts/loops.py "stale'
    assert "unparseable" in denial(root, "morning", call("Bash", command=command))


@pytest.mark.parametrize("command", ["", "   "])
def test_empty_bash_command_is_denied(root, command):
    assert "empty command" in denial(root, "morning", call("Bash", command=command))


def test_raw_gh_is_denied(root):
    assert "command not allowed" in denial(root, "morning", call("Bash", command="gh api repos/x"))


def test_denied_command_reason_quotes_its_first_four_words(root):
    reason = denial(root, "morning", call("Bash", command="gh pr merge 12 --admin"))
    assert reason == "command not allowed in morning: gh pr merge 12"


def test_guard_pin_is_denied_outside_dev(root):
    command = "uv run scripts/guard_pin.py pin"
    assert "command not allowed" in denial(root, "interactive", call("Bash", command=command))


def test_replay_cannot_read_slack(root):
    tool = "mcp__plugin_enterprise-search_slack__slack_read_channel"
    assert "not allowed in replay" in denial(root, "replay", call(tool, channel_id="C1"))


def test_unset_mode_denies_everything(root):
    assert "JARVIS_MODE is unset" in denial(root, None, call("Read", file_path="JARVIS.md"))


def test_unknown_mode_denies_everything(root):
    assert "unknown mode" in denial(root, "weekend", call("Read", file_path="JARVIS.md"))


def test_dev_mode_writes_scripts_but_cannot_post(root):
    guard.decide(root, "dev", call("Write", file_path="scripts/x.py"))
    assert "not allowed in dev" in denial(root, "dev", post())


def test_dev_mode_still_cannot_write_raw_or_ledgers(root):
    assert "protected" in denial(root, "dev", call("Write", file_path="raw/slack/x.jsonl"))
    assert "protected" in denial(root, "dev", call("Edit", file_path="state/outcomes.jsonl"))


def test_permission_rules_for_morning_scope_edits_and_bash(root):
    rules = guard.permission_rules(root, "morning")
    assert "Edit(reports/**)" in rules
    assert rules.count("Edit(reports/**)") == 1
    assert not any(rule.startswith(("Write(", "MultiEdit(", "NotebookEdit(")) for rule in rules)
    assert "Bash(uv run scripts/source_coverage.py:*)" in rules
    assert SEND in rules
    assert not any(rule.startswith("Edit(memory/dreams") for rule in rules)
    assert "WebFetch" not in rules


def test_permission_rules_for_dev_allow_any_bash(root):
    rules = guard.permission_rules(root, "dev")
    assert "Bash" in rules
    assert not any(rule.startswith("Bash(") for rule in rules)


def test_rules_cli_prints_one_rule_per_line(root):
    result = run_script(root, "guard.py", "rules", "dream")
    assert result.returncode == 0
    assert "Edit(memory/dreams/**)" in result.stdout.splitlines()


def hook(monkeypatch, payload, mode="morning"):
    """Run guard.main in hook mode in process, with the call on stdin."""
    monkeypatch.setenv("JARVIS_MODE", mode)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(payload if isinstance(payload, str) else json.dumps(payload))
    )
    return guard.main([])


def test_main_rules_prints_one_rule_per_line(root, capsys):
    assert guard.main(["rules", "dream"]) == 0
    assert "Edit(memory/dreams/**)" in capsys.readouterr().out.splitlines()


def test_main_allows_a_permitted_call(root, monkeypatch):
    assert hook(monkeypatch, call("Read", file_path="JARVIS.md")) == 0
    assert not (root / "state" / "denied_calls.jsonl").exists()


def test_main_denies_and_logs_a_forbidden_call(root, monkeypatch, capsys):
    monkeypatch.setenv("JARVIS_RUN_ID", "2026-10-26T0800-morning")
    assert hook(monkeypatch, call("WebFetch", url="https://example.com")) == 2
    assert "Jarvis guard denied this call: WebFetch is not allowed in morning" in capsys.readouterr().err
    [line] = common.read_jsonl(root / "state" / "denied_calls.jsonl")
    assert (line["tool"], line["mode"], line["run_id"]) == ("WebFetch", "morning", "2026-10-26T0800-morning")


def test_main_with_an_unknown_two_word_argv_still_judges_the_call(root, monkeypatch):
    monkeypatch.setenv("JARVIS_MODE", "morning")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(call("WebFetch", url="https://example.com"))))
    assert guard.main(["list", "dev"]) == 2


def test_denial_digest_ignores_input_key_order(root):
    guard.log_denial(root, "morning", {"tool_name": "Bash", "tool_input": {"a": 1, "b": 2}}, "x")
    guard.log_denial(root, "morning", {"tool_name": "Bash", "tool_input": {"b": 2, "a": 1}}, "x")
    first, second = common.read_jsonl(root / "state" / "denied_calls.jsonl")
    assert first["input_sha256"] == second["input_sha256"]


def test_main_denies_and_logs_a_guard_error(root, monkeypatch, capsys):
    assert hook(monkeypatch, "{not json") == 2
    assert "guard error: JSONDecodeError" in capsys.readouterr().err
    [line] = common.read_jsonl(root / "state" / "denied_calls.jsonl")
    assert line["tool"] is None
    assert line["reason"].startswith("guard error: JSONDecodeError")


def link_venv(root):
    (root / ".venv").symlink_to(REPO / ".venv")


def test_denied_call_through_guard_sh_exits_2_and_is_logged(root):
    link_venv(root)
    env = {"JARVIS_MODE": "morning", "JARVIS_RUN_ID": "2026-10-26T0800-morning"}
    result = run_script(root, "guard.sh", stdin=call("WebFetch", url="https://example.com"), env=env)
    assert result.returncode == 2
    assert "WebFetch" in result.stderr
    [line] = common.read_jsonl(root / "state" / "denied_calls.jsonl")
    assert line["tool"] == "WebFetch"
    assert line["mode"] == "morning"
    assert line["run_id"] == "2026-10-26T0800-morning"
    assert "input_sha256" in line and "url" not in json.dumps(line)


def test_allowed_call_through_guard_sh_exits_0(root):
    link_venv(root)
    result = run_script(
        root, "guard.sh", stdin=call("Read", file_path="JARVIS.md"), env={"JARVIS_MODE": "morning"}
    )
    assert result.returncode == 0, result.stderr


def test_malformed_hook_json_is_denied(root):
    link_venv(root)
    result = run_script(root, "guard.sh", stdin="{not json", env={"JARVIS_MODE": "morning"})
    assert result.returncode == 2
    assert "guard error" in result.stderr


def test_guard_sh_with_a_broken_python_exits_2(root):
    venv_bin = root / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    broken = venv_bin / "python"
    broken.write_text("#!/bin/sh\nexit 1\n")
    broken.chmod(0o755)
    result = run_script(
        root, "guard.sh", stdin=call("Read", file_path="JARVIS.md"), env={"JARVIS_MODE": "dev"}
    )
    assert result.returncode == 2


def test_guard_sh_without_any_venv_exits_2(root):
    assert not (root / ".venv").exists()
    result = run_script(
        root, "guard.sh", stdin=call("Read", file_path="JARVIS.md"), env={"JARVIS_MODE": "dev"}
    )
    assert result.returncode == 2


def test_live_settings_wire_guard_and_capture_on_every_tool():
    settings = json.loads((REPO / ".claude" / "settings.json").read_text())
    [pre] = settings["hooks"]["PreToolUse"]
    [post_hook] = settings["hooks"]["PostToolUse"]
    assert pre["matcher"] == "*" and pre["hooks"][0]["command"].endswith("/scripts/guard.sh")
    assert post_hook["matcher"] == "*" and post_hook["hooks"][0]["command"].endswith("/scripts/capture.sh")
    assert os.access(REPO / "scripts" / "guard.sh", os.X_OK)
    assert os.access(REPO / "scripts" / "capture.sh", os.X_OK)


# Delivery adapters and the bootstrap mode


def render_delivery(repo, delivery):
    import yaml
    from conftest import RENDER_NOW

    path = repo / "profile" / "jarvis.profile.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["delivery"] = delivery
    path.write_text(yaml.safe_dump(profile, sort_keys=False))
    assert run_script(repo, "bootstrap.py", "render", env={"JARVIS_NOW": RENDER_NOW}).returncode == 0


def test_gmail_delivery_allows_only_the_principal_as_the_sole_recipient(root):
    render_delivery(root, {"adapter": "gmail-to-self"})
    send = "mcp__claude_ai_Gmail__send_message"
    guard.decide(root, "morning", call(send, to=["ada@example.com"], subject="Jarvis", body=REPORT))
    for to in (["someone@example.com"], ["ada@example.com", "someone@example.com"], "ada@example.com"):
        assert "not the delivery target" in denial(root, "morning", call(send, to=to, body=REPORT))
    reason = denial(root, "morning", call(send, to=["ada@example.com"], body=REPORT, cc=["x@example.com"]))
    assert "parameter not allowed: cc" in reason


def test_file_only_delivery_leaves_no_post_tool_in_any_mode(root):
    render_delivery(root, {"adapter": "file-only"})
    for tool in (SEND, HEADLESS_SEND, "mcp__claude_ai_Gmail__send_message"):
        assert "not allowed in morning" in denial(root, "morning", post() | {"tool_name": tool})


def test_bootstrap_mode_writes_the_profile_and_owner_file_but_never_config_or_scripts(root):
    for path in ("profile/jarvis.profile.yaml", "OWNER.md", "adapters/sources/linear/adapter.yaml"):
        guard.decide(root, "bootstrap", call("Write", file_path=path, content="x"))
    for path in ("config/allowed_tools.yaml", "scripts/guard.py", "JARVIS.md", "raw/slack/x.jsonl"):
        assert "protected in bootstrap" in denial(root, "bootstrap", call("Write", file_path=path))
    guard.decide(root, "bootstrap", call("Bash", command="uv run scripts/bootstrap.py render"))
    reason = denial(root, "bootstrap", call("Bash", command="uv run scripts/guard_pin.py pin"))
    assert "not allowed in bootstrap" in reason
    assert "not allowed in bootstrap" in denial(root, "bootstrap", post())


def test_other_modes_never_write_the_profile_or_adapters(root):
    for mode in ("morning", "interactive", "consolidate"):
        for path in ("profile/jarvis.profile.yaml", "adapters/sources/slack/prompt.md", "OWNER.md"):
            assert f"protected in {mode}" in denial(root, mode, call("Write", file_path=path))
