"""GET-only GitHub reader: the one way Jarvis reads GitHub; raw `gh` is never allowlisted.

Usage:
  gh_read.py <api-path>

Exactly one argument, an api.github.com path matching a read pattern:
  repos/<owner>/<repo>/pulls..., /issues..., /commits... (including reviews and comments under them)
  search/issues?..., search/commits?...
  user, notifications
Flags (--hostname, -X, -f, --input, --method, ...), graphql, and anything else are refused with exit 2.
Runs `gh api --method GET <path>` against github.com and prints its stdout.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable

SEGMENT = r"[A-Za-z0-9_.-]+"
QUERY = r"(\?[A-Za-z0-9_.~%+:=&,-]*)?"
READ_PATHS = [
    re.compile(rf"^repos/{SEGMENT}/{SEGMENT}/(pulls|issues|commits)(/{SEGMENT})*{QUERY}$"),
    re.compile(r"^search/(issues|commits)\?[A-Za-z0-9_.~%+:=&,-]+$"),
    re.compile(rf"^user{QUERY}$"),
    re.compile(rf"^notifications{QUERY}$"),
]

Runner = Callable[[list[str]], subprocess.CompletedProcess]


class Refused(Exception):
    pass


def decide(argv: list[str]) -> list[str]:
    """The exact gh command for a read request; Refused for anything that could write, leak, or redirect."""
    if len(argv) != 1:
        raise Refused("exactly one argument, the api path; flags and request bodies are not allowed")
    path = argv[0].removeprefix("/")
    if path.startswith("-"):
        raise Refused(f"flags are not allowed: {argv[0]}")
    _check_route(path.split("?", 1)[0].split("/"), argv[0])
    if not any(pattern.match(path) for pattern in READ_PATHS):
        raise Refused(f"not a read path: {argv[0]}")
    return ["gh", "api", "--method", "GET", path]


def _check_route(segments: list[str], requested: str) -> None:
    """Refuse graphql and path traversal, judged on the route's segments before any query string."""
    if segments[0].lower() == "graphql":
        raise Refused("graphql is not allowed")
    if ".." in segments:
        raise Refused(f"path traversal is not allowed: {requested}")


def _run_gh(command: list[str]) -> subprocess.CompletedProcess:
    env = {**os.environ, "GH_HOST": "github.com"}
    env.pop("GH_ENTERPRISE_TOKEN", None)
    return subprocess.run(command, capture_output=True, text=True, env=env, check=False)


def main(argv: list[str], runner: Runner = _run_gh) -> int:
    try:
        command = decide(argv)
    except Refused as refusal:
        print(f"gh_read refused: {refusal}", file=sys.stderr)
        return 2
    result = runner(command)
    sys.stdout.write(result.stdout or "")
    if result.stderr:
        sys.stderr.write(result.stderr)
    return result.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
