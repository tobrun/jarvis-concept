"""PostToolUse hook: append each read tool's exact response to raw/<source>/<YYYY-MM-DD>.jsonl.

Usage:
  capture.py < hook.json      (run through scripts/capture.sh by Claude Code)

The source comes from config/sources.yaml, rendered from the enabled adapters: each source lists exact MCP
tool names (`tools`), a Bash argv prefix (`bash`, e.g. `uv run scripts/gh_read.py`), or a folder whose Read
calls it captures (`read`, e.g. notes). Any other tool is ignored. One line per call:
{raw_id, source, tool, input, payload, fetched_at, run_id}, with payload = tool_response verbatim, after the
tombstone registry in state/tombstones.jsonl is applied. The date is the fetch date in the owner's timezone.
run_id is JARVIS_RUN_ID, else interactive-<date>.

A PostToolUse hook cannot block, so this never fails: any error is appended to state/capture_errors.jsonl
and the exit code is 0.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import common
import raw


def tool_sources(sources: dict[str, Any]) -> dict[str, str]:
    """Exact tool name -> source, from the `sources` section of config/sources.yaml."""
    mapping: dict[str, str] = {}
    for source, spec in (sources.get("sources") or {}).items():
        for tool in (spec or {}).get("tools") or []:
            mapping[tool] = source
    return mapping


def source_for(tool: str, tool_input: Any, sources: dict[str, Any], root: Path) -> str | None:
    """The raw source a tool call belongs to, or None when it is not captured."""
    mapped = tool_sources(sources).get(tool)
    if mapped:
        return mapped
    if not isinstance(tool_input, dict):
        return None
    if tool == "Bash":
        return _bash_source(str(tool_input.get("command", "")), sources)
    if tool == "Read" and tool_input.get("file_path"):
        return _read_source(Path(str(tool_input["file_path"])), sources, root)
    return None


def _specs(sources: dict[str, Any], key: str) -> list[tuple[str, Any]]:
    """(source, value of `key`) for every source that captures through `key`."""
    return [
        (source, spec[key])
        for source, spec in (sources.get("sources") or {}).items()
        if isinstance(spec, dict) and spec.get(key)
    ]


def _bash_source(command: str, sources: dict[str, Any]) -> str | None:
    """The source whose argv prefix starts the command; None for an unparsable command."""
    try:
        argv = shlex.split(command)
    except ValueError:
        return None
    for source, prefix in _specs(sources, "bash"):
        if argv[: len(prefix)] == list(prefix):
            return source
    return None


def _read_source(path: Path, sources: dict[str, Any], root: Path) -> str | None:
    """The source whose folder holds the file; a relative path is taken from the repo root."""
    path = (path if path.is_absolute() else root / path).resolve()
    for source, folder in _specs(sources, "read"):
        if path.is_relative_to((root / folder).resolve()):
            return source
    return None


def call_id(source: str, tool: str, tool_input: Any, payload: Any) -> str:
    digest = hashlib.sha256(raw.serialize([tool, tool_input, payload]).encode()).hexdigest()
    return f"{source}:call:{digest[:16]}"


def build_line(event: dict[str, Any], source: str, run_id: str, fetched_at: str) -> dict[str, Any]:
    tool, tool_input, payload = event.get("tool_name"), event.get("tool_input"), event.get("tool_response")
    return {
        "raw_id": call_id(source, tool, tool_input, payload),
        "source": source,
        "tool": tool,
        "input": tool_input,
        "payload": payload,
        "fetched_at": fetched_at,
        "run_id": run_id,
    }


def capture(event: dict[str, Any], root: Path) -> Path | None:
    """Append the call's raw line; returns the file written, or None when the tool is not captured."""
    sources = common.load_yaml(root / "config" / "sources.yaml") or {}
    source = source_for(str(event.get("tool_name", "")), event.get("tool_input"), sources, root)
    if source is None:
        return None
    moment = common.now()
    day = moment.date().isoformat()
    run_id = os.environ.get("JARVIS_RUN_ID") or f"interactive-{day}"
    line = raw.apply_registry(build_line(event, source, run_id, moment.isoformat()), raw.registry(root), root)
    path = root / "raw" / source / f"{day}.jsonl"
    common.append_jsonl(path, line)
    return path


def main(argv: list[str], stdin: str | None = None) -> int:
    tool = None
    try:
        root = common.repo_root()
        event = json.loads(sys.stdin.read() if stdin is None else stdin)
        if not isinstance(event, dict):
            raise ValueError("hook input is not a JSON object")
        tool = event.get("tool_name")
        capture(event, root)
    except Exception as error:
        with contextlib.suppress(Exception):
            common.append_jsonl(
                common.repo_root() / "state" / "capture_errors.jsonl",
                {"at": common.now().isoformat(), "error": f"{type(error).__name__}: {error}", "tool": tool},
            )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
