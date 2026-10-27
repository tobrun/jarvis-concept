"""The eval gate behind the git hooks: no change to the prompt, thresholds, or scripts lands without an eval.

Usage (from scripts/hooks/, installed by `git config core.hooksPath scripts/hooks`):
    eval_gate.py pre-commit
    eval_gate.py commit-msg <message-file>

A commit is gated when it stages JARVIS.md, OWNER.md, config/thresholds.md, or anything under scripts/.
Until a golden week is frozen (eval/golden/*/manifest.json) a gated commit is allowed with a notice.
After that, pre-commit requires eval/last.json to carry the staged gated_hash, a bump recall no lower than
eval/baseline.json's minus eval_tolerance (config/jarvis.yaml), and a miss count no higher than baseline;
commit-msg requires a line starting with `eval:` that states the delta.
`gated_hash` is imported by the replay runner to stamp eval/last.json.
`git commit --no-verify` bypasses both hooks.
"""

from __future__ import annotations

import hashlib
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

from common import jarvis_config, read_json

GATED = ("JARVIS.md", "OWNER.md", "config/thresholds.md", "scripts")


def git(root: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], input=stdin, capture_output=True, text=True, check=True
    ).stdout


def is_gated(path: str) -> bool:
    return path in ("JARVIS.md", "OWNER.md", "config/thresholds.md") or path.startswith("scripts/")


def _worktree_paths(root: Path) -> list[str]:
    """The gated paths on disk, tracked or untracked but not ignored, that are files or symlinks."""
    paths = [p for p in git(root, "ls-files", "-co", "--exclude-standard", "--", *GATED).splitlines() if p]
    return [p for p in paths if (root / p).is_file() or (root / p).is_symlink()]


def _git_mode(path: Path) -> str:
    """The mode git records for the file: symlink, executable, or regular."""
    mode = os.lstat(path).st_mode
    return "120000" if stat.S_ISLNK(mode) else "100755" if mode & stat.S_IXUSR else "100644"


def _worktree_entries(root: Path) -> list[str]:
    """`git ls-files -s` lines for the gated files as they are on disk, tracked or not."""
    paths = _worktree_paths(root)
    if not paths:
        return []
    blobs = git(root, "hash-object", "--no-filters", "--stdin-paths", stdin="\n".join(paths) + "\n").split()
    entries = []
    for path, blob in zip(paths, blobs, strict=True):
        entries.append(f"{_git_mode(root / path)} {blob} 0\t{path}")
    return entries


def gated_hash(root: Path, staged: bool = True) -> str:
    """sha256 over the sorted `git ls-files -s` entries of the gated paths: the index, or the working tree."""
    if staged:
        entries = [line for line in git(root, "ls-files", "-s", "--", *GATED).splitlines() if line]
    else:
        entries = _worktree_entries(root)
    return hashlib.sha256("\n".join(sorted(entries)).encode()).hexdigest()


def staged_paths(root: Path) -> list[str]:
    return [p for p in git(root, "diff", "--cached", "--name-only").splitlines() if p]


def golden_exists(root: Path) -> bool:
    return any((root / "eval" / "golden").glob("*/manifest.json"))


def recall_ok(last: float | None, baseline: float | None, tolerance: float) -> bool:
    """Recall may drop by at most the tolerance; a null on either side has nothing to compare and passes."""
    if last is None or baseline is None:
        return True
    return last >= baseline - tolerance - 1e-9


def pre_commit_problem(root: Path) -> str | None:
    """Why the staged commit may not land, or None when the eval allows it."""
    last = read_json(root / "eval" / "last.json", None)
    if last is None:
        return "no eval/last.json: run `uv run scripts/replay.py eval` on the staged change"
    if last.get("gated_hash") != gated_hash(root, staged=True):
        return "eval/last.json was made for different content than what is staged: rerun the eval"
    baseline = read_json(root / "eval" / "baseline.json", None)
    if baseline is None:
        return "no eval/baseline.json: accept a baseline with `uv run scripts/replay.py eval --accept`"
    tolerance = float(jarvis_config(root).get("eval_tolerance", 0.05))
    return regression(last, baseline, tolerance)


def regression(last: dict[str, Any], baseline: dict[str, Any], tolerance: float) -> str | None:
    """How the eval falls short of the baseline, or None when recall and misses hold."""
    if not recall_ok(last.get("bump_recall"), baseline.get("bump_recall"), tolerance):
        return (
            f"bump recall {last['bump_recall']:.3f} is below baseline "
            f"{baseline['bump_recall']:.3f} minus tolerance {tolerance}"
        )
    if (last.get("miss_count") or 0) > (baseline.get("miss_count") or 0):
        return f"miss count {last.get('miss_count')} is above baseline {baseline.get('miss_count')}"
    return None


def has_eval_line(message: str) -> bool:
    return any(line.startswith("eval:") for line in message.splitlines())


def commit_msg_problem(message: str) -> str | None:
    return None if has_eval_line(message) else "gated commit needs a line starting with `eval:` (the delta)"


def hook_problem(root: Path, argv: list[str]) -> str | None:
    """Why the hook named in argv blocks a gated commit, or None; the gate stays open until a golden week."""
    if not golden_exists(root):
        if argv[0] == "pre-commit":
            print("eval gate open: no golden week frozen yet", file=sys.stderr)
        return None
    if argv[0] == "pre-commit":
        return pre_commit_problem(root)
    return commit_msg_problem(Path(argv[1]).read_text())


def valid_usage(argv: list[str]) -> bool:
    return bool(argv) and (argv[0] == "pre-commit" or (argv[0] == "commit-msg" and len(argv) == 2))


def main(argv: list[str]) -> int:
    if not valid_usage(argv):
        print(__doc__, file=sys.stderr)
        return 2
    root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").strip())
    if not any(is_gated(p) for p in staged_paths(root)):
        return 0
    problem = hook_problem(root, argv)
    if problem:
        print(f"eval gate: {problem}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
