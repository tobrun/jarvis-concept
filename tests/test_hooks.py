"""The eval gate as git runs it: real repos, hooks installed through core.hooksPath, real `git commit`."""

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import eval_gate
import pytest

REPO = Path(__file__).resolve().parent.parent


def git(root, *args, check=True):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, env=env, check=check
    )


@pytest.fixture
def gated_repo(tmp_path, rendered):
    """A git repo with the gate's scripts and hooks, the test interpreter's venv, and one initial commit."""
    root = tmp_path / "jarvis"
    (root / "scripts" / "hooks").mkdir(parents=True)
    (root / "config").mkdir()
    for name in ("eval_gate.py", "common.py"):
        shutil.copy2(REPO / "scripts" / name, root / "scripts" / name)
    for name in ("pre-commit", "commit-msg"):
        shutil.copy2(REPO / "scripts" / "hooks" / name, root / "scripts" / "hooks" / name)
    shutil.copy2(rendered / "config" / "jarvis.yaml", root / "config" / "jarvis.yaml")
    (root / "JARVIS.md").write_text("# Jarvis\n")
    (root / ".gitignore").write_text(".venv\n")
    # The hooks run .venv/bin/python; link the whole test venv so its site-packages (pyyaml) come along.
    (root / ".venv").symlink_to(sys.prefix, target_is_directory=True)
    git(root, "init", "-q")
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Test")
    git(root, "add", ".")
    git(root, "commit", "-q", "--no-verify", "-m", "initial")
    git(root, "config", "core.hooksPath", "scripts/hooks")
    return root


def freeze_golden_week(root):
    golden = root / "eval" / "golden" / "2026-W44"
    golden.mkdir(parents=True)
    (golden / "manifest.json").write_text('{"week": "2026-W44", "start_commit": null, "frozen_at": "x"}\n')


def write_eval(root, name, **values):
    (root / "eval").mkdir(exist_ok=True)
    (root / "eval" / name).write_text(json.dumps(values))


def stage_jarvis_change(root):
    (root / "JARVIS.md").write_text("# Jarvis\n\nA new rule.\n")
    git(root, "add", "JARVIS.md")


def commit(root, message="change"):
    return git(root, "commit", "-q", "-m", message, check=False)


def head_subject(root):
    return git(root, "log", "-1", "--format=%s").stdout.strip()


def test_commit_staging_a_script_without_a_golden_week_is_allowed_with_a_notice(gated_repo):
    (gated_repo / "scripts" / "x.py").write_text("print('x')\n")
    git(gated_repo, "add", "scripts/x.py")

    result = commit(gated_repo, "add x")

    assert result.returncode == 0, result.stderr
    assert "eval gate open: no golden week frozen yet" in result.stderr
    assert head_subject(gated_repo) == "add x"


def test_commit_staging_jarvis_md_with_a_golden_week_and_no_eval_is_blocked(gated_repo):
    freeze_golden_week(gated_repo)
    stage_jarvis_change(gated_repo)

    result = commit(gated_repo, "new rule\n\neval: recall +0.00")

    assert result.returncode == 1
    assert "no eval/last.json" in result.stderr
    assert head_subject(gated_repo) == "initial"


def test_commit_whose_eval_was_made_for_other_content_is_blocked(gated_repo):
    freeze_golden_week(gated_repo)
    write_eval(gated_repo, "baseline.json", bump_recall=0.8, miss_count=3)
    write_eval(gated_repo, "last.json", gated_hash="0" * 64, bump_recall=0.8, miss_count=3)
    stage_jarvis_change(gated_repo)

    result = commit(gated_repo, "new rule\n\neval: recall +0.00")

    assert result.returncode == 1
    assert "different content than what is staged" in result.stderr
    assert head_subject(gated_repo) == "initial"


def test_commit_whose_recall_drops_past_the_tolerance_is_blocked(gated_repo):
    freeze_golden_week(gated_repo)
    stage_jarvis_change(gated_repo)
    staged = eval_gate.gated_hash(gated_repo)
    write_eval(gated_repo, "baseline.json", bump_recall=0.8, miss_count=3)
    write_eval(gated_repo, "last.json", gated_hash=staged, bump_recall=0.7, miss_count=3)

    result = commit(gated_repo, "new rule\n\neval: recall -0.10")

    assert result.returncode == 1
    assert "bump recall 0.700 is below baseline 0.800 minus tolerance 0.05" in result.stderr
    assert head_subject(gated_repo) == "initial"


def test_gated_commit_with_a_passing_eval_and_an_eval_line_lands(gated_repo):
    freeze_golden_week(gated_repo)
    stage_jarvis_change(gated_repo)
    staged = eval_gate.gated_hash(gated_repo)
    write_eval(gated_repo, "baseline.json", bump_recall=0.8, miss_count=3)
    write_eval(gated_repo, "last.json", gated_hash=staged, bump_recall=0.76, miss_count=3)

    result = commit(gated_repo, "new rule\n\neval: recall -0.04, misses +0")

    assert result.returncode == 0, result.stderr
    assert head_subject(gated_repo) == "new rule"


def test_commit_staging_only_memory_is_allowed_silently(gated_repo):
    freeze_golden_week(gated_repo)
    (gated_repo / "memory").mkdir()
    (gated_repo / "memory" / "today.md").write_text("notes\n")
    git(gated_repo, "add", "memory/today.md")

    result = commit(gated_repo, "memory")

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert head_subject(gated_repo) == "memory"


def test_gated_commit_message_without_an_eval_line_is_blocked_by_commit_msg(gated_repo):
    freeze_golden_week(gated_repo)
    stage_jarvis_change(gated_repo)
    staged = eval_gate.gated_hash(gated_repo)
    write_eval(gated_repo, "baseline.json", bump_recall=0.8, miss_count=3)
    write_eval(gated_repo, "last.json", gated_hash=staged, bump_recall=0.8, miss_count=3)

    result = commit(gated_repo, "new rule\n\nno delta stated")

    assert result.returncode == 1
    assert "needs a line starting with `eval:`" in result.stderr
    assert head_subject(gated_repo) == "initial"


def test_working_tree_hash_matches_the_staged_hash_once_the_change_is_staged(gated_repo):
    (gated_repo / "scripts" / "x.py").write_text("print('x')\n")
    before_staging = eval_gate.gated_hash(gated_repo, staged=False)

    git(gated_repo, "add", "scripts/x.py")

    assert eval_gate.gated_hash(gated_repo, staged=True) == before_staging


def test_commit_without_an_accepted_baseline_is_blocked(gated_repo):
    freeze_golden_week(gated_repo)
    stage_jarvis_change(gated_repo)
    write_eval(gated_repo, "last.json", gated_hash=eval_gate.gated_hash(gated_repo), bump_recall=0.8)

    result = commit(gated_repo, "new rule\n\neval: recall +0.00")

    assert result.returncode == 1
    assert "no eval/baseline.json" in result.stderr
    assert head_subject(gated_repo) == "initial"


def test_commit_whose_miss_count_rises_above_baseline_is_blocked(gated_repo):
    freeze_golden_week(gated_repo)
    stage_jarvis_change(gated_repo)
    staged = eval_gate.gated_hash(gated_repo)
    write_eval(gated_repo, "baseline.json", bump_recall=0.8, miss_count=3)
    write_eval(gated_repo, "last.json", gated_hash=staged, bump_recall=0.9, miss_count=4)

    result = commit(gated_repo, "new rule\n\neval: recall +0.10, misses +1")

    assert result.returncode == 1
    assert "miss count 4 is above baseline 3" in result.stderr
    assert head_subject(gated_repo) == "initial"


def test_hook_called_without_a_known_hook_or_message_file_prints_usage_and_exits_2(capsys):
    for argv in ([], ["post-commit"], ["commit-msg"], ["commit-msg", "a", "b"]):
        assert eval_gate.main(argv) == 2
    assert "Usage" in capsys.readouterr().err


def test_working_tree_hash_records_an_executable_script_as_git_would(gated_repo):
    tool = gated_repo / "scripts" / "tool.sh"
    tool.write_text("#!/bin/sh\n")
    tool.chmod(0o755)
    before_staging = eval_gate.gated_hash(gated_repo, staged=False)

    git(gated_repo, "add", "scripts/tool.sh")

    assert eval_gate.gated_hash(gated_repo, staged=True) == before_staging
    assert git(gated_repo, "ls-files", "-s", "scripts/tool.sh").stdout.startswith("100755 ")


def test_working_tree_hash_without_gated_files_is_the_hash_of_nothing(tmp_path):
    git(tmp_path, "init", "-q")

    assert eval_gate.gated_hash(tmp_path, staged=False) == hashlib.sha256(b"").hexdigest()
