"""Replay a past run, or eval the golden week, in a throwaway copy of the repo with no network.

Usage:
  replay.py replay <YYYY-MM-DD> <mode>     replay one committed run; writes eval/replays/<date>-<mode>.md
                                           and eval/replays/<date>-<mode>.candidates.jsonl
  replay.py eval <YYYY-Www> [--keep]       replay the 15 report runs of a frozen golden week; writes
                                           eval/last.json
  replay.py eval --accept                  copy eval/last.json to eval/baseline.json

The live guard is verified and self-tested first. Each replayed session runs in a temp copy:
  memory/ state/ curation/ drafts/ reports/   from the commit before the run (`replay`) or the golden
                                              week's start_commit (`eval`)
  raw/                                        live raw (golden raw for `eval`) cut at the run's finish, so
                                              the run's own fetches are included and later ones are not,
                                              with the live tombstone registry applied to every line
  state/tombstones.jsonl                      the live registry, so a tombstoned id never resolves in the copy
  JARVIS.md OWNER.md CLAUDE.md .claude/ scripts/ config/ pyproject.toml uv.lock .python-version,
  state/guard.sha256
                                              the current working tree, so a change is replayed before it
                                              is committed
  .venv                                       a link to the live one; UV_NO_SYNC keeps uv from touching it
It runs in mode `replay` (no source, post, GitHub, or web tool; writes only inside the copy) with
JARVIS_NOW at the original run's start, UV_OFFLINE=1 and HF_HUB_OFFLINE=1.
Only eval/replays/ and eval/last.json are written in the live repo.

Eval scoring: the golden outcomes keep their type and time; their surfaced_at is recomputed as the slot
times of the replayed runs whose report or surfaced candidates cite one of the outcome's raw ids, then
metrics.py computes bump recall and the miss count. eval/last.json carries the working-tree gated_hash,
which equals the staged hash once the change is staged (the pre-commit eval gate compares them).
"""

from __future__ import annotations

import difflib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import common
import eval_gate
import guard
import guard_pin
import metrics
import raw
import report_check
import run
import schedule
from bizdays import is_business_day

COPIED_FROM_COMMIT = ("memory", "state", "curation", "drafts", "reports")
OVERLAID = (
    "JARVIS.md",
    "OWNER.md",
    "CLAUDE.md",
    ".claude",
    "scripts",
    "config",
    "pyproject.toml",
    "uv.lock",
    ".python-version",
)


class ReplayError(Exception):
    pass


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ReplayError(f"git {' '.join(args)}: {result.stderr.strip()}")
    return result.stdout


def find_run_commit(root: Path, day: str, mode: str) -> str:
    """The newest commit whose subject is `<mode> <day> <HH:MM>`, as run.py writes it."""
    for line in git(root, "log", "--format=%H %s").splitlines():
        commit, _, subject = line.partition(" ")
        parts = subject.split(" ")
        if len(parts) == 3 and parts[0] == mode and parts[1] == day:
            return commit
    raise ReplayError(f"no commit `{mode} {day} <time>` found")


def run_record(root: Path, commit: str, day: str, mode: str) -> dict[str, Any]:
    """The run's entry in the state/runs.json that its own commit recorded."""
    runs = json.loads(git(root, "show", f"{commit}:state/runs.json")).get("runs", [])
    matches = [r for r in runs if r.get("mode") == mode and r.get("slot_date") == day and r.get("finished")]
    if not matches:
        raise ReplayError(f"no finished {mode} run for {day} in state/runs.json at {commit[:10]}")
    return matches[-1]


def extract(root: Path, commit: str, paths: tuple[str, ...], dest: Path) -> None:
    present = [p for p in paths if git(root, "ls-tree", "--name-only", commit, p).strip()]
    for path in paths:
        (dest / path).mkdir(parents=True, exist_ok=True)
    if not present:
        return
    archive = subprocess.run(
        ["git", "-C", str(root), "archive", "--format=tar", commit, *present], capture_output=True, check=True
    )
    subprocess.run(["tar", "-x", "-C", str(dest)], input=archive.stdout, check=True)


def fetched_by(path: Path, cutoff: datetime) -> list[dict[str, Any]]:
    """The lines of one raw file fetched at or before `cutoff`."""
    return [
        line
        for line in common.read_jsonl(path)
        if line.get("fetched_at") and common.parse_ts(line["fetched_at"]) <= cutoff
    ]


def copy_raw(
    source_raw: Path, dest_raw: Path, cutoff: datetime, registry: list[dict[str, Any]], root: Path
) -> int:
    """Copy raw lines fetched at or before `cutoff`, redacted by the tombstone registry; returns how many.

    Raw ids are parsed with the live repo's config/sources.yaml (`root`), wherever the raw comes from.
    """
    kept = 0
    for path in sorted(source_raw.glob("*/*.jsonl")):
        lines = [raw.apply_registry(line, registry, root) for line in fetched_by(path, cutoff)]
        if lines:
            target = dest_raw / path.parent.name / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(
                "".join(json.dumps(line, ensure_ascii=False, sort_keys=True) + "\n" for line in lines)
            )
            kept += len(lines)
    return kept


def overlay_current(root: Path, copy: Path) -> None:
    """The working tree's prompt, scripts, and config, the live guard pin, and the live tombstone registry."""
    for name in OVERLAID:
        source = root / name
        if source.is_dir():
            shutil.copytree(
                source, copy / name, ignore=shutil.ignore_patterns("__pycache__"), dirs_exist_ok=True
            )
        elif source.exists():
            shutil.copy2(source, copy / name)
    shutil.copy2(root / "state" / "guard.sha256", copy / "state" / "guard.sha256")
    # The live registry, a superset of the commit's: an item tombstoned since stays redacted and uncitable.
    if raw.tombstones_path(root).exists():
        shutil.copy2(raw.tombstones_path(root), copy / "state" / "tombstones.jsonl")


def build_copy(root: Path, commit: str, raw_source: Path, cutoff: datetime) -> Path:
    copy = Path(tempfile.mkdtemp(prefix="jarvis-replay-")).resolve()
    extract(root, commit, COPIED_FROM_COMMIT, copy)
    overlay_current(root, copy)
    for leftover in ("runs.lock", "run.lock"):
        (copy / "state" / leftover).unlink(missing_ok=True)
    (copy / "raw").mkdir(exist_ok=True)
    copy_raw(raw_source, copy / "raw", cutoff, raw.registry(root), root)
    for name in ("eval/replays", "state/tmp"):
        (copy / name).mkdir(parents=True, exist_ok=True)
    (copy / ".venv").symlink_to(os.path.realpath(root / ".venv"))
    return copy


def replay_env(copy: Path, config: dict[str, Any], run_id: str, now: datetime) -> dict[str, str]:
    env = run.claude_env(copy, config, "replay", run_id)
    env.update(JARVIS_NOW=now.isoformat(), UV_OFFLINE="1", HF_HUB_OFFLINE="1", UV_NO_SYNC="1")
    return env


def replay_prompt(original: dict[str, Any], day: str, mode: str) -> str:
    plan = {
        "mode": mode,
        "replay_of": original["run_id"],
        "slot_date": day,
        "started": original["started"],
        "report_path": f"reports/{day}-{mode}.md",
    }
    lines = [
        f"Jarvis replay of the {mode} run {original['run_id']}. Follow JARVIS.md section 16 for replay.",
        "The network is off: work from raw/ only and never post.",
        "Write the report file and the candidates as the original run would have.",
        f"Write the report to reports/{day}-{mode}.md.",
        "",
        "Plan:",
        "```json",
        json.dumps(plan, indent=2, sort_keys=True),
        "```",
        "",
    ]
    return "\n".join(lines)


def replay_session(
    root: Path, copy: Path, original: dict[str, Any], day: str, mode: str, now: datetime
) -> tuple[str, list[dict[str, Any]]]:
    """Run one replay session in `copy`; returns its status and the guard denials it logged."""
    config = common.jarvis_config(root)
    limits = config["modes"]["replay"]
    run_id = f"replay-{original['run_id']}"
    command = run.claude_command(
        config, limits["model"], int(limits["max_turns"]), guard.permission_rules(copy, "replay")
    )
    log = root / "state" / "logs" / f"{run_id}.log"
    env = replay_env(copy, config, run_id, now)
    try:
        status = run.run_session(
            command, replay_prompt(original, day, mode), env, copy, float(limits["timeout_minutes"]) * 60, log
        )
    except OSError as error:
        raise ReplayError(f"could not start {command[0]}: {error}") from error
    denied = [
        d for d in common.read_jsonl(copy / "state" / "denied_calls.jsonl") if d.get("run_id") == run_id
    ]
    return status, denied


def check_live_guard(root: Path) -> None:
    problems = guard_pin.verify(root) or guard_pin.self_test(root)
    if problems:
        raise ReplayError("guard preflight failed: " + "; ".join(problems))


def diff_text(original: str, replayed: str, day: str, mode: str) -> str:
    lines = difflib.unified_diff(
        original.splitlines(keepends=True),
        replayed.splitlines(keepends=True),
        fromfile=f"original reports/{day}-{mode}.md",
        tofile=f"replayed reports/{day}-{mode}.md",
    )
    return "".join(lines)


def report_pair(root: Path, commit: str, copy: Path, day: str, mode: str) -> tuple[str, str]:
    """The original run's report at `commit` and the replayed one in `copy`; empty when one is missing."""
    try:
        original = git(root, "show", f"{commit}:reports/{day}-{mode}.md")
    except ReplayError:
        original = ""
    replayed = copy / "reports" / f"{day}-{mode}.md"
    return original, replayed.read_text() if replayed.exists() else ""


def replay_summary(
    original: dict[str, Any], commit: str, status: str, denied: list[dict[str, Any]], diff: str
) -> str:
    summary = [
        f"# Replay of {original['run_id']}",
        "",
        f"Replayed at {common.now().isoformat()} from commit {commit[:10]}; session {status}.",
        f"Denied calls during replay: {len(denied)}"
        + (f" ({', '.join(sorted({d.get('tool', '?') for d in denied}))})" if denied else "")
        + ".",
        "Report diff: none." if not diff else "Report diff:",
        "",
    ]
    if diff:
        summary += ["```diff", diff.rstrip("\n"), "```", ""]
    return "\n".join(summary)


def replay(root: Path, day: str, mode: str, keep: bool = False) -> Path:
    if mode not in schedule.REPORT_MODES:
        raise ReplayError(f"only report runs are replayed, not {mode}")
    check_live_guard(root)
    commit = find_run_commit(root, day, mode)
    original = run_record(root, commit, day, mode)
    parent = git(root, "rev-parse", f"{commit}^").strip()
    copy = build_copy(root, parent, root / "raw", common.parse_ts(original["finished"]))
    try:
        status, denied = replay_session(root, copy, original, day, mode, common.parse_ts(original["started"]))
        out_dir = root / "eval" / "replays"
        out_dir.mkdir(parents=True, exist_ok=True)
        candidates = copy / "state" / "candidates" / f"{day}-{mode}.jsonl"
        if candidates.exists():
            shutil.copy2(candidates, out_dir / f"{day}-{mode}.candidates.jsonl")
        diff = diff_text(*report_pair(root, commit, copy, day, mode), day, mode)
        target = out_dir / f"{day}-{mode}.md"
        target.write_text(replay_summary(original, commit, status, denied, diff))
        return target
    finally:
        if not keep:
            shutil.rmtree(copy, ignore_errors=True)


def golden_runs(week: str, config: dict[str, Any], root: Path) -> list[tuple[str, str, datetime, datetime]]:
    """(date, mode, now, raw cutoff) for every report slot on the business days of the week."""
    monday = metrics.week_monday(week)
    holidays = common.holidays(root)
    live_runs = common.read_json(schedule.runs_path(root), {"runs": []}).get("runs", [])
    planned = []
    for offset in range(7):
        day = monday + timedelta(days=offset)
        if not is_business_day(day, holidays):
            continue
        for mode, slot in schedule.report_slots(day, config["schedule"]).items():
            planned.append((day.isoformat(), mode, slot, raw_cutoff(live_runs, mode, slot, config)))
    return planned


def raw_cutoff(
    live_runs: list[dict[str, Any]], mode: str, slot: datetime, config: dict[str, Any]
) -> datetime:
    """When the live run of this slot finished ok, else the slot plus the mode's wall clock."""
    day = slot.date().isoformat()
    finished = [
        common.parse_ts(r["finished"])
        for r in live_runs
        if r.get("mode") == mode and r.get("slot_date") == day and r.get("status") == "ok"
    ]
    limit = timedelta(minutes=float(config["modes"][mode]["timeout_minutes"]))
    return finished[-1] if finished else slot + limit


def cited_ids(copy: Path, day: str, mode: str) -> set[str]:
    """Raw ids the replayed run surfaced: cited in its report or surfaced in its candidates."""
    found: set[str] = set()
    report = copy / "reports" / f"{day}-{mode}.md"
    if report.exists():
        found.update(report_check.RAW_ID.findall(report.read_text()))
    for candidate in common.read_jsonl(copy / "state" / "candidates" / f"{day}-{mode}.jsonl"):
        if candidate.get("decision") == "surface" and candidate.get("raw_id"):
            found.add(candidate["raw_id"])
    return found


def rescore(
    outcomes: list[dict[str, Any]], surfaced: list[tuple[datetime, set[str]]]
) -> list[dict[str, Any]]:
    rescored = []
    for outcome in outcomes:
        ids = set(outcome.get("raw_ids") or [])
        times = sorted(moment.isoformat() for moment, cited in surfaced if ids & cited)
        rescored.append({**outcome, "surfaced_at": times})
    return rescored


def evaluate(root: Path, week: str, keep: bool = False) -> dict[str, Any]:
    golden = root / "eval" / "golden" / week
    manifest = common.read_json(golden / "manifest.json", None)
    if manifest is None:
        raise ReplayError(f"no golden week at {golden}; freeze one with scripts/freeze_golden.py")
    check_live_guard(root)
    config = common.jarvis_config(root)
    surfaced: list[tuple[datetime, set[str]]] = []
    sessions = []
    for day, mode, now, cutoff in golden_runs(week, config, root):
        copy = build_copy(root, manifest["start_commit"], golden / "raw", cutoff)
        try:
            original = {"run_id": f"{day}T{now.strftime('%H%M')}-{mode}", "started": now.isoformat()}
            status, denied = replay_session(root, copy, original, day, mode, now)
            surfaced.append((now, cited_ids(copy, day, mode)))
            sessions.append({"run_id": original["run_id"], "status": status, "denied": len(denied)})
        finally:
            if not keep:
                shutil.rmtree(copy, ignore_errors=True)
    outcomes = rescore(common.read_jsonl(golden / "outcomes.jsonl"), surfaced)
    by_cause = metrics.misses_by_cause(outcomes)
    result = {
        "week": week,
        "at": common.now().isoformat(),
        "start_commit": manifest["start_commit"],
        "gated_hash": eval_gate.gated_hash(root, staged=False),
        "bump_recall": metrics.bump_recall(outcomes),
        "miss_count": sum(by_cause.values()),
        "misses_by_cause": by_cause,
        "noise_rate": metrics.noise_rate(outcomes),
        "sessions": sessions,
    }
    common.write_json(root / "eval" / "last.json", result)
    return result


def accept(root: Path) -> None:
    last = root / "eval" / "last.json"
    if not last.exists():
        raise ReplayError("no eval/last.json to accept; run `replay.py eval <YYYY-Www>` first")
    shutil.copy2(last, root / "eval" / "baseline.json")


def main(argv: list[str]) -> int:
    root = common.repo_root()
    keep = "--keep" in argv
    args = [a for a in argv if a != "--keep"]
    try:
        return command(root, args, keep)
    except (ReplayError, ValueError) as error:
        print(f"replay.py: {error}", file=sys.stderr)
        return 1


def command(root: Path, args: list[str], keep: bool) -> int:
    """Run the command `args` names; 2 with the usage when it names none."""
    if len(args) == 3 and args[0] == "replay":
        date.fromisoformat(args[1])
        print(replay(root, args[1], args[2], keep))
    elif args == ["eval", "--accept"]:
        accept(root)
        print("eval/baseline.json updated from eval/last.json")
    elif len(args) == 2 and args[0] == "eval":
        print(json.dumps(evaluate(root, args[1], keep), indent=2, sort_keys=True))
    else:
        print(__doc__, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
