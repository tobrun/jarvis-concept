from datetime import date

import common
from conftest import run_script
from loops import split_frontmatter, stale_loops

LIMITS = {
    "stale_waiting_business_days": 2,
    "stale_jira_in_progress_days": 5,
    "stale_pr_review_days": 3,
    "snooze_default_business_days": 3,
}
NO_HOLIDAYS: set[date] = set()
MONDAY_MORNING = "2026-10-26T09:00:00+01:00"


def loop(**fields):
    base = {
        "id": "l1",
        "type": "waiting_on_me",
        "kind": None,
        "status": "open",
        "last_movement": "2026-10-22T15:00:00+02:00",
        "due": None,
        "snoozed_until": None,
        "surfaced_at": [],
        "raw_ids": ["slack:C01:1729600000.000100"],
    }
    return {**base, **fields}


def stale_ids(*loops):
    moment = common.parse_ts(MONDAY_MORNING)
    return [found["id"] for found in stale_loops(list(loops), moment, LIMITS, NO_HOLIDAYS)]


def test_waiting_on_me_moved_thursday_is_slipping_on_monday():
    assert stale_ids(loop(last_movement="2026-10-22T15:00:00+02:00")) == ["l1"]


def test_waiting_on_me_moved_friday_is_not_slipping_on_monday():
    assert stale_ids(loop(last_movement="2026-10-23T15:00:00+02:00")) == []


def test_jira_in_progress_idle_five_calendar_days_is_slipping():
    jira = loop(type="waiting_on_other", kind="jira_in_progress", last_movement="2026-10-21T10:00:00+02:00")
    assert stale_ids(jira) == ["l1"]


def test_pr_review_idle_two_days_is_not_slipping():
    pr = loop(type="waiting_on_other", kind="pr_review", last_movement="2026-10-24T10:00:00+02:00")
    assert stale_ids(pr) == []


def test_loop_due_today_is_slipping():
    deadline = loop(type="deadline", last_movement="2026-10-26T08:00:00+01:00", due="2026-10-26")
    assert stale_ids(deadline) == ["l1"]


def test_loop_snoozed_until_tomorrow_is_not_listed():
    assert stale_ids(loop(snoozed_until="2026-10-27")) == []


def test_stale_entry_carries_the_reason():
    found = stale_loops([loop()], common.parse_ts(MONDAY_MORNING), LIMITS, NO_HOLIDAYS)
    assert found[0]["reason"] == "waiting_on_me idle 2 business days (threshold 2)"


LOOP_FILE = """---
id: vendor-quote
type: waiting_on_other
kind: null
status: open
last_movement: '2026-10-20T11:00:00+02:00'
due: null
snoozed_until: null
surfaced_at: []
raw_ids:
- gmail:18c2
---
# Vendor quote

Waiting on Anna for the signed quote.
"""


def test_snooze_without_days_sets_snoozed_until_three_business_days_ahead(repo):
    path = repo / "memory" / "loops" / "vendor-quote.md"
    path.parent.mkdir(parents=True)
    path.write_text(LOOP_FILE)

    result = run_script(repo, "loops.py", "snooze", "vendor-quote", env={"JARVIS_NOW": MONDAY_MORNING})

    assert result.returncode == 0, result.stderr
    meta, body = split_frontmatter(path.read_text())
    assert meta["snoozed_until"] == "2026-10-29"
    assert meta["raw_ids"] == ["gmail:18c2"]
    assert body == "# Vendor quote\n\nWaiting on Anna for the signed quote.\n"


def test_snoozed_loop_drops_out_of_the_stale_list(repo):
    path = repo / "memory" / "loops" / "vendor-quote.md"
    path.parent.mkdir(parents=True)
    path.write_text(LOOP_FILE)
    env = {"JARVIS_NOW": MONDAY_MORNING}

    before = run_script(repo, "loops.py", "stale", env=env)
    run_script(repo, "loops.py", "snooze", "vendor-quote", env=env)
    after = run_script(repo, "loops.py", "stale", env=env)

    assert '"id": "vendor-quote"' in before.stdout
    assert after.stdout.strip() == "[]"


def test_resolved_and_dropped_loops_are_never_stale():
    assert stale_ids(loop(status="resolved"), loop(id="l2", status="dropped", due="2026-10-20")) == []


def test_loop_snoozed_until_today_is_listed_again():
    assert stale_ids(loop(snoozed_until="2026-10-26")) == ["l1"]


def test_loop_that_never_moved_and_has_no_due_date_is_not_stale():
    assert stale_ids(loop(last_movement=None)) == []


def test_watch_loop_has_no_idle_threshold():
    assert stale_ids(loop(type="watch", last_movement="2026-09-01T10:00:00+02:00")) == []


def test_text_without_frontmatter_or_with_an_unclosed_block_is_all_body():
    for text in ("", "# Notes\n", "---\nid: x\n# never closed\n"):
        assert split_frontmatter(text) == ({}, text)


def test_empty_frontmatter_is_an_empty_dict_and_dates_become_iso_strings():
    assert split_frontmatter("---\n---\nbody\n") == ({}, "body\n")
    meta, _ = split_frontmatter("---\ndue: 2026-10-30\nsurfaced_at:\n- 2026-10-26T08:00:00+01:00\n---\n")
    assert meta == {"due": "2026-10-30", "surfaced_at": ["2026-10-26T08:00:00+01:00"]}
