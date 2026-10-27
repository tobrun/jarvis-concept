"""Pin and verify the guard's enforcement set, and prove the guard still denies.

Usage:
  guard_pin.py verify       exit 0 when every pinned file matches and no hook is disabled; else 1 naming why
  guard_pin.py pin          rewrite state/guard.sha256 from the files on disk (dev mode, by hand)
  guard_pin.py self-test    run guard.sh in a scratch copy of the enforcement set on an unlisted write tool,
                            a Read of JARVIS.md, and the delivery tool aimed at someone else; exit 0 when
                            it denies the first and third and allows the second

state/guard.sha256 is `sha256sum` format: one `<hex>  <path>` line per file of ENFORCEMENT_SET, in order.
`verify` also fails when `.claude/settings.local.json` exists in the repo, or when any settings file Claude
Code reads (the repo's, the config dir's, the managed one) sets `disableAllHooks`.
The config dir is CLAUDE_CONFIG_DIR, else `claude_config_dir` from config/jarvis.yaml, else ~/.claude.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import common

# Every file the guard runs, imports, or reads, plus the hook wiring and this script.
ENFORCEMENT_SET = (
    ".claude/settings.json",
    "scripts/guard.py",
    "scripts/guard.sh",
    "scripts/guard_pin.py",
    "scripts/common.py",
    "scripts/report_check.py",
    "scripts/raw.py",
    "config/allowed_tools.yaml",
    "config/jarvis.yaml",
    "config/sources.yaml",
)
MANAGED_SETTINGS = {
    "darwin": Path("/Library/Application Support/ClaudeCode/managed-settings.json"),
    "linux": Path("/etc/claude-code/managed-settings.json"),
}
# A write tool no allowlist can list: the self-test proves default deny without naming any real server.
FORBIDDEN_CALL = {"tool_name": "mcp__jarvis_selftest__send_message", "tool_input": {"to": "anyone"}}
ALLOWED_CALL = {"tool_name": "Read", "tool_input": {"file_path": "JARVIS.md"}}


def pin_path(root: Path) -> Path:
    return root / "state" / "guard.sha256"


def digests(root: Path) -> list[tuple[str, str]]:
    """(hex, path) for each enforcement file; a missing file hashes as `missing`."""
    found = []
    for name in ENFORCEMENT_SET:
        path = root / name
        found.append((hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "missing", name))
    return found


def pin(root: Path) -> None:
    lines = "".join(f"{digest}  {name}\n" for digest, name in digests(root))
    path = pin_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(lines)


def read_pin(root: Path) -> dict[str, str]:
    pinned = {}
    for line in pin_path(root).read_text().splitlines():
        digest, _, name = line.partition("  ")
        if name:
            pinned[name] = digest
    return pinned


def config_dir(root: Path) -> Path:
    configured = os.environ.get("CLAUDE_CONFIG_DIR") or common.jarvis_config(root).get("claude_config_dir")
    return Path(os.path.expanduser(configured or "~/.claude"))


def settings_files(root: Path) -> list[Path]:
    home = config_dir(root)
    return [
        root / ".claude" / "settings.json",
        root / ".claude" / "settings.local.json",
        home / "settings.json",
        home / "settings.local.json",
        *([MANAGED_SETTINGS[sys.platform]] if sys.platform in MANAGED_SETTINGS else []),
    ]


def tamper_problems(root: Path) -> list[str]:
    problems = []
    if (root / ".claude" / "settings.local.json").exists():
        problems.append(".claude/settings.local.json exists; it can override the guard hooks, remove it")
    for path in settings_files(root):
        problem = settings_problem(path)
        if problem:
            problems.append(problem)
    return problems


def settings_problem(path: Path) -> str | None:
    """Why one settings file weakens the guard: unreadable, or disabling every hook; None when it is fine."""
    try:
        settings = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as error:
        return f"{path} is unreadable: {error}"
    if isinstance(settings, dict) and settings.get("disableAllHooks"):
        return f"{path} sets disableAllHooks"
    return None


def verify(root: Path) -> list[str]:
    """Problems with the enforcement set; empty means the pinned guard is the one on disk."""
    if not pin_path(root).exists():
        return ["state/guard.sha256 is missing; pin the guard in dev mode (uv run scripts/guard_pin.py pin)"]
    pinned = read_pin(root)
    problems = [
        f"{name} does not match its pinned hash"
        for digest, name in digests(root)
        if pinned.get(name) != digest
    ]
    problems += [
        f"{name} is pinned but not in the enforcement set" for name in pinned if name not in ENFORCEMENT_SET
    ]
    return problems + tamper_problems(root)


def guard_exit(root: Path, call: dict) -> int:
    env = {
        **os.environ,
        "JARVIS_ROOT": str(root),
        "JARVIS_MODE": "morning",
        "JARVIS_RUN_ID": "guard-self-test",
    }
    env.pop("JARVIS_UNSENT", None)
    result = subprocess.run(
        [str(root / "scripts" / "guard.sh")],
        input=json.dumps(call),
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=False,
    )
    return result.returncode


def self_test(root: Path) -> list[str]:
    """Run the enforcement set, copied byte for byte into a scratch root, so the probe denial is not logged
    in the live state/denied_calls.jsonl."""
    with tempfile.TemporaryDirectory(prefix="jarvis-guard-") as scratch:
        probe = Path(scratch)
        for name in ENFORCEMENT_SET:
            if (root / name).exists():
                (probe / name).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(root / name, probe / name)
        (probe / ".venv").symlink_to(os.path.realpath(root / ".venv"))
        problems = []
        if guard_exit(probe, FORBIDDEN_CALL) != 2:
            problems.append("the guard did not deny an unlisted write tool in morning mode")
        if guard_exit(probe, ALLOWED_CALL) != 0:
            problems.append("the guard denied a Read of JARVIS.md in morning mode")
        misdirected = misdirected_post(probe)
        if misdirected and guard_exit(probe, misdirected) != 2:
            problems.append(
                f"the guard did not deny {misdirected['tool_name']} aimed away from the delivery target"
            )
    return problems


def misdirected_post(root: Path) -> dict | None:
    """A call of the delivery tool aimed at someone else, or None when the delivery sends nothing."""
    constrained = (common.load_yaml(root / "config" / "allowed_tools.yaml") or {}).get("constrained") or {}
    for tool, spec in sorted(constrained.items()):
        target = ["someone-else@example.invalid"] if isinstance(spec["target"], list) else "someone-else"
        return {"tool_name": tool, "tool_input": {spec["target_key"]: target, spec["message_key"]: "probe"}}
    return None


def main(argv: list[str]) -> int:
    root = common.repo_root()
    if argv == ["pin"]:
        pin(root)
        print(f"pinned {len(ENFORCEMENT_SET)} files to {pin_path(root)}")
        return 0
    if argv == ["verify"]:
        problems = verify(root)
    elif argv == ["self-test"]:
        problems = self_test(root)
    else:
        print(__doc__, file=sys.stderr)
        return 2
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
