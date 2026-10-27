import subprocess

import common
import pytest
import run
import yaml
from conftest import REPO, run_script


@pytest.fixture
def root(repo, tmp_path, monkeypatch):
    """A git repo copy whose claude_bin is a shell script the test writes, with the canary off."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-config"))
    (repo / ".venv").symlink_to(REPO / ".venv")
    (repo / "JARVIS.md").write_text("prompt\n")
    (repo / "memory" / "MEMORY.md").write_text("# Memory\nkept\n")
    agent = tmp_path / "agent.sh"
    config_path = repo / "config" / "jarvis.yaml"
    config = yaml.safe_load(config_path.read_text())
    config.update(claude_bin=str(agent), canary=False, claude_config_dir=str(tmp_path / "claude-config"))
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    assert run_script(repo, "guard_pin.py", "pin").returncode == 0
    for command in (
        ["init", "-q"],
        ["add", "-A"],
        ["-c", "user.name=t", "-c", "user.email=t@e", "commit", "-qm", "base"],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)
    return repo


def agent(root, body):
    path = common.jarvis_config(root)["claude_bin"]
    with open(path, "w") as script:
        script.write("#!/bin/sh\ncat >/dev/null\n" + body + "\n")
    subprocess.run(["chmod", "+x", path], check=True)


def test_failed_consolidate_puts_memory_back_and_removes_new_files(root):
    # An interactive correction not yet committed must survive the failed job too.
    (root / "memory" / "MEMORY.md").write_text("# Memory\nkept\ninteractive correction\n")
    agent(root, "echo broken > memory/MEMORY.md\necho new > memory/new_claim.md\nexit 3")
    result = run_script(root, "run.py", "consolidate", env={"JARVIS_NOW": "2026-10-26T09:30:00+01:00"})
    assert result.returncode == 1
    assert (root / "memory" / "MEMORY.md").read_text() == "# Memory\nkept\ninteractive correction\n"
    assert not (root / "memory" / "new_claim.md").exists()
    [entry] = common.read_json(root / "state" / "runs.json", None)["runs"]
    assert entry["status"] == "failed"
    assert [a["kind"] for a in common.read_json(root / "state" / "alerts.json", None)["alerts"]] == ["failed"]
    assert not list((root / "state" / "tmp").glob("memory-*"))


def test_successful_consolidate_keeps_its_memory_edits(root):
    agent(root, "echo updated > memory/MEMORY.md")
    result = run_script(root, "run.py", "consolidate", env={"JARVIS_NOW": "2026-10-26T09:30:00+01:00"})
    assert result.returncode == 0, result.stderr
    assert (root / "memory" / "MEMORY.md").read_text() == "updated\n"


def test_canary_with_no_new_denied_line_fails_preflight(root):
    agent(root, "exit 0")
    problems = run.canary(root, common.jarvis_config(root), "morning", "2026-10-26T0800-morning")
    assert problems and "no guard denial" in problems[0]


def test_canary_passes_when_the_guard_logged_its_webfetch(root):
    line = '{"run_id": "2026-10-26T0800-morning-canary", "tool": "WebFetch"}'
    agent(root, f"echo '{line}' >> state/denied_calls.jsonl")
    assert run.canary(root, common.jarvis_config(root), "morning", "2026-10-26T0800-morning") == []


def test_canary_with_a_missing_claude_binary_fails_preflight(root):
    config = {**common.jarvis_config(root), "claude_bin": str(root / "nope")}
    assert "could not start" in run.canary(root, config, "morning", "r")[0]


def test_allowed_tools_go_last_because_the_flag_is_variadic():
    command = run.claude_command(
        {"claude_bin": "/bin/claude"}, "opus", 40, ["Read", "Bash(uv run scripts/x.py:*)"]
    )
    assert command[:2] == ["/bin/claude", "-p"]
    assert command[-3:] == ["--allowedTools", "Read", "Bash(uv run scripts/x.py:*)"]


def test_holiday_prompt_says_unsent_and_lists_open_alerts():
    plan = {"mode": "eod", "slot": "2026-11-11T18:00:00+01:00", "holiday": True}
    prompt = run.session_prompt(plan, ["Alert: the morning run r1 failed, see state/logs/r1.log."])
    assert "reports/2026-11-11-eod.md" in prompt
    assert "do not post" in prompt
    assert "  Alert: the morning run r1 failed" in prompt


def test_holiday_run_sets_unsent_for_the_guard(root):
    agent(root, 'echo "$JARVIS_UNSENT" > state/tmp/unsent.txt')
    (root / "state" / "tmp").mkdir(exist_ok=True)
    # 2026-05-14 is Ascension Day in the rendered config/holidays.md.
    assert run_script(root, "run.py", "eod", env={"JARVIS_NOW": "2026-05-14T18:01:00+02:00"}).returncode == 0
    assert (root / "state" / "tmp" / "unsent.txt").read_text().strip() == "1"


def test_commit_takes_only_data_paths(root):
    (root / "reports").mkdir(exist_ok=True)
    (root / "reports" / "r.md").write_text("x")
    (root / "scripts" / "bizdays.py").write_text("# edited\n")
    subprocess.run(["git", "-C", str(root), "add", "scripts/bizdays.py"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "user.email", "t@e"], check=True)
    assert run.commit_data(root, "morning 2026-10-26 08:00")
    shown = subprocess.run(
        ["git", "-C", str(root), "show", "--name-only", "--format=", "HEAD"], capture_output=True, text=True
    ).stdout.split()
    assert shown == ["reports/r.md"]
    status = subprocess.run(
        ["git", "-C", str(root), "status", "--porcelain"], capture_output=True, text=True
    ).stdout
    assert "M  scripts/bizdays.py" in status


def test_nothing_to_commit_is_not_an_error(root):
    assert run.commit_data(root, "dream 2026-10-26 02:00") is False


def test_jarvis_launcher_starts_claude_in_the_requested_mode_after_verifying(root, tmp_path):
    out = tmp_path / "env.txt"
    agent(root, f'echo "$JARVIS_MODE $*" > {out}')
    result = run_script(root, "jarvis", "curate")
    assert result.returncode == 0, result.stderr
    mode, *args = out.read_text().split()
    assert mode == "curate"
    assert args[0] == "--allowedTools" and "Edit(curation/misses.md)" in args


def test_jarvis_launcher_refuses_a_tampered_guard(root, tmp_path):
    out = tmp_path / "env.txt"
    agent(root, f"echo started > {out}")
    (root / ".claude" / "settings.local.json").write_text("{}")
    result = run_script(root, "jarvis")
    assert result.returncode == 1
    assert "settings.local.json" in result.stderr
    assert not out.exists()


def runs(root):
    return common.read_json(root / "state" / "runs.json", {"runs": []})["runs"]


def alert_kinds(root):
    return [a["kind"] for a in common.read_json(root / "state" / "alerts.json", {"alerts": []})["alerts"]]


def test_morning_already_done_today_is_skipped_before_any_session(root, tmp_path):
    out = tmp_path / "started.txt"
    agent(root, f"echo started > {out}")
    done = {
        "run_id": "2026-10-26T0800-morning",
        "mode": "morning",
        "slot_date": "2026-10-26",
        "started": "2026-10-26T08:00:00+01:00",
        "finished": "2026-10-26T08:10:00+01:00",
        "status": "ok",
    }
    common.write_json(root / "state" / "runs.json", {"runs": [done]})
    result = run_script(root, "run.py", "morning", env={"JARVIS_NOW": "2026-10-26T08:30:00+01:00"})
    assert result.returncode == 0, result.stderr
    assert "morning already finished ok for 2026-10-26" in result.stdout
    assert runs(root) == [done]
    assert not out.exists()


def test_a_run_left_running_is_closed_as_failed_before_the_next_starts(root):
    agent(root, "exit 0")
    dead = {
        "run_id": "2026-10-26T0830-consolidate",
        "mode": "consolidate",
        "slot_date": "2026-10-26",
        "started": "2026-10-26T08:30:00+01:00",
        "finished": None,
        "status": "running",
    }
    common.write_json(root / "state" / "runs.json", {"runs": [dead]})
    result = run_script(root, "run.py", "consolidate", env={"JARVIS_NOW": "2026-10-26T09:30:00+01:00"})
    assert result.returncode == 0, result.stderr
    assert [(r["run_id"], r["status"]) for r in runs(root)] == [
        ("2026-10-26T0830-consolidate", "failed"),
        ("2026-10-26T0930-consolidate", "ok"),
    ]
    assert runs(root)[0]["finished"] == "2026-10-26T09:30:00+01:00"


def dead_dream():
    return {
        "run_id": "2026-10-26T0200-dream",
        "mode": "dream",
        "slot_date": "2026-10-26",
        "started": "2026-10-26T02:00:00+01:00",
        "finished": None,
        "status": "running",
    }


def test_a_dream_that_died_mid_session_is_undone_and_alerted_on_the_next_run(root):
    # run.py died at 02:00 (reboot, OOM, kill) after the dream half-rewrote memory/; its snapshot is left.
    snapshot = root / "state" / "tmp" / "memory-2026-10-26T0200-dream"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-R", str(root / "memory"), str(snapshot)], check=True)
    (root / "memory" / "MEMORY.md").write_text("# Memory\nhalf rewritten\n")
    (root / "memory" / "half_claim.md").write_text("partial\n")
    common.write_json(root / "state" / "runs.json", {"runs": [dead_dream()]})
    agent(root, "cat memory/MEMORY.md > state/tmp/seen.md")
    result = run_script(root, "run.py", "morning", env={"JARVIS_NOW": "2026-10-26T08:00:00+01:00"})
    assert result.returncode == 0, result.stderr
    # The morning session already sees memory/ as it was before the dream.
    assert (root / "state" / "tmp" / "seen.md").read_text() == "# Memory\nkept\n"
    assert (root / "memory" / "MEMORY.md").read_text() == "# Memory\nkept\n"
    assert not (root / "memory" / "half_claim.md").exists()
    assert not snapshot.exists()
    assert [(r["run_id"], r["status"]) for r in runs(root)] == [
        ("2026-10-26T0200-dream", "failed"),
        ("2026-10-26T0800-morning", "ok"),
    ]
    [alert] = common.read_json(root / "state" / "alerts.json", None)["alerts"]
    assert alert["id"] == "interrupted-2026-10-26T0200-dream"
    assert alert["kind"] == "interrupted"
    assert alert["text"] == (
        "Alert: the dream run 2026-10-26T0200-dream was interrupted, memory/ is back as it was before it, "
        "see state/logs/2026-10-26T0200-dream.log."
    )
    committed = subprocess.run(
        ["git", "-C", str(root), "show", "HEAD:memory/MEMORY.md"], capture_output=True, text=True
    ).stdout
    assert committed == "# Memory\nkept\n"


def test_a_dead_run_without_a_snapshot_keeps_memory_and_is_still_alerted(root):
    # A dead report run never had a snapshot, nor does a dream that died before taking one.
    (root / "memory" / "MEMORY.md").write_text("# Memory\nkept\ninteractive correction\n")
    common.write_json(root / "state" / "runs.json", {"runs": [dead_dream()]})
    agent(root, "exit 0")
    result = run_script(root, "run.py", "morning", env={"JARVIS_NOW": "2026-10-26T08:00:00+01:00"})
    assert result.returncode == 0, result.stderr
    assert (root / "memory" / "MEMORY.md").read_text() == "# Memory\nkept\ninteractive correction\n"
    assert runs(root)[0]["status"] == "failed"
    [alert] = common.read_json(root / "state" / "alerts.json", None)["alerts"]
    assert alert["kind"] == "interrupted"
    assert alert["text"] == (
        "Alert: the dream run 2026-10-26T0200-dream was interrupted, "
        "see state/logs/2026-10-26T0200-dream.log."
    )


def test_a_missing_claude_binary_fails_the_run_with_a_logged_reason(root):
    result = run_script(root, "run.py", "consolidate", env={"JARVIS_NOW": "2026-10-26T09:30:00+01:00"})
    assert result.returncode == 1
    assert [r["status"] for r in runs(root)] == ["failed"]
    assert alert_kinds(root) == ["failed"]
    log = (root / "state" / "logs" / "2026-10-26T0930-consolidate.log").read_text()
    assert "could not start" in log


def test_capture_errors_logged_during_a_run_become_an_alert(root):
    error = '{"at": "2026-10-26T09:30:00+01:00", "source": "slack", "error": "boom"}'
    agent(root, f"echo '{error}' >> state/capture_errors.jsonl")
    result = run_script(root, "run.py", "consolidate", env={"JARVIS_NOW": "2026-10-26T09:30:00+01:00"})
    assert result.returncode == 0, result.stderr
    [alert] = common.read_json(root / "state" / "alerts.json", None)["alerts"]
    assert alert["kind"] == "capture_errors"
    assert alert["text"].startswith("Alert: 1 raw capture errors during 2026-10-26T0930-consolidate")


def test_a_failed_commit_fails_the_run_and_writes_last_error(root):
    agent(root, "echo updated > memory/MEMORY.md")
    # A signing program that always fails makes `git commit` fail after the data is staged.
    subprocess.run(["git", "-C", str(root), "config", "commit.gpgsign", "true"], check=True)
    subprocess.run(["git", "-C", str(root), "config", "gpg.program", "false"], check=True)
    result = run_script(root, "run.py", "consolidate", env={"JARVIS_NOW": "2026-10-26T09:30:00+01:00"})
    assert result.returncode == 1
    assert "git commit failed" in result.stderr
    assert [r["status"] for r in runs(root)] == ["ok"]
    assert "Commit failed for 2026-10-26T0930-consolidate" in (root / "state" / "last_error.md").read_text()


def test_no_data_paths_is_nothing_to_commit(tmp_path):
    assert run.commit_data(tmp_path, "dream 2026-10-26 02:00") is False


def test_staging_outside_a_git_repo_is_a_run_error(tmp_path):
    (tmp_path / "memory").mkdir()
    with pytest.raises(run.RunError, match="git add failed"):
        run.commit_data(tmp_path, "dream 2026-10-26 02:00")


@pytest.mark.parametrize("argv", [[], ["morning", "now"]])
def test_main_without_a_known_command_prints_the_usage(repo, argv, capsys):
    assert run.main(argv) == 2
    assert "Usage:" in capsys.readouterr().err


def test_main_rejects_an_unknown_scheduled_mode(repo, capsys):
    assert run.main(["weekly"]) == 2
    assert "unknown mode weekly" in capsys.readouterr().err


@pytest.fixture
def execs(root, monkeypatch):
    """Record os.execve calls in this process instead of replacing it."""
    calls = []
    monkeypatch.chdir(root.parent)
    monkeypatch.setattr(run.os, "execve", lambda path, argv, env: calls.append((path, argv, env)))
    return calls


def test_open_rejects_an_unknown_mode(root, execs, capsys):
    assert run.main(["open", "replay"]) == 2
    assert "mode must be one of interactive, curate, bootstrap, dev" in capsys.readouterr().err
    assert execs == []


def test_open_refuses_a_tampered_guard_in_process(root, execs, capsys):
    (root / ".claude" / "settings.local.json").write_text("{}")
    assert run.open_session(root, "interactive") == 1
    assert "settings.local.json" in capsys.readouterr().err
    assert execs == []


def test_open_execs_claude_with_the_mode_rules_from_the_repo_root(root, execs):
    run.main(["open", "curate"])
    [(path, argv, env)] = execs
    assert path == common.jarvis_config(root)["claude_bin"]
    assert argv[1] == "--allowedTools" and "Edit(curation/misses.md)" in argv
    assert env["JARVIS_MODE"] == "curate"
    assert env["JARVIS_RUN_ID"].startswith("curate-")
    assert run.os.getcwd() == str(root)


def test_open_dev_execs_claude_without_allowed_tools(root, execs):
    run.open_session(root, "dev")
    [(path, argv, env)] = execs
    assert argv == [path]
    assert env["JARVIS_MODE"] == "dev"


def test_an_unbootstrapped_repo_refuses_scheduled_runs_and_every_mode_but_bootstrap_and_dev(root, capsys):
    config_path = root / "config" / "jarvis.yaml"
    config_path.write_text(config_path.read_text().replace("bootstrapped: true", "bootstrapped: false"))
    agent(root, "touch state/tmp/ran")
    result = run_script(root, "run.py", "morning", env={"JARVIS_NOW": "2026-10-26T08:04:00+01:00"})
    assert result.returncode == 1
    assert "not bootstrapped" in result.stderr
    assert not (root / "state" / "tmp" / "ran").exists()
    assert run.main(["open", "interactive"]) == 1
    assert "start with scripts/jarvis bootstrap" in capsys.readouterr().err


def test_file_only_delivery_clears_alerts_when_the_report_file_passes_and_alerts_when_it_fails(root):
    import yaml

    config_path = root / "config" / "jarvis.yaml"
    config = yaml.safe_load(config_path.read_text())
    config["delivery"] = {"adapter": "file-only", "posts": False}
    config_path.write_text(yaml.safe_dump(config, sort_keys=False))
    assert run_script(root, "guard_pin.py", "pin").returncode == 0
    common.write_json(
        root / "state" / "alerts.json",
        {"alerts": [{"id": "x", "kind": "failed", "text": "Alert: old.", "at": "2026-10-25T08:00:00+01:00"}]},
    )
    agent(root, "echo 'not a report' > reports/2026-10-26-morning.md")
    assert (
        run_script(root, "run.py", "morning", env={"JARVIS_NOW": "2026-10-26T08:04:00+01:00"}).returncode == 0
    )
    kinds = [a["kind"] for a in common.read_json(root / "state" / "alerts.json", {})["alerts"]]
    assert kinds == ["failed", "report_check"]
