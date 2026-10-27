import json

import pytest
from conftest import REPO, run_script


@pytest.fixture
def root(repo, tmp_path, monkeypatch):
    home = tmp_path / "claude-config"
    home.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(home))
    (repo / ".venv").symlink_to(REPO / ".venv")
    (repo / "JARVIS.md").write_text("prompt\n")
    assert run_script(repo, "guard_pin.py", "pin").returncode == 0
    return repo


def verify(root):
    return run_script(root, "guard_pin.py", "verify")


def test_pin_then_verify_passes(root):
    result = verify(root)
    assert result.returncode == 0, result.stderr


def test_verify_after_editing_the_allowlist_fails_naming_the_file(root):
    allowlist = root / "config" / "allowed_tools.yaml"
    allowlist.write_text(allowlist.read_text() + "\n# widened\n")
    result = verify(root)
    assert result.returncode == 1
    assert "config/allowed_tools.yaml" in result.stderr


def test_verify_after_editing_common_fails(root):
    common_py = root / "scripts" / "common.py"
    common_py.write_text(common_py.read_text() + "\n")
    result = verify(root)
    assert result.returncode == 1
    assert "scripts/common.py" in result.stderr


def test_settings_local_json_fails_verify(root):
    (root / ".claude" / "settings.local.json").write_text("{}")
    result = verify(root)
    assert result.returncode == 1
    assert "settings.local.json" in result.stderr


def test_disable_all_hooks_in_the_config_dir_fails_verify(root, tmp_path):
    (tmp_path / "claude-config" / "settings.json").write_text(json.dumps({"disableAllHooks": True}))
    result = verify(root)
    assert result.returncode == 1
    assert "disableAllHooks" in result.stderr


def test_unreadable_settings_in_the_config_dir_fails_verify(root, tmp_path):
    (tmp_path / "claude-config" / "settings.json").write_text("{not json")
    result = verify(root)
    assert result.returncode == 1
    assert "settings.json is unreadable" in result.stderr


def test_missing_pin_file_fails_verify(root):
    (root / "state" / "guard.sha256").unlink()
    result = verify(root)
    assert result.returncode == 1
    assert "guard.sha256 is missing" in result.stderr


def test_self_test_passes_when_the_guard_denies_the_forbidden_call(root):
    result = run_script(root, "guard_pin.py", "self-test")
    assert result.returncode == 0, result.stderr


def test_self_test_fails_with_a_guard_that_allows_everything(root):
    (root / "scripts" / "guard.py").write_text("import sys\nsys.exit(0)\n")
    result = run_script(root, "guard_pin.py", "self-test")
    assert result.returncode == 1
    assert "did not deny" in result.stderr


def test_self_test_fails_with_a_guard_that_denies_everything(root):
    (root / "scripts" / "guard.py").write_text("import sys\nsys.exit(2)\n")
    result = run_script(root, "guard_pin.py", "self-test")
    assert result.returncode == 1
    assert "denied a Read" in result.stderr


def test_unknown_command_prints_usage_and_exits_2(root):
    result = run_script(root, "guard_pin.py", "unpin")
    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_pin_then_verify_passes_on_a_freshly_rendered_repo(repo):
    env = {"CLAUDE_CONFIG_DIR": "/nonexistent"}
    assert run_script(repo, "guard_pin.py", "pin").returncode == 0
    result = run_script(repo, "guard_pin.py", "verify", env=env)
    assert result.returncode == 0, result.stderr
