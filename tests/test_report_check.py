import json

import common
import raw
import report_check
from conftest import run_script

GOOD = """Jarvis, morning, 2026-10-26 08:04

Today
  10:00 PLAT sync with Ana: she owes the eval plan, you owe the budget reply. Prep it? [slack:C1:1729.0001]
    Last touchpoint Thursday; no agenda yet.
Slipping
  Budget reply to Ana, 3 business days, draft ready. Send it? [slack:C1:1729.0002]
Waiting on you: none
Waiting on others
  Leo, review of the guard PR since Friday. Nudge him? [slack:C1:1729.0001]
FYI: none

Sources: slack 40/40, jira 12/12. Skipped wispr. Candidates 9, surfaced 3, suppressed 6. Denied calls 0.
"""


def seed_raw(root):
    line = {
        "raw_id": "slack:call:aaaa",
        "source": "slack",
        "tool": "mcp__plugin_enterprise-search_slack__slack_read_channel",
        "input": {"channel_id": "C1"},
        "payload": {"messages": [{"ts": "1729.0001", "text": "a"}, {"ts": "1729.0002", "text": "b"}]},
        "fetched_at": "2026-10-26T08:01:00+01:00",
        "run_id": "2026-10-26T0800-morning",
    }
    common.append_jsonl(root / "raw" / "slack" / "2026-10-26.jsonl", line)


def test_well_formed_report_has_no_problems(repo):
    seed_raw(repo)
    assert report_check.check(GOOD, repo) == []


def test_report_of_61_lines_is_rejected_with_the_count():
    long = GOOD.replace("FYI: none", "FYI: none" + "\n" * 48)
    problems = report_check.check(long)
    assert any("61 lines" in p for p in problems)


def test_report_of_60_lines_is_rejected_with_the_count():
    long = GOOD.replace("FYI: none", "FYI: none" + "\n" * 47)
    assert "report has 60 lines; it must stay under 60" in report_check.check(long)


def test_report_of_59_lines_passes():
    assert report_check.check(GOOD.replace("FYI: none", "FYI: none" + "\n" * 46)) == []


def test_header_naming_an_unknown_mode_is_rejected():
    problems = report_check.check(GOOD.replace("Jarvis, morning,", "Jarvis, weekend,"))
    assert "line 1: header must be `Jarvis, <mode>, <YYYY-MM-DD> <HH:MM>`" in problems


def test_em_dash_anywhere_is_rejected():
    problems = report_check.check(GOOD.replace("Prep it?", "Prep it \u2014 now?"))
    assert any("em dash" in p for p in problems)


def test_exclamation_mark_is_rejected():
    problems = report_check.check(GOOD.replace("Send it?", "Send it!"))
    assert any("exclamation" in p for p in problems)


def test_slipping_before_today_is_an_order_problem():
    swapped = GOOD.replace("Today\n", "Slipping\n", 1).replace("Slipping\n  Budget", "Today\n  Budget", 1)
    problems = report_check.check(swapped)
    assert any("in that order" in p for p in problems)


def test_item_line_without_raw_id_names_the_line():
    problems = report_check.check(GOOD.replace(" [slack:C1:1729.0002]", ""))
    assert "line 7: item has no [raw_id]" in problems


def test_section_name_indented_by_one_space_is_not_a_section():
    problems = report_check.check(GOOD.replace("\nToday\n", "\n Today\n", 1))
    assert (
        "sections must be Today, Slipping, Waiting on you, Waiting on others, FYI in that order; "
        "found Slipping, Waiting on you, Waiting on others, FYI"
    ) in problems


def test_missing_sources_line_is_a_problem():
    without = "\n".join(line for line in GOOD.splitlines() if not line.startswith("Sources:"))
    assert "missing the Sources line after FYI" in report_check.check(without)


def test_six_fyi_items_are_too_many():
    fyi = "FYI\n" + "".join(f"  Note {n}. [slack:C1:1729.000{n}]\n" for n in range(1, 7))
    problems = report_check.check(GOOD.replace("FYI: none\n", fyi))
    assert "FYI has 6 items; at most 5" in problems


def test_report_without_sections_says_none_were_found():
    problems = report_check.check("Jarvis, morning, 2026-10-26 08:04\n\nSources: none\n")
    assert any(p.endswith("in that order; found none") for p in problems)


def test_weekly_addendum_after_sources_is_free_form():
    weekly = GOOD.replace("morning", "eod") + "\nGoal review\n  G1 platform evals, moved, 0.8 to 0.85\n"
    assert report_check.check(weekly) == []


def test_item_citing_an_id_absent_from_raw_names_the_id(repo):
    seed_raw(repo)
    problems = report_check.check(GOOD.replace("C1:1729.0002", "C9:1729.0009"), repo)
    assert "line 7: [slack:C9:1729.0009] does not resolve in raw/" in problems


def test_item_citing_a_tombstoned_id_is_rejected(repo):
    seed_raw(repo)
    assert raw.tombstone(repo, "slack:C1:1729.0002", "hr").status != "not_found"
    problems = report_check.check(GOOD, repo)
    assert "line 7: [slack:C1:1729.0002] is tombstoned and must not be cited" in problems


def test_item_citing_a_malformed_id_is_rejected(repo):
    seed_raw(repo)
    problems = report_check.check(GOOD.replace("C1:1729.0002", "C1"), repo)
    assert "line 7: [slack:C1] is not a well-formed raw id" in problems


def test_reply_citing_an_absent_id_names_its_line(repo):
    problems = report_check.check_reply("Done.\nSee [slack:C9:1729.0009]\n", repo)
    assert problems == ["line 2: [slack:C9:1729.0009] does not resolve in raw/"]


def test_open_alert_missing_before_today_is_a_problem(repo):
    seed_raw(repo)
    alerts = {"alerts": [{"id": "a1", "kind": "timeout", "text": "Last eod run timed out.", "at": "x"}]}
    (repo / "state" / "alerts.json").write_text(json.dumps(alerts))
    assert "open alert missing before Today: Last eod run timed out." in report_check.check(GOOD, repo)


def test_open_alert_on_line_2_satisfies_the_check(repo):
    seed_raw(repo)
    alerts = {"alerts": [{"id": "a1", "kind": "timeout", "text": "Last eod run timed out.", "at": "x"}]}
    (repo / "state" / "alerts.json").write_text(json.dumps(alerts))
    with_alert = GOOD.replace("08:04\n", "08:04\nLast eod run timed out.\n", 1)
    assert report_check.check(with_alert, repo) == []


def test_cli_exits_1_and_prints_problems(repo, tmp_path):
    report = tmp_path / "r.md"
    report.write_text(GOOD.replace("Send it?", "Send it!"))
    result = run_script(repo, "report_check.py", str(report))
    assert result.returncode == 1
    assert "exclamation mark" in result.stdout


def test_cli_reply_mode_checks_the_reply(repo, tmp_path):
    reply = tmp_path / "reply.txt"
    reply.write_text("Done!\n")
    result = run_script(repo, "report_check.py", "--reply", str(reply))
    assert result.returncode == 1
    assert result.stdout == "line 1: exclamation mark\n"


def test_cli_exits_0_and_prints_nothing_for_a_clean_reply(repo, tmp_path):
    reply = tmp_path / "reply.txt"
    reply.write_text("Done.\n")
    result = run_script(repo, "report_check.py", "--reply", str(reply))
    assert result.returncode == 0
    assert result.stdout == ""


def test_cli_without_a_file_prints_usage_and_exits_2(repo):
    result = run_script(repo, "report_check.py", "--reply")
    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_item_citing_a_prefix_of_a_fetched_id_does_not_resolve(repo):
    seed_raw(repo)
    line = {
        "raw_id": "jira:call:bbbb",
        "source": "jira",
        "tool": "mcp__claude_ai_Atlassian_Rovo__getJiraIssue",
        "input": {"issueIdOrKey": "PLAT-123"},
        "payload": {"key": "PLAT-123", "fields": {"summary": "Login regression"}},
        "fetched_at": "2026-10-26T08:01:00+01:00",
        "run_id": "2026-10-26T0800-morning",
    }
    common.append_jsonl(repo / "raw" / "jira" / "2026-10-26.jsonl", line)
    assert report_check.check(GOOD.replace("slack:C1:1729.0002", "jira:PLAT-123"), repo) == []
    problems = report_check.check(GOOD.replace("slack:C1:1729.0002", "jira:PLAT-12"), repo)
    assert "line 7: [jira:PLAT-12] does not resolve in raw/" in problems
