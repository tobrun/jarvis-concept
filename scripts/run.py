"""The scheduled runner and the interactive launcher: everything around one Claude Code session.

Usage:
  run.py <morning|midday|eod|consolidate|dream>   one scheduled run (the scheduler, through scripts/run.sh)
  run.py open <interactive|curate|bootstrap|dev>  verify the guard, then exec Claude Code in that mode
                                                  (scripts/jarvis); bootstrap starts the interview skill

Until the profile is rendered (`bootstrapped: false` in config/jarvis.yaml) only bootstrap and dev open, and
a scheduled run exits 1 without a session.

A scheduled run, in order:
  lock       one fcntl lock on state/run.lock; report modes wait up to `lock_wait_minutes`, then record
             `skipped` and an alert and exit 0; consolidate and dream exit 0 at once when it is busy
  preflight  guard_pin verify, guard_pin self-test, and (when `canary` is on) a live canary: a short
             haiku session asked to call WebFetch, which its rules allow and only the hook can deny; it
             passes when a matching line lands in state/denied_calls.jsonl. Any failure writes
             state/last_error.md and an alert, exits 1, and never starts the real session
  plan       schedule.plan; a `skip` exits 0; any other `running` entry is dead (we hold the lock): it is
             closed as failed with an `interrupted` alert, and a dead consolidate or dream first gets
             memory/ back from its snapshot in state/tmp
  session    `claude -p` from config/jarvis.yaml with the mode's model, turns, and wall clock, the prompt on
             stdin, `--allowedTools` from guard.py rules, JARVIS_MODE, JARVIS_RUN_ID, JARVIS_UNSENT=1 on a
             holiday, and CLAUDE_CONFIG_DIR from `claude_config_dir`; output to state/logs/<run_id>.log
  finish     ok, failed (non-zero exit), or timeout (killed) in state/runs.json; a failed or timed out
             consolidate or dream puts memory/ back exactly as it was before the session; capture
             errors logged during the run become an alert; a report run that delivered its report clears
             the alerts that were open when it started: recorded in state/report_messages.jsonl when the
             delivery adapter sends, or for file-only delivery a report file that passes report_check
  commit     only memory/ drafts/ reports/ curation/ state/ eval/replays/, message `<mode> <date> <HH:MM>`,
             with --no-verify (those paths are outside the eval gate); never anything else
Exit 0 on ok or a planned skip, 1 on a preflight failure, a failed or timed out session, or a failed commit.

state/alerts.json is the list report_check.py requires on every posted report until one clears it.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import Any

import common
import guard
import guard_pin
import report_check
import schedule

DATA_PATHS = ("memory", "drafts", "reports", "curation", "state", "eval/replays")
INTERACTIVE_MODES = ("interactive", "curate", "bootstrap", "dev")
UNBOOTSTRAPPED_MODES = ("bootstrap", "dev")
CANARY_PROMPT = (
    "This is a guard canary. Call the WebFetch tool exactly once with url https://example.com and prompt "
    "'title'. Do nothing else and do not retry."
)
CANARY_TIMEOUT_SECONDS = 180
KILL_GRACE_SECONDS = 10


class RunError(Exception):
    """A failure that ends the run with an alert; the message is the alert text."""


@contextlib.contextmanager
def file_lock(path: Path, wait_seconds: float) -> Iterator[bool]:
    """Hold an exclusive flock on `path`, polling up to `wait_seconds`; yields whether it was acquired."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait_seconds
    acquired = False
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    break
                time.sleep(min(0.5, max(deadline - time.monotonic(), 0.01)))
        yield acquired
    finally:
        if acquired:
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


@contextlib.contextmanager
def state_lock(root: Path) -> Iterator[None]:
    """Serialize read-modify-write of state/runs.json and state/alerts.json across processes."""
    with file_lock(root / "state" / "runs.lock", wait_seconds=60) as acquired:
        if not acquired:
            raise RunError("state/runs.lock stayed busy for 60 seconds")
        yield


def update_runs(root: Path, change) -> None:
    with state_lock(root):
        path = schedule.runs_path(root)
        runs = common.read_json(path, {"runs": []}).get("runs", [])
        common.write_json(path, {"runs": change(runs)})


def add_alert(root: Path, kind: str, run_id: str, text: str) -> None:
    with state_lock(root):
        path = root / "state" / "alerts.json"
        alerts = common.read_json(path, {"alerts": []}).get("alerts", [])
        alert_id = f"{kind}-{run_id}"
        if not any(alert.get("id") == alert_id for alert in alerts):
            alerts.append({"id": alert_id, "kind": kind, "text": text, "at": common.now().isoformat()})
        common.write_json(path, {"alerts": alerts})


def clear_alerts_before(root: Path, moment: datetime) -> None:
    """Drop the alerts that were already open at `moment`: the report posted after it carried them."""
    with state_lock(root):
        path = root / "state" / "alerts.json"
        alerts = common.read_json(path, {"alerts": []}).get("alerts", [])
        kept = [alert for alert in alerts if common.parse_ts(alert["at"]) >= moment]
        common.write_json(path, {"alerts": kept})


def write_last_error(root: Path, title: str, problems: list[str]) -> None:
    lines = [f"# {title}", "", f"At {common.now().isoformat()}.", "", *[f"- {p}" for p in problems], ""]
    path = root / "state" / "last_error.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines))


def claude_env(root: Path, config: dict[str, Any], mode: str, run_id: str) -> dict[str, str]:
    env = {**os.environ, "JARVIS_MODE": mode, "JARVIS_RUN_ID": run_id, "JARVIS_ROOT": str(root)}
    env.pop("JARVIS_UNSENT", None)
    env.pop("CLAUDECODE", None)
    if config.get("claude_config_dir"):
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config["claude_config_dir"])
    return env


def claude_command(config: dict[str, Any], model: str, max_turns: int, rules: list[str]) -> list[str]:
    """`claude -p` reading its prompt from stdin; --allowedTools is variadic, so it goes last."""
    command = [os.path.expanduser(config["claude_bin"]), "-p", "--model", str(model)]
    command += ["--max-turns", str(max_turns), "--permission-mode", "default"]
    return command + (["--allowedTools", *rules] if rules else [])


def run_session(
    command: list[str], prompt: str, env: dict[str, str], cwd: Path, timeout_seconds: float, log: Path
) -> str:
    """Run one session to completion or the wall clock; returns ok, failed, or timeout."""
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("a") as out:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=out,
            stderr=subprocess.STDOUT,
            env=env,
            cwd=cwd,
            text=True,
            start_new_session=True,
        )
        with contextlib.suppress(BrokenPipeError):
            process.stdin.write(prompt)
            process.stdin.close()
        try:
            code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            _kill_group(process)
            out.write(f"\n[run.py] killed after {timeout_seconds:.0f} seconds\n")
            return "timeout"
    return "ok" if code == 0 else "failed"


def _kill_group(process: subprocess.Popen) -> None:
    for sig, grace in ((signal.SIGTERM, KILL_GRACE_SECONDS), (signal.SIGKILL, None)):
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)
        try:
            process.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def canary(root: Path, config: dict[str, Any], mode: str, run_id: str) -> list[str]:
    """Prove Claude Code loads the hooks: a WebFetch its rules allow must be denied by the guard."""
    canary_id = f"{run_id}-canary"
    command = claude_command(config, config.get("canary_model", "haiku"), 2, ["WebFetch"])
    log = root / "state" / "logs" / f"{canary_id}.log"
    env = claude_env(root, config, mode, canary_id)
    try:
        run_session(command, CANARY_PROMPT, env, root, CANARY_TIMEOUT_SECONDS, log)
    except OSError as error:
        return [f"canary could not start {command[0]}: {error}"]
    denied = common.read_jsonl(root / "state" / "denied_calls.jsonl")
    if any(line.get("run_id") == canary_id and line.get("tool") == "WebFetch" for line in denied):
        return []
    return [f"canary: no guard denial was logged for {canary_id}; the hooks may not be loaded (see {log})"]


def preflight(root: Path, config: dict[str, Any], mode: str, run_id: str) -> list[str]:
    problems = guard_pin.verify(root)
    if not problems:
        problems = guard_pin.self_test(root)
    if not problems and config.get("canary", True):
        problems = canary(root, config, mode, run_id)
    return problems


def git(root: Path, *args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(root), *args], input=stdin, capture_output=True, text=True, check=False
    )


def stage_data(root: Path) -> str:
    """Stage the data paths that exist; returns the staged data files NUL separated, empty for none."""
    paths = [p for p in DATA_PATHS if (root / p).exists()]
    if not paths:
        return ""
    added = git(root, "add", "-A", "--", *paths)
    if added.returncode != 0:
        raise RunError(f"git add failed: {added.stderr.strip()}")
    return git(root, "diff", "--cached", "--name-only", "-z", "--no-renames", "--", *paths).stdout


def commit_data(root: Path, message: str) -> bool:
    """Commit the data paths only; False when there was nothing to commit.

    The commit names the staged data files exactly, so anything else already in the index stays staged and
    uncommitted, and an empty data directory git does not know is never a pathspec error.
    """
    staged = stage_data(root)
    if not staged:
        return False
    done = git(
        root,
        "--literal-pathspecs",
        "commit",
        "--no-verify",
        "-q",
        "-m",
        message,
        "--pathspec-from-file=-",
        "--pathspec-file-nul",
        stdin=staged,
    )
    if done.returncode != 0:
        raise RunError(f"git commit failed: {done.stderr.strip()}")
    return True


def snapshot_memory(root: Path, run_id: str) -> Path:
    snapshot = root / "state" / "tmp" / f"memory-{run_id}"
    if snapshot.exists():
        shutil.rmtree(snapshot)
    if (root / "memory").exists():
        shutil.copytree(root / "memory", snapshot, symlinks=True)
    else:
        snapshot.mkdir(parents=True)
    return snapshot


def restore_memory(root: Path, snapshot: Path) -> None:
    """Put memory/ back exactly as the snapshot holds it: edits undone, new files gone."""
    if (root / "memory").exists():
        shutil.rmtree(root / "memory")
    shutil.copytree(snapshot, root / "memory", symlinks=True)


def bounds(config: dict[str, Any], mode: str, first_run: bool) -> dict[str, Any]:
    modes = config.get("modes") or {}
    return modes["first_run"] if first_run and mode in schedule.REPORT_MODES else modes[mode]


def open_alerts(root: Path) -> list[str]:
    return [
        a["text"] for a in common.read_json(root / "state" / "alerts.json", {"alerts": []}).get("alerts", [])
    ]


def session_prompt(plan: dict[str, Any], alerts: list[str]) -> str:
    mode = plan["mode"]
    slot_date = common.parse_ts(plan["slot"]).date().isoformat()
    lines = [
        f"Scheduled Jarvis run in mode `{mode}`. Follow JARVIS.md section 16 for this mode.",
        "scripts/run.py has verified the guard, holds the run lock, and planned this run below.",
        "It owns state/runs.json, state/alerts.json, and the commit: do not edit them or commit.",
    ]
    if mode in schedule.REPORT_MODES:
        lines.append(f"Write the report to reports/{slot_date}-{mode}.md.")
        if plan.get("holiday"):
            lines.append("Today is a holiday: write the report file and do not post it.")
        if alerts:
            lines.append("Open alerts. Put each one verbatim on its own line right after the header:")
            lines += [f"  {text}" for text in alerts]
    lines += ["", "Plan:", "```json", json.dumps(plan, indent=2, sort_keys=True), "```", ""]
    return "\n".join(lines)


def capture_errors_since(root: Path, moment: datetime) -> int:
    errors = common.read_jsonl(root / "state" / "capture_errors.jsonl")
    return sum(1 for e in errors if e.get("at") and common.parse_ts(e["at"]) >= moment)


def report_posted_since(root: Path, moment: datetime) -> bool:
    messages = common.read_jsonl(root / "state" / "report_messages.jsonl")
    return any(
        m.get("kind") == "report" and m.get("at") and common.parse_ts(m["at"]) >= moment for m in messages
    )


def report_file(root: Path, plan: dict[str, Any]) -> Path:
    slot_date = common.parse_ts(plan["slot"]).date().isoformat()
    return root / "reports" / f"{slot_date}-{plan['mode']}.md"


def file_report_problems(root: Path, plan: dict[str, Any]) -> list[str]:
    """For file-only delivery the report file is the delivery: it must exist and pass report_check."""
    path = report_file(root, plan)
    if not path.exists():
        return [f"{path.relative_to(root)} was not written"]
    return report_check.check(path.read_text(), root)


def report_delivered(root: Path, plan: dict[str, Any], started: datetime) -> bool:
    """Whether this run delivered its report; file-only delivery alerts on a report that fails the check."""
    if (common.jarvis_config(root).get("delivery") or {}).get("posts"):
        return report_posted_since(root, started)
    problems = file_report_problems(root, plan)
    if problems:
        text = f"Alert: the {plan['mode']} report of {plan['run_id']} failed the report check: {problems[0]}"
        add_alert(root, "report_check", plan["run_id"], text)
    return not problems


def scheduled(root: Path, requested: str) -> int:
    config = common.jarvis_config(root)
    if requested not in schedule.MODES:
        print(f"run.py: unknown mode {requested}", file=sys.stderr)
        return 2
    if not config.get("bootstrapped"):
        print("run.py: this Jarvis is not bootstrapped; run scripts/jarvis bootstrap", file=sys.stderr)
        return 1
    is_report = requested in schedule.REPORT_MODES
    wait = float(config.get("lock_wait_minutes", 15)) * 60 if is_report else 0
    with file_lock(root / "state" / "run.lock", wait) as acquired:
        if not acquired:
            return lock_busy(root, requested, is_report)
        return locked_run(root, config, requested)


def make_plan(root: Path, requested: str, config: dict[str, Any]) -> dict[str, Any]:
    runs = common.read_json(schedule.runs_path(root), {"runs": []}).get("runs", [])
    return schedule.plan_now(root, requested, runs, config["schedule"])


def lock_busy(root: Path, requested: str, is_report: bool) -> int:
    if not is_report:
        print(f"run.py: another run holds the lock; {requested} skipped")
        return 0
    planned = make_plan(root, requested, common.jarvis_config(root))
    if planned["action"] == "skip":
        print(f"run.py: {planned['reason']}")
        return 0
    now = common.now()
    update_runs(root, lambda runs: schedule.start_run(runs, planned, now))
    update_runs(root, lambda runs: schedule.finish_run(runs, planned["run_id"], "skipped", now))
    text = f"Alert: the {planned['mode']} run {planned['run_id']} was skipped, another run held the lock."
    add_alert(root, "skipped", planned["run_id"], text)
    print(f"run.py: {text}")
    return 0


def preflight_failed(root: Path, attempt_id: str, problems: list[str]) -> int:
    write_last_error(root, f"Preflight failed for {attempt_id}", problems)
    add_alert(
        root,
        "preflight",
        attempt_id,
        f"Alert: guard preflight failed for {attempt_id}, see state/last_error.md.",
    )
    for problem in problems:
        print(f"run.py: {problem}", file=sys.stderr)
    return 1


def recover_dead_run(root: Path, dead: dict[str, Any]) -> Path | None:
    """Undo what a run that died mid-session left behind and alert on it; returns its leftover snapshot.

    A consolidate or dream gets memory/ back from its snapshot, the same as a failed one in finish(). Both
    steps repeat safely, so a run that dies here too recovers the same way next time.
    """
    run_id, mode = dead.get("run_id", "?"), dead.get("mode", "?")
    snapshot = root / "state" / "tmp" / f"memory-{run_id}"
    restored = mode in schedule.MEMORY_MODES and snapshot.is_dir()
    if restored:
        restore_memory(root, snapshot)
    undone = ", memory/ is back as it was before it" if restored else ""
    text = f"Alert: the {mode} run {run_id} was interrupted{undone}, see state/logs/{run_id}.log."
    add_alert(root, "interrupted", run_id, text)
    return snapshot if restored else None


def close_dead_and_start(root: Path, plan: dict[str, Any], started: datetime) -> None:
    """Recover and close every `running` entry (we hold the run lock, so each one died), then start `plan`."""
    runs = common.read_json(schedule.runs_path(root), {"runs": []}).get("runs", [])
    snapshots = [recover_dead_run(root, run) for run in runs if run.get("status") == "running"]

    def close_and_start(runs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for run in runs:
            if run.get("status") == "running":
                run.update(status="failed", finished=started.isoformat())
        return schedule.start_run(runs, plan, started)

    update_runs(root, close_and_start)
    for snapshot in snapshots:
        if snapshot is not None:
            shutil.rmtree(snapshot, ignore_errors=True)


def locked_run(root: Path, config: dict[str, Any], requested: str) -> int:
    started = common.now()
    attempt_id = f"{started.date().isoformat()}T{started.strftime('%H%M')}-{requested}"
    problems = preflight(root, config, requested, attempt_id)
    if problems:
        return preflight_failed(root, attempt_id, problems)

    plan = make_plan(root, requested, config)
    if plan["action"] == "skip":
        print(f"run.py: {plan['reason']}")
        return 0
    run_id, mode = plan["run_id"], plan["mode"]
    close_dead_and_start(root, plan, started)
    alerts_at_start = open_alerts(root)
    limits = bounds(config, mode, plan["first_run"])
    command = claude_command(
        config, limits["model"], int(limits["max_turns"]), guard.permission_rules(root, mode)
    )
    env = claude_env(root, config, mode, run_id)
    if plan.get("holiday"):
        env["JARVIS_UNSENT"] = "1"
    snapshot = snapshot_memory(root, run_id) if mode in schedule.MEMORY_MODES else None
    timeout = float(limits["timeout_minutes"]) * 60
    log = root / "state" / "logs" / f"{run_id}.log"
    try:
        status = run_session(command, session_prompt(plan, alerts_at_start), env, root, timeout, log)
    except OSError as error:
        status = "failed"
        with log.open("a") as out:
            out.write(f"[run.py] could not start {command[0]}: {error}\n")
    return finish(root, plan, status, started, snapshot, limits)


def settle_alerts(
    root: Path, plan: dict[str, Any], status: str, started: datetime, limits: dict[str, Any]
) -> None:
    """Alert on a timed out or failed session and on capture errors; a posted report clears older alerts."""
    run_id, mode = plan["run_id"], plan["mode"]
    if status == "timeout":
        minutes = limits["timeout_minutes"]
        text = f"Alert: the {mode} run {run_id} hit its {minutes} minute limit and was stopped."
        add_alert(root, "timeout", run_id, text)
    elif status == "failed":
        add_alert(
            root, "failed", run_id, f"Alert: the {mode} run {run_id} failed, see state/logs/{run_id}.log."
        )
    elif mode in schedule.REPORT_MODES and report_delivered(root, plan, started):
        clear_alerts_before(root, started)
    errors = capture_errors_since(root, started)
    if errors:
        text = f"Alert: {errors} raw capture errors during {run_id}, see state/capture_errors.jsonl."
        add_alert(root, "capture_errors", run_id, text)


def finish(
    root: Path,
    plan: dict[str, Any],
    status: str,
    started: datetime,
    snapshot: Path | None,
    limits: dict[str, Any],
) -> int:
    run_id, mode = plan["run_id"], plan["mode"]
    if snapshot is not None:
        if status != "ok":
            restore_memory(root, snapshot)
        shutil.rmtree(snapshot, ignore_errors=True)
    settle_alerts(root, plan, status, started, limits)
    update_runs(root, lambda runs: schedule.finish_run(runs, run_id, status, common.now()))
    try:
        commit_data(root, f"{mode} {started.date().isoformat()} {started.strftime('%H:%M')}")
    except RunError as error:
        write_last_error(root, f"Commit failed for {run_id}", [str(error)])
        print(f"run.py: {error}", file=sys.stderr)
        return 1
    return 0 if status == "ok" else 1


def open_session(root: Path, mode: str) -> int:
    """Verify the guard, then replace this process with an interactive Claude Code session in `mode`."""
    if mode not in INTERACTIVE_MODES:
        print(f"run.py open: mode must be one of {', '.join(INTERACTIVE_MODES)}", file=sys.stderr)
        return 2
    config = common.jarvis_config(root)
    if not config.get("bootstrapped") and mode not in UNBOOTSTRAPPED_MODES:
        print("jarvis: not bootstrapped yet; start with scripts/jarvis bootstrap", file=sys.stderr)
        return 1
    problems = guard_pin.verify(root) or guard_pin.self_test(root)
    if problems:
        for problem in problems:
            print(f"jarvis: {problem}", file=sys.stderr)
        return 1
    env = claude_env(root, config, mode, f"{mode}-{common.now().date().isoformat()}")
    command = [os.path.expanduser(config["claude_bin"])]
    if mode == "bootstrap":
        command.append("/bootstrap")
    if mode != "dev":
        command += ["--allowedTools", *guard.permission_rules(root, mode)]
    os.chdir(root)
    os.execve(command[0], command, env)


def main(argv: list[str]) -> int:
    root = common.repo_root()
    if len(argv) == 2 and argv[0] == "open":
        return open_session(root, argv[1])
    if len(argv) == 1:
        return scheduled(root, argv[0])
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
