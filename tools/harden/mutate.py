"""Mutation testing for the gauntlet's check 8: reported, never a gate.

Usage:
  python3 tools/harden/mutate.py <scratch dir> <module.py>... -- <pytest args>

Copies the repo (without .venv, which is linked) into <scratch dir>, so a mutant never touches the live
guard hook, then for each module applies one AST mutation at a time - comparison flips, `and`/`or` swaps,
`not` removal, constant changes, `return` of None - and runs the given pytest args against the copy with
a per-mutant timeout. Prints `killed`, `survived`, `timeout` counts and every survivor as
`file:line description`. Exit 0 always: mutation is scored and reported, not a threshold.
"""

from __future__ import annotations

import ast
import copy
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
COMPARE_FLIPS = {
    ast.Eq: ast.NotEq,
    ast.NotEq: ast.Eq,
    ast.Lt: ast.GtE,
    ast.GtE: ast.Lt,
    ast.Gt: ast.LtE,
    ast.LtE: ast.Gt,
    ast.In: ast.NotIn,
    ast.NotIn: ast.In,
    ast.Is: ast.IsNot,
    ast.IsNot: ast.Is,
}
TIMEOUT_SECONDS = 120


def mutation_sites(tree: ast.AST):
    """Yield (node index, description, mutate function) for every mutation the tree admits."""
    nodes = list(ast.walk(tree))
    for index, node in enumerate(nodes):
        if isinstance(node, ast.Compare) and type(node.ops[0]) in COMPARE_FLIPS:
            yield index, f"{type(node.ops[0]).__name__} flipped", _flip_compare
        elif isinstance(node, ast.BoolOp):
            yield index, f"{type(node.op).__name__} swapped", _swap_boolop
        elif isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
            yield index, "not removed", None
        elif isinstance(node, ast.Constant) and isinstance(node.value, bool):
            yield index, f"{node.value} negated", _negate_bool
        elif isinstance(node, ast.Constant) and type(node.value) is int:
            yield index, f"{node.value} incremented", _increment
        elif isinstance(node, ast.Return) and node.value is not None:
            yield index, "returns None", _return_none


def _flip_compare(node):
    node.ops[0] = COMPARE_FLIPS[type(node.ops[0])]()


def _swap_boolop(node):
    node.op = ast.Or() if isinstance(node.op, ast.And) else ast.And()


def _negate_bool(node):
    node.value = not node.value


def _increment(node):
    node.value += 1


def _return_none(node):
    node.value = ast.Constant(None)


class _DropNot(ast.NodeTransformer):
    def __init__(self, target):
        self.target = target

    def visit_UnaryOp(self, node):
        self.generic_visit(node)
        return node.operand if node is self.target else node


def mutant_source(tree: ast.AST, index: int, mutate) -> str:
    mutated = copy.deepcopy(tree)
    node = list(ast.walk(mutated))[index]
    if mutate is None:
        mutated = _DropNot(node).visit(mutated)
    else:
        mutate(node)
    return ast.unparse(ast.fix_missing_locations(mutated))


def make_copy(scratch: Path) -> None:
    ignore = shutil.ignore_patterns(
        ".venv", ".git", "__pycache__", ".dev", "raw", ".pytest_cache", ".ruff_cache"
    )
    shutil.copytree(REPO, scratch, ignore=ignore, dirs_exist_ok=True)
    if not (scratch / ".venv").exists():
        (scratch / ".venv").symlink_to(REPO / ".venv")


def run_tests(scratch: Path, pytest_args: list[str]) -> str:
    env = {**os.environ, "JARVIS_ROOT": "", "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("JARVIS_ROOT")
    try:
        result = subprocess.run(
            [
                str(scratch / ".venv" / "bin" / "python"),
                "-m",
                "pytest",
                "-x",
                "-q",
                "-p",
                "no:cacheprovider",
                *pytest_args,
            ],
            cwd=scratch,
            env=env,
            capture_output=True,
            timeout=TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return "timeout"
    return "survived" if result.returncode == 0 else "killed"


def main(argv: list[str]) -> int:
    split = argv.index("--")
    scratch, modules, pytest_args = Path(argv[0]), argv[1:split], argv[split + 1 :]
    make_copy(scratch)
    if run_tests(scratch, pytest_args) != "survived":
        print("the unmutated copy does not pass the tests; nothing to measure")
        return 0
    counts = {"killed": 0, "survived": 0, "timeout": 0}
    survivors = []
    for module in modules:
        path = scratch / module
        original = path.read_text()
        tree = ast.parse(original)
        nodes = list(ast.walk(tree))
        for index, description, mutate in mutation_sites(tree):
            path.write_text(mutant_source(tree, index, mutate))
            outcome = run_tests(scratch, pytest_args)
            counts[outcome] += 1
            if outcome == "survived":
                survivors.append(f"{module}:{getattr(nodes[index], 'lineno', '?')} {description}")
        path.write_text(original)
    print(f"killed {counts['killed']}, survived {counts['survived']}, timeout {counts['timeout']}")
    for line in survivors:
        print(f"survived: {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
