import fcntl
import json
import os
import threading
import time
from datetime import datetime

import common
import eval_gate
import pytest
import replay
from conftest import run_script

pytestmark = pytest.mark.e2e

SEND = "mcp__plugin_enterprise-search_slack__slack_send_message"
READ_CHANNEL = "mcp__plugin_enterprise-search_slack__slack_read_channel"
SELF_DM = "U0EXAMPLE01"
SLACK_RESPONSE = {"messages": [{"ts": "1729.0001", "user": "U0ANA", "text": "Budget reply by today?"}]}


def report(header="Jarvis, morning, 2026-10-26 08:04", alerts=()):
    lines = [
        header,
        *alerts,
        "",
        "Today",
        "  Budget reply to Ana is due today. Send it? [slack:C1:1729.0001]",
    ]
    lines += ["Slipping: none", "Waiting on you: none", "Waiting on others: none", "FYI: none", ""]
    lines += ["Sources: slack 1/1. Candidates 1, surfaced 1, suppressed 0. Denied calls 1.", ""]
    return "\n".join(lines)


def read_slack():
    return {"tool": READ_CHANNEL, "input": {"channel_id": "C1"}, "response": SLACK_RESPONSE}


def write(path, content):
    return {"tool": "Write", "input": {"file_path": path, "content": content}}


def post(text):
    return {"tool": SEND, "input": {"channel_id": SELF_DM, "message": text}, "response": {"ts": "1729.9"}}


def record_post():
    message = json.dumps({"message_id": "1729.9", "raw_id": None, "kind": "report"})
    return [
        write("state/tmp/message.json", message),
        {
            "tool": "Bash",
            "input": {"command": "uv run scripts/append.py report_messages --file state/tmp/message.json"},
        },
    ]


def morning_steps(text=None):
    text = text or report()
    return [
        read_slack(),
        {"tool": "mcp__claude_ai_Atlassian_Rovo__transitionJiraIssue", "input": {"issueIdOrKey": "PLAT-1"}},
        write("reports/{slot_date}-{mode}.md", text),
        post(text),
        *record_post(),
    ]


def outcomes(jarvis):
    return [(c["tool"], c["outcome"]) for c in jarvis.calls()]


def test_scheduled_morning_run_reads_posts_commits_data_only(jarvis):
    (jarvis.root / "scripts" / "bizdays.py").write_text(
        (jarvis.root / "scripts" / "bizdays.py").read_text() + "# local edit\n"
    )
    jarvis.script(morning_steps())
    result = jarvis.run("morning")
    assert result.returncode == 0, result.stderr

    assert outcomes(jarvis) == [
        (READ_CHANNEL, "allowed"),
        ("mcp__claude_ai_Atlassian_Rovo__transitionJiraIssue", "denied_by_hook"),
        ("Write", "allowed"),
        (SEND, "allowed"),
        ("Write", "allowed"),
        ("Bash", "allowed"),
    ]
    [raw_line] = common.read_jsonl(jarvis.root / "raw" / "slack" / "2026-10-26.jsonl")
    assert raw_line["payload"] == SLACK_RESPONSE
    assert raw_line["run_id"] == "2026-10-26T0804-morning"
    denied = common.read_jsonl(jarvis.root / "state" / "denied_calls.jsonl")
    assert any(d["tool"].endswith("transitionJiraIssue") for d in denied)
    assert any(d["tool"] == "WebFetch" and d["run_id"].endswith("-canary") for d in denied)
    assert (jarvis.root / "reports" / "2026-10-26-morning.md").read_text() == report()
    [entry] = jarvis.runs()
    assert entry["status"] == "ok" and entry["finished"]

    assert jarvis.git("log", "-1", "--format=%s").strip() == "morning 2026-10-26 08:04"
    committed = jarvis.git("show", "--name-only", "--format=", "HEAD").split()
    assert "reports/2026-10-26-morning.md" in committed
    assert not [path for path in committed if path.startswith(("scripts/", "raw/"))]
    assert " M scripts/bizdays.py" in jarvis.git("status", "--porcelain")


def test_allowlist_edited_after_pinning_stops_the_run_before_the_agent(jarvis):
    allowlist = jarvis.root / "config" / "allowed_tools.yaml"
    allowlist.write_text(allowlist.read_text().replace("modes:\n", "modes:\n  # widened\n", 1))
    jarvis.script(morning_steps())
    result = jarvis.run("morning")
    assert result.returncode == 1
    assert "config/allowed_tools.yaml" in (jarvis.root / "state" / "last_error.md").read_text()
    assert [a["kind"] for a in jarvis.alerts()] == ["preflight"]
    assert jarvis.records() == []
    assert jarvis.runs() == []


def test_failed_run_then_successful_run_must_carry_the_alert(jarvis):
    jarvis.script([read_slack(), {"exit": 1}])
    assert jarvis.run("morning").returncode == 1
    [alert] = jarvis.alerts()
    assert alert["kind"] == "failed"

    without = report("Jarvis, morning, 2026-10-26 08:20")
    with_alert = report("Jarvis, morning, 2026-10-26 08:20", alerts=[alert["text"]])
    jarvis.script(
        [
            read_slack(),
            post(without),
            write("reports/{slot_date}-{mode}.md", with_alert),
            post(with_alert),
            *record_post(),
        ]
    )
    assert jarvis.run("morning", now="2026-10-26T08:20:00+01:00").returncode == 0
    posts = [c for c in jarvis.calls() if c["tool"] == SEND]
    assert [p["outcome"] for p in posts] == ["denied_by_hook", "allowed"]
    assert "open alert missing" in posts[0]["reason"]
    assert jarvis.alerts() == []
    assert [r["status"] for r in jarvis.runs()] == ["failed", "ok"]


def test_morning_fired_at_1340_runs_as_late_midday_with_the_morning_brief(jarvis):
    jarvis.script([])
    assert jarvis.run("morning", now="2026-10-26T13:40:00+01:00").returncode == 0
    [session] = jarvis.sessions()
    plan = session["plan"]
    assert plan["mode"] == "midday" and plan["requested"] == "morning"
    assert plan["late"] is True and plan["late_minutes"] == 40
    assert plan["include_morning"] is True
    assert jarvis.runs()[0]["run_id"] == "2026-10-26T1340-midday"


def hold_lock(root, seconds):
    fd = os.open(root / "state" / "run.lock", os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX)

    def release():
        time.sleep(seconds)
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)

    thread = threading.Thread(target=release)
    thread.start()
    return thread


def test_report_run_waits_for_a_held_lock_and_runs_after_release(jarvis):
    jarvis.script([])
    thread = hold_lock(jarvis.root, 2)
    started = time.monotonic()
    result = jarvis.run("morning")
    thread.join()
    assert result.returncode == 0, result.stderr
    assert time.monotonic() - started >= 2
    assert [r["status"] for r in jarvis.runs()] == ["ok"]


def test_consolidate_while_a_report_holds_the_lock_exits_without_the_agent(jarvis):
    jarvis.script([])
    thread = hold_lock(jarvis.root, 3)
    result = jarvis.run("consolidate", now="2026-10-26T09:30:00+01:00")
    thread.join()
    assert result.returncode == 0
    assert "skipped" in result.stdout
    assert jarvis.records() == []
    assert jarvis.runs() == []


def test_report_still_locked_after_the_wait_is_recorded_as_skipped_with_an_alert(jarvis):
    jarvis.script([])
    thread = hold_lock(jarvis.root, 9)
    result = jarvis.run("morning")
    thread.join()
    assert result.returncode == 0
    assert [r["status"] for r in jarvis.runs()] == ["skipped"]
    assert [a["kind"] for a in jarvis.alerts()] == ["skipped"]
    assert jarvis.records() == []


def test_session_exceeding_its_timeout_is_killed_with_an_alert(jarvis):
    jarvis.configure(lambda config: config["modes"]["morning"].update(timeout_minutes=0.05))
    jarvis.script([{"sleep": 60}])
    started = time.monotonic()
    result = jarvis.run("morning")
    assert result.returncode == 1
    assert time.monotonic() - started < 30
    assert [r["status"] for r in jarvis.runs()] == ["timeout"]
    assert [a["kind"] for a in jarvis.alerts()] == ["timeout"]


def test_replay_is_offline_isolated_and_writes_the_diff(jarvis):
    jarvis.script(morning_steps())
    assert jarvis.run("morning").returncode == 0
    head = jarvis.git("rev-parse", "HEAD")
    live_memory = jarvis.root / "memory"
    replayed = report().replace("Send it?", "Draft is ready, send it?")
    jarvis.script(
        [
            read_slack(),
            write(str(live_memory / "planted.md"), "x"),
            {"tool": "Read", "input": {"file_path": "raw/slack/2026-10-26.jsonl"}},
            write("reports/2026-10-26-morning.md", replayed),
        ]
    )
    before = len(jarvis.calls())
    result = run_script(jarvis.root, "replay.py", "replay", "2026-10-26", "morning")
    assert result.returncode == 0, result.stderr

    calls = jarvis.calls()[before:]
    assert [(c["tool"], c["outcome"]) for c in calls] == [
        (READ_CHANNEL, "denied_by_hook"),
        ("Write", "denied_by_hook"),
        ("Read", "allowed"),
        ("Write", "allowed"),
    ]
    assert "1729.0001" in calls[2]["response"]["file"]["content"]
    [session] = jarvis.sessions()[1:]
    assert session["run_id"] == "replay-2026-10-26T0804-morning"
    assert not any(rule.startswith("mcp__") for rule in session["allowed"])

    diff = (jarvis.root / "eval" / "replays" / "2026-10-26-morning.md").read_text()
    assert "Denied calls during replay: 2" in diff
    assert "+  Budget reply to Ana is due today. Draft is ready, send it?" in diff
    assert not (live_memory / "planted.md").exists()
    assert jarvis.git("rev-parse", "HEAD") == head
    assert jarvis.git("status", "--porcelain").split("\n")[0].endswith("eval/replays/")


def golden_week(jarvis):
    golden = jarvis.root / "eval" / "golden" / "2026-W44"
    line = {
        "raw_id": "slack:call:golden",
        "source": "slack",
        "tool": READ_CHANNEL,
        "input": {"channel_id": "C1"},
        "payload": SLACK_RESPONSE,
        "fetched_at": "2026-10-26T07:58:00+01:00",
        "run_id": "2026-10-26T0800-morning",
    }
    common.append_jsonl(golden / "raw" / "slack" / "2026-10-26.jsonl", line)
    bumps = [
        {
            "type": "bump",
            "raw_ids": ["slack:C1:1729.0001"],
            "surfaced_at": [],
            "at": "2026-10-27T10:00:00+01:00",
        },
        {"type": "bump", "raw_ids": ["slack:C9:1.1"], "surfaced_at": [], "at": "2026-10-27T11:00:00+01:00"},
    ]
    for bump in bumps:
        common.append_jsonl(golden / "outcomes.jsonl", {**bump, "cause_class": "ranking"})
    head = jarvis.git("rev-parse", "HEAD").strip()
    common.write_json(golden / "manifest.json", {"week": "2026-W44", "start_commit": head, "frozen_at": "x"})


def test_eval_over_a_golden_week_writes_recall_and_the_staged_hash(jarvis):
    golden_week(jarvis)
    bizdays = jarvis.root / "scripts" / "bizdays.py"
    bizdays.write_text(bizdays.read_text() + "# candidate change\n")
    jarvis.script(
        [
            {"tool": "Read", "input": {"file_path": "raw/slack/2026-10-26.jsonl"}},
            write("reports/{slot_date}-{mode}.md", report()),
        ]
    )
    result = run_script(jarvis.root, "replay.py", "eval", "2026-W44")
    assert result.returncode == 0, result.stderr

    last = common.read_json(jarvis.root / "eval" / "last.json", None)
    assert len(last["sessions"]) == 15
    assert {s["status"] for s in last["sessions"]} == {"ok"}
    assert last["bump_recall"] == 0.5
    assert last["miss_count"] == 1
    jarvis.git("add", "scripts/bizdays.py")
    assert last["gated_hash"] == eval_gate.gated_hash(jarvis.root, staged=True)

    assert run_script(jarvis.root, "replay.py", "eval", "--accept").returncode == 0
    assert common.read_json(jarvis.root / "eval" / "baseline.json", None) == last


def test_run_sh_ignores_a_fixed_clock(jarvis):
    jarvis.script([])
    result = run_script(jarvis.root, "run.sh", "consolidate", env={"JARVIS_NOW": "2020-01-06T09:30:00+01:00"})
    assert result.returncode == 0, result.stderr
    [entry] = jarvis.runs()
    assert entry["run_id"].startswith(datetime.now(common.tz()).date().isoformat())


def test_replay_of_a_run_without_a_report_diffs_against_nothing_and_keeps_its_candidates(jarvis):
    assert jarvis.run("morning").returncode == 0
    candidate = {
        "raw_id": "slack:C1:1729.0001",
        "score": 0.9,
        "features": {},
        "reason": "due today",
        "decision": "surface",
        "mode": "morning",
    }
    jarvis.script(
        [
            {
                "tool": "Write",
                "input": {"file_path": "reports/{slot_date}-{mode}.md", "content": "Replayed\n"},
            },
            {"tool": "Write", "input": {"file_path": "state/tmp/c.json", "content": json.dumps(candidate)}},
            {
                "tool": "Bash",
                "input": {"command": "uv run scripts/append.py candidates --file state/tmp/c.json"},
            },
        ]
    )
    assert replay.main(["replay", "2026-10-26", "morning"]) == 0

    out = jarvis.root / "eval" / "replays"
    [kept] = common.read_jsonl(out / "2026-10-26-morning.candidates.jsonl")
    assert kept["raw_id"] == "slack:C1:1729.0001"
    summary = (out / "2026-10-26-morning.md").read_text()
    assert "Denied calls during replay: 0." in summary
    assert "--- original reports/2026-10-26-morning.md" in summary
    assert "+Replayed" in summary


def use_file_only_delivery(jarvis):
    """Re-render the example profile with file-only delivery, keep the harness settings, and re-pin."""
    import yaml
    from conftest import RENDER_NOW

    path = jarvis.root / "profile" / "jarvis.profile.yaml"
    profile = yaml.safe_load(path.read_text())
    profile["delivery"] = {"adapter": "file-only"}
    path.write_text(yaml.safe_dump(profile, sort_keys=False))
    settings = {
        k: common.jarvis_config(jarvis.root)[k]
        for k in ("claude_bin", "claude_config_dir", "lock_wait_minutes")
    }
    modes = common.jarvis_config(jarvis.root)["modes"]
    assert run_script(jarvis.root, "bootstrap.py", "render", env={"JARVIS_NOW": RENDER_NOW}).returncode == 0
    jarvis.configure(lambda config: config.update(settings, modes=modes))


def test_file_only_run_has_no_send_tool_and_a_passing_report_file_clears_the_alerts(jarvis):
    use_file_only_delivery(jarvis)
    jarvis.script([read_slack(), {"exit": 1}])
    assert jarvis.run("morning").returncode == 1
    [alert] = jarvis.alerts()

    text = report("Jarvis, morning, 2026-10-26 08:20", alerts=[alert["text"]])
    jarvis.script([read_slack(), post(text), write("reports/{slot_date}-{mode}.md", text)])
    assert jarvis.run("morning", now="2026-10-26T08:20:00+01:00").returncode == 0
    sends = [c for c in jarvis.calls() if c["tool"] == SEND]
    assert [s["outcome"] for s in sends] == ["denied_by_hook"]
    assert "is not allowed in morning" in sends[0]["reason"]
    assert jarvis.alerts() == []
    assert "reports/2026-10-26-morning.md" in jarvis.git("show", "--name-only", "--format=", "HEAD").split()
