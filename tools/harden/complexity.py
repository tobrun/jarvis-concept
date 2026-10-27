"""Coverage-weighted complexity per function, the gauntlet's check 6.

Usage:
  python3 tools/harden/complexity.py <radon cc json> <coverage json> [--threshold 6]

Inputs come from `radon cc -j scripts` and `coverage json` over the full test suite (tools/harden/README.md).
score = complexity + (complexity^2 - complexity) * (1 - covered), where covered is the share of the function's
executable lines the tests ran: a fully covered function scores its complexity, an uncovered one its square.
Prints each function over the threshold as `file:function score (complexity N, coverage P%)`; exit 1 if any.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def score(complexity: int, covered: float) -> float:
    return complexity + (complexity * complexity - complexity) * (1 - covered)


def coverage_by_file(report: dict) -> dict[str, tuple[set[int], set[int]]]:
    """file -> (executed lines, missing lines), keyed by the repo-relative path."""
    files = {}
    for name, data in report["files"].items():
        files[name] = (set(data["executed_lines"]), set(data["missing_lines"]))
    return files


def functions(radon: dict):
    for path, blocks in radon.items():
        for block in blocks:
            yield path, block
            for method in block.get("methods", []):
                yield path, method


def main(argv: list[str]) -> int:
    threshold = 6.0
    if "--threshold" in argv:
        threshold = float(argv[argv.index("--threshold") + 1])
    radon = json.loads(Path(argv[0]).read_text())
    lines = coverage_by_file(json.loads(Path(argv[1]).read_text()))
    over = []
    for path, block in functions(radon):
        if block.get("type") not in ("function", "method"):
            continue
        executed, missing = lines.get(path, (set(), set()))
        span = set(range(block["lineno"], block["endline"] + 1))
        run, skipped = len(span & executed), len(span & missing)
        covered = run / (run + skipped) if run + skipped else 0.0
        value = score(block["complexity"], covered)
        if value > threshold:
            over.append(
                (
                    value,
                    f"{path}:{block['name']} {value:.1f} "
                    f"(complexity {block['complexity']}, coverage {covered:.0%})",
                )
            )
    for _, line in sorted(over, reverse=True):
        print(line)
    return 1 if over else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
