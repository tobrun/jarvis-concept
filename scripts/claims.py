"""Claims in memory/claims/<id>.md: draw the sample the verification pass checks.

Usage:
  claims.py sample [--pct 10]   prints one sampled active claim id per line

A claim file is YAML frontmatter between `---` lines, then a free markdown body:
  id, statement, kind, salience, confidence,
  status in {active, superseded, retracted, unverifiable}, supersedes, evidence

The sample is max(1, round(pct% of active claims)) ids when any claim is active, none otherwise.
It is drawn with random.Random(seed) over the sorted active ids, seeded by JARVIS_RUN_ID (or "manual"),
so a replay of the same run draws the same claims.
"""

from __future__ import annotations

import argparse
import os
import random
import sys
from pathlib import Path
from typing import Any

import common
from loops import split_frontmatter


def read_claims(root: Path) -> list[dict[str, Any]]:
    claims = []
    for path in sorted((root / "memory" / "claims").glob("*.md")):
        meta, _ = split_frontmatter(path.read_text())
        claims.append({"id": path.stem, **meta})
    return claims


def sample(claims: list[dict[str, Any]], pct: float, seed: str) -> list[str]:
    active = sorted(str(claim["id"]) for claim in claims if claim.get("status") == "active")
    if not active:
        return []
    count = min(len(active), max(1, round(len(active) * pct / 100)))
    return random.Random(seed).sample(active, count)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    sample_cmd = commands.add_parser("sample")
    sample_cmd.add_argument("--pct", type=float, default=10)
    args = parser.parse_args(argv)
    seed = os.environ.get("JARVIS_RUN_ID") or "manual"
    for claim_id in sample(read_claims(common.repo_root()), args.pct, seed):
        print(claim_id)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
