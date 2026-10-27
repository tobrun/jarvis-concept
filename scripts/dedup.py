"""Dedup keys for surfaced items, so the same thread with the same text is surfaced once.

Usage:
  dedup.py key <thread_id> --file <text file>   prints "<thread_id>:<sha256[:16] of normalized text>"
  dedup.py seen <key>                           exit 0 if the key is in state/surfaced.jsonl, else 1

Normalized text is the text with every whitespace run collapsed to one space, then stripped.
state/surfaced.jsonl is written through `append.py surfaced` ({key, raw_id, at}).
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from pathlib import Path

import common


def key(thread_id: str, text: str) -> str:
    normalized = " ".join(text.split())
    return f"{thread_id}:{hashlib.sha256(normalized.encode()).hexdigest()[:16]}"


def seen(root: Path, wanted: str) -> bool:
    return any(entry.get("key") == wanted for entry in common.read_jsonl(root / "state" / "surfaced.jsonl"))


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    key_cmd = commands.add_parser("key")
    key_cmd.add_argument("thread_id")
    key_cmd.add_argument("--file", required=True, type=Path)
    seen_cmd = commands.add_parser("seen")
    seen_cmd.add_argument("key")
    args = parser.parse_args(argv)
    if args.command == "key":
        print(key(args.thread_id, args.file.read_text()))
        return 0
    return 0 if seen(common.repo_root(), args.key) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
