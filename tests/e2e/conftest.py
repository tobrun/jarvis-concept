"""The e2e harness: a real git repo copy of Jarvis whose claude_bin is tests/e2e/fake_claude.py."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import common
import pytest
import yaml
from conftest import REPO, run_script

NOW = "2026-10-26T08:04:00+01:00"
COPIED = (".gitignore", "uv.lock", ".python-version")


@dataclass
class Jarvis:
    root: Path
    work: Path

    def script(self, steps: list[dict[str, Any]]) -> None:
        (self.work / "script.json").write_text(json.dumps(steps))

    def run(self, mode: str, now: str = NOW) -> subprocess.CompletedProcess[str]:
        return run_script(self.root, "run.py", mode, env={"JARVIS_NOW": now})

    def records(self) -> list[dict[str, Any]]:
        return common.read_jsonl(self.work / "fake.log")

    def calls(self) -> list[dict[str, Any]]:
        return [r for r in self.records() if "tool" in r and r["tool"] != "WebFetch"]

    def sessions(self) -> list[dict[str, Any]]:
        return [r for r in self.records() if "plan" in r and not r["run_id"].endswith("-canary")]

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.root), *args], capture_output=True, text=True, check=True
        ).stdout

    def runs(self) -> list[dict[str, Any]]:
        return common.read_json(self.root / "state" / "runs.json", {"runs": []})["runs"]

    def alerts(self) -> list[dict[str, Any]]:
        return common.read_json(self.root / "state" / "alerts.json", {"alerts": []})["alerts"]

    def configure(self, change) -> None:
        """Edit config/jarvis.yaml and re-pin the guard, as the owner would in dev mode."""
        path = self.root / "config" / "jarvis.yaml"
        config = yaml.safe_load(path.read_text())
        change(config)
        path.write_text(yaml.safe_dump(config, sort_keys=False))
        assert run_script(self.root, "guard_pin.py", "pin").returncode == 0


def snapshot(jarvis: Jarvis) -> dict[str, Any]:
    """The observable state of a run: what the e2e report shows before and after each scenario."""
    root = jarvis.root
    log = jarvis.git("log", "--format=%s")
    return {
        "runs": jarvis.runs(),
        "alerts": jarvis.alerts(),
        "denied_calls": [
            {k: d.get(k) for k in ("tool", "mode", "run_id", "reason")}
            for d in common.read_jsonl(root / "state" / "denied_calls.jsonl")
        ],
        "raw_files": sorted(str(p.relative_to(root)) for p in (root / "raw").rglob("*.jsonl")),
        "reports": sorted(p.name for p in (root / "reports").glob("*.md")),
        "eval": sorted(str(p.relative_to(root)) for p in (root / "eval").rglob("*") if p.is_file()),
        "last_error": (root / "state" / "last_error.md").exists(),
        "commits": log.splitlines(),
        "agent_calls": [(c["tool"], c["outcome"]) for c in jarvis.calls()],
    }


@pytest.fixture
def jarvis(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    work = tmp_path / "work"
    (work / "bin").mkdir(parents=True)
    fake = work / "bin" / "claude"
    fake.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{REPO / "tests" / "e2e" / "fake_claude.py"}" "$@"\n'
    )
    fake.chmod(0o755)
    # The fake agent's `uv run scripts/x.py` runs the script with the test interpreter: no real uv sync.
    uv = work / "bin" / "uv"
    uv.write_text(f'#!/bin/sh\n[ "$1" = run ] && shift\nexec "{sys.executable}" "$@"\n')
    uv.chmod(0o755)
    monkeypatch.setenv("PATH", f"{work / 'bin'}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_SCRIPT", str(work / "script.json"))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(work / "fake.log"))
    (work / "claude-config").mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(work / "claude-config"))

    for name in COPIED:
        if (REPO / name).exists():
            shutil.copy2(REPO / name, repo / name)
    (repo / ".venv").symlink_to(REPO / ".venv")
    for name in ("memory", "drafts", "reports", "curation", "eval"):
        (repo / name / ".keep").write_text("")
    (repo / "memory" / "MEMORY.md").write_text("# Memory\n")
    jarvis = Jarvis(repo, work)

    def settings(config: dict[str, Any]) -> None:
        config.update(
            claude_bin=str(fake),
            claude_config_dir=str(work / "claude-config"),
            lock_wait_minutes=0.1,
        )
        for mode in config["modes"]:
            config["modes"][mode]["timeout_minutes"] = 0.25

    jarvis.configure(settings)
    for command in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "e2e@example.com"],
        ["config", "user.name", "e2e"],
        ["add", "-A"],
        ["commit", "-q", "-m", "baseline"],
    ):
        subprocess.run(["git", "-C", str(repo), *command], check=True, capture_output=True)
    jarvis.script([])
    evidence = os.environ.get("E2E_EVIDENCE_DIR")
    before = snapshot(jarvis) if evidence else None
    yield jarvis
    if evidence:
        record = {"test": request.node.name, "before": before, "after": snapshot(jarvis)}
        Path(evidence, f"{request.node.name}.json").write_text(json.dumps(record, indent=2, default=str))
