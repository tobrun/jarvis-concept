"""Shared fixtures: a temp repo rendered from the example profile, and a runner for scripts as subprocesses.

Tests never read the developer's own profile or config/: every repo fixture is the example profile
(examples/profile.example.yaml) rendered once per session by scripts/bootstrap.py, then copied per test.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
EXAMPLE_PROFILE = REPO / "examples" / "profile.example.yaml"
# The clock the session's render runs at, so the generated holidays cover the years tests use.
RENDER_NOW = "2026-10-27T09:00:00"
SHIPPED = ("scripts", "adapters", "templates", ".claude", "examples")


def copy_shipped(root: Path) -> None:
    """The tracked parts of the kit a fresh clone has, without caches."""
    for name in SHIPPED:
        if (REPO / name).exists():
            shutil.copytree(REPO / name, root / name, ignore=shutil.ignore_patterns("__pycache__"))
    for name in ("CLAUDE.md", "pyproject.toml"):
        shutil.copy2(REPO / name, root / name)


def render(root: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(root / "scripts" / "bootstrap.py"), "render"],
        env={**os.environ, "JARVIS_ROOT": str(root), "JARVIS_NOW": RENDER_NOW},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.fixture(scope="session")
def rendered(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The kit with the example profile rendered: config/, JARVIS.md, OWNER.md, scheduler units."""
    root = tmp_path_factory.mktemp("rendered") / "jarvis"
    root.mkdir()
    copy_shipped(root)
    (root / "profile").mkdir()
    shutil.copy2(EXAMPLE_PROFILE, root / "profile" / "jarvis.profile.yaml")
    render(root)
    return root


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, rendered: Path) -> Path:
    """A throwaway repo rendered from the example profile; JARVIS_ROOT points at it."""
    root = tmp_path / "jarvis"
    shutil.copytree(rendered, root, symlinks=True)
    for name in ("raw", "memory", "state", "reports", "drafts", "curation", "eval"):
        (root / name).mkdir(exist_ok=True)
    monkeypatch.setenv("JARVIS_ROOT", str(root))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv("JARVIS_NOW", raising=False)
    monkeypatch.delenv("JARVIS_MODE", raising=False)
    monkeypatch.delenv("JARVIS_RUN_ID", raising=False)
    return root


def run_script(
    root: Path, name: str, *args: str, stdin: str | dict | None = None, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run scripts/<name> from the given repo root with the test interpreter, as the agent or a hook would."""
    if isinstance(stdin, dict):
        stdin = json.dumps(stdin)
    full_env = {**os.environ, "JARVIS_ROOT": str(root), **(env or {})}
    script = root / "scripts" / name
    command = [sys.executable, str(script), *args] if name.endswith(".py") else [str(script), *args]
    return subprocess.run(command, input=stdin, capture_output=True, text=True, env=full_env, check=False)
