"""A stand-in for `claude -p` that drives the repo's real hooks with a scripted list of tool calls.

It reads the prompt from stdin and the project's .claude/settings.json from the working directory, and for
each scripted call it does what Claude Code does around a tool:
  PreToolUse   every matching hook command runs with the call as JSON on stdin; exit 2 blocks the call
  permission   the call must match --allowedTools (exact name, `Tool(glob)` for edits, `Bash(prefix:*)`);
               Read, Glob, Grep, and LS need no rule
  tool         Write and Edit write the file, Read reads it, Bash runs the command in a shell; any other
               tool returns the step's scripted `response`
  PostToolUse  every matching hook runs with the call and its `tool_response`

The script is the JSON list at FAKE_CLAUDE_SCRIPT. A step is a call `{"tool", "input", "response"}`,
`{"sleep": seconds}`, or `{"exit": code}`. Strings in a call's input may use {root}, {run_id}, and any key
of the JSON plan block in the prompt, e.g. {mode} or {slot_date}.
A run whose JARVIS_RUN_ID ends in `-canary` ignores the script and makes the one WebFetch the canary asks for.
The session's plan and every call's outcome are appended as JSON lines to FAKE_CLAUDE_LOG.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path, PurePosixPath
from typing import Any

FREE_TOOLS = {"Read", "Glob", "Grep", "LS"}
EDIT_KEYS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}


def parse_args(argv: list[str]) -> dict[str, Any]:
    options: dict[str, Any] = {"allowed": []}
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--allowedTools":
            index += 1
            while index < len(argv) and not argv[index].startswith("--"):
                options["allowed"].append(argv[index])
                index += 1
            continue
        if arg in ("--model", "--max-turns", "--permission-mode"):
            options[arg.lstrip("-")] = argv[index + 1]
            index += 2
            continue
        index += 1
    return options


def plan_from(prompt: str) -> dict[str, Any]:
    blocks = re.findall(r"```json\n(.*?)\n```", prompt, re.S)
    return json.loads(blocks[-1]) if blocks else {}


def fill(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):
        for key, text in variables.items():
            value = value.replace("{" + key + "}", text)
        return value
    if isinstance(value, dict):
        return {k: fill(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, variables) for v in value]
    return value


def permitted(tool: str, tool_input: dict[str, Any], rules: list[str], root: Path) -> bool:
    if tool in FREE_TOOLS or tool in rules:
        return True
    for rule in rules:
        match = re.fullmatch(r"(\w+)\((.*)\)", rule)
        # As in Claude Code, Edit(path) rules cover every file-editing tool and no other path rule does.
        wanted = "Edit" if tool in EDIT_KEYS else tool
        if not match or match.group(1) != wanted:
            continue
        pattern = match.group(2)
        if tool == "Bash" and pattern.endswith(":*"):
            prefix = pattern[:-2]
            command = tool_input.get("command", "")
            if command == prefix or command.startswith(prefix + " "):
                return True
        elif tool in EDIT_KEYS:
            path = Path(tool_input.get(EDIT_KEYS[tool], ""))
            if not path.is_absolute():
                path = root / path
            try:
                relative = PurePosixPath(path.resolve().relative_to(root.resolve()).as_posix())
            except ValueError:
                continue
            if relative.full_match(pattern):
                return True
    return False


def run_hooks(event: str, payload: dict[str, Any], root: Path) -> subprocess.CompletedProcess[str] | None:
    """Run the event's matching hooks; returns the first blocking result, else None."""
    settings = json.loads((root / ".claude" / "settings.json").read_text())
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    for group in settings.get("hooks", {}).get(event, []):
        if not re.fullmatch(group.get("matcher", "*").replace("*", ".*"), payload["tool_name"]):
            continue
        for hook in group["hooks"]:
            result = subprocess.run(
                hook["command"],
                shell=True,
                input=json.dumps({**payload, "hook_event_name": event}),
                capture_output=True,
                text=True,
                env=env,
                cwd=root,
                timeout=hook.get("timeout", 60),
                check=False,
            )
            if event == "PreToolUse" and result.returncode == 2:
                return result
    return None


def execute(tool: str, tool_input: dict[str, Any], scripted: Any, root: Path) -> Any:
    if tool == "Write":
        path = root / tool_input["file_path"]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(tool_input["content"])
        return {"type": "create", "filePath": str(path)}
    if tool == "Edit":
        path = root / tool_input["file_path"]
        path.write_text(path.read_text().replace(tool_input["old_string"], tool_input["new_string"]))
        return {"filePath": str(path)}
    if tool == "Read":
        path = root / tool_input["file_path"]
        return {"type": "text", "file": {"filePath": str(path), "content": path.read_text()}}
    if tool == "Bash":
        result = subprocess.run(
            tool_input["command"], shell=True, capture_output=True, text=True, cwd=root, check=False
        )
        return {"stdout": result.stdout, "stderr": result.stderr, "exit_code": result.returncode}
    return scripted


def call(step: dict[str, Any], root: Path, rules: list[str], log: Path | None) -> None:
    tool, tool_input = step["tool"], step.get("input", {})
    payload = {"session_id": "fake", "cwd": str(root), "tool_name": tool, "tool_input": tool_input}
    record: dict[str, Any] = {"tool": tool, "input": tool_input}
    blocked = run_hooks("PreToolUse", payload, root)
    if blocked is not None:
        record.update(outcome="denied_by_hook", reason=blocked.stderr.strip())
    elif not permitted(tool, tool_input, rules, root):
        record.update(outcome="denied_by_permissions")
    else:
        response = execute(tool, tool_input, step.get("response"), root)
        run_hooks("PostToolUse", {**payload, "tool_response": response}, root)
        record.update(outcome="allowed", response=response)
    if log:
        with log.open("a") as out:
            out.write(json.dumps(record, sort_keys=True) + "\n")


def main(argv: list[str]) -> int:
    options = parse_args(argv)
    prompt = sys.stdin.read()
    root = Path.cwd()
    run_id = os.environ.get("JARVIS_RUN_ID", "")
    log = Path(os.environ["FAKE_CLAUDE_LOG"]) if os.environ.get("FAKE_CLAUDE_LOG") else None
    if run_id.endswith("-canary"):
        steps = [{"tool": "WebFetch", "input": {"url": "https://example.com", "prompt": "title"}}]
    else:
        steps = json.loads(Path(os.environ["FAKE_CLAUDE_SCRIPT"]).read_text())
    plan = plan_from(prompt)
    if log:
        with log.open("a") as out:
            out.write(json.dumps({"plan": plan, "run_id": run_id, "allowed": options["allowed"]}) + "\n")
    variables = {key: str(value) for key, value in plan.items()}
    variables.update(root=str(root), run_id=run_id)
    if "slot" in plan and "slot_date" not in plan:
        variables["slot_date"] = plan["slot"][:10]
    for step in steps:
        if "sleep" in step:
            time.sleep(step["sleep"])
        elif "exit" in step:
            return int(step["exit"])
        else:
            call(fill(step, variables), root, options["allowed"], log)
    print("fake session done")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
