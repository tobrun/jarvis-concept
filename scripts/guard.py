"""The PreToolUse guard: allow or deny every tool call against config/allowed_tools.yaml for the current mode.

Usage:
  guard.py                 hook mode: Claude Code PreToolUse JSON on stdin; exit 0 allows, exit 2 denies
  guard.py rules <mode>    print the mode's Claude Code permission rules, one per line, for --allowedTools

Run through scripts/guard.sh, which turns any failure into a deny.
The mode is JARVIS_MODE; unset or unknown denies every call.
Every denial is appended to state/denied_calls.jsonl as {at, mode, run_id, tool, reason, input_sha256}.

Checks, in order: the tool is in the mode's tool set; a constrained tool (the delivery adapter's send) carries
only its listed keys, exactly its target, and a top-level report that passes report_check outside OOO and
holidays, or a thread reply that passes report_check's reply check; file edits resolve inside the repo,
outside the mode's protected globs (`protected_by_mode`, else `protected`), and inside one of its write
globs; Bash has no shell operators or expansion and starts with one of the mode's argv prefixes.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import sys
from pathlib import Path, PurePosixPath
from typing import Any

import common
import report_check

EDIT_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
TEMP_ROOTS = ("/tmp/", "/private/tmp/", "/private/var/folders/", "/var/folders/")
# A command in quoting chunks: single-quoted, double-quoted (each to its closing quote or the end), or bare.
# This matches how sh splits quotes only while no backslash sits outside single quotes, so one is refused.
QUOTE_CHUNK = re.compile(r"'[^']*'?|\"[^\"]*\"?|[^'\"]+")
EXPANSION = re.compile(r"[$`\n\\]")
# Outside quotes: a run of shell operators, any character but a plain ASCII word character, blank, or
# operator (globs, braces, tildes, `!`, `^`, control and non-ASCII characters), and a word-initial `#`
# (sh starts a comment) or `=` (zsh expands `=name` to a program path).
OPERATOR_RUN = re.compile(r"[;&|<>()]+")
BARE_UNSAFE = re.compile(r"[^A-Za-z0-9_\-./:,@%+=# \t;&|<>()]")
WORD_START = re.compile(r"(?:^|[ \t])([#=])")


class Denied(Exception):
    """A tool call the guard refuses; the message is the reason Claude sees."""


def load_policy(root: Path) -> dict[str, Any]:
    return common.load_yaml(root / "config" / "allowed_tools.yaml")


def tool_set(policy: dict[str, Any], mode: str) -> set[str]:
    names: set[str] = set()
    for entry in policy["modes"][mode]["tools"]:
        names.update(policy["groups"].get(entry, [entry]))
    return names


def _glob_match(relative: str, patterns: list[str]) -> bool:
    path = PurePosixPath(relative.lower())
    return any(path.full_match(pattern.lower()) for pattern in patterns)


def _resolve(root: Path, raw_path: str) -> Path:
    """The real path an edit lands on: relative to the repo, with symlinks and `..` resolved."""
    if not raw_path:
        raise Denied("file edit without a path")
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    return Path(os.path.realpath(candidate))


def _dev_scratch(resolved: Path, real_root: Path, mode: str) -> bool:
    """Dev mode may write temp files outside the repo."""
    return mode == "dev" and str(resolved).startswith(TEMP_ROOTS) and not resolved.is_relative_to(real_root)


def check_path(root: Path, raw_path: str, mode: str, policy: dict[str, Any]) -> None:
    """Resolve symlinks and `..`; require the path inside the repo, unprotected, and in the write scope."""
    resolved = _resolve(root, raw_path)
    real_root = Path(os.path.realpath(root))
    if _dev_scratch(resolved, real_root, mode):
        return
    if not resolved.is_relative_to(real_root):
        raise Denied(f"{raw_path} is outside the repo")
    relative = resolved.relative_to(real_root).as_posix()
    protected = (policy.get("protected_by_mode") or {}).get(mode, policy["protected"])
    if _glob_match(relative, protected):
        raise Denied(f"{relative} is protected in {mode}")
    if not _glob_match(relative, policy["modes"][mode]["write"]):
        raise Denied(f"{relative} is outside the {mode} write scope")


def bash_argv(command: str) -> list[str]:
    """Split a command into argv, refusing anything a shell would treat as more than one plain command."""
    _reject_expansion(command)
    _reject_unquoted(command)
    try:
        tokens = shlex.split(command, comments=False)
    except ValueError as error:
        raise Denied(f"unparseable command: {error}") from error
    if not tokens:
        raise Denied("empty command")
    return tokens


def _reject_expansion(command: str) -> None:
    """Refuse `$`, a backtick, a newline, or a backslash outside single quotes, where a shell would act on it.

    A backslash escapes a quote or a newline, so past one the chunks stop matching the shell's view.
    The first one found is still truly outside single quotes: every chunk before it split as sh splits it.
    """
    exposed = "".join(chunk for chunk in QUOTE_CHUNK.findall(command) if not chunk.startswith("'"))
    found = EXPANSION.search(exposed)
    if found is None:
        return
    if found.group() == "\\":
        raise Denied("a backslash outside single quotes is not allowed; it can escape a quote or a newline")
    if found.group() == "\n":
        raise Denied("multi-line commands are not allowed")
    raise Denied("shell expansion ($ or backtick) is not allowed")


def _reject_unquoted(command: str) -> None:
    """Refuse an operator or any character outside quotes that sh could read other than shlex does.

    Past _reject_expansion no backslash sits outside single quotes, so the chunks split as sh splits them.
    Each quoted chunk stands in as one plain word character, which keeps a `#` after it mid-word.
    """
    bare = "".join("_" if chunk[0] in "'\"" else chunk for chunk in QUOTE_CHUNK.findall(command))
    operator = OPERATOR_RUN.search(bare)
    if operator:
        raise Denied(f"shell operator {operator.group()!r} is not allowed; run one plain command")
    unsafe = BARE_UNSAFE.search(bare)
    if unsafe:
        raise Denied(f"unquoted {unsafe.group()!r} is not allowed; quote it so the shell takes it literally")
    word_start = WORD_START.search(bare)
    if word_start:
        raise Denied(
            f"unquoted {word_start.group(1)!r} at the start of a word is not allowed; "
            "quote it so the shell takes it literally"
        )


def check_bash(command: str, mode: str, policy: dict[str, Any]) -> None:
    prefixes = policy["modes"][mode]["bash"]
    if prefixes == "any":
        return
    argv = bash_argv(command)
    if not any(argv[: len(prefix)] == prefix for prefix in prefixes):
        raise Denied(f"command not allowed in {mode}: {' '.join(argv[:4])}")


def _covering_ooo(root: Path) -> str | None:
    ooo = common.read_json(root / "state" / "ooo.json", None)
    if ooo and ooo.get("until") and common.parse_ts(ooo["until"]) > common.now():
        return ooo.get("raw_id", "unknown event")
    return None


def check_post(root: Path, tool_input: dict[str, Any], spec: dict[str, Any]) -> None:
    """A delivery call: only the adapter's keys, exactly its target, and a report or reply that passes."""
    extra = set(tool_input) - set(spec["keys"])
    if extra:
        raise Denied(f"parameter not allowed: {', '.join(sorted(extra))}")
    target_key = spec["target_key"]
    if tool_input.get(target_key) != spec["target"]:
        raise Denied(f"{target_key} {tool_input.get(target_key)!r} is not the delivery target")
    message = tool_input.get(spec["message_key"], "")
    if not isinstance(message, str):
        raise Denied(f"{spec['message_key']} must be text")
    thread_key = spec.get("thread_key")
    if thread_key and tool_input.get(thread_key):
        problems = report_check.check_reply(message, root)
    else:
        problems = _top_level_problems(root, message)
    if problems:
        raise Denied("report check failed: " + "; ".join(problems[:5]))


def _top_level_problems(root: Path, message: str) -> list[str]:
    """A top-level post goes out only in a sent run outside OOO; then report_check decides."""
    if os.environ.get("JARVIS_UNSENT") == "1":
        raise Denied("this run is unsent (holiday or OOO); write the report file only")
    covering = _covering_ooo(root)
    if covering:
        raise Denied(f"the principal is out of office ({covering}); write the report file only")
    return report_check.check(message, root)


def decide(root: Path, mode: str | None, call: dict[str, Any]) -> None:
    """Raise Denied for a call the mode does not allow; return for an allowed one."""
    if not mode:
        raise Denied("JARVIS_MODE is unset; start Jarvis with scripts/run.sh or scripts/jarvis")
    policy = load_policy(root)
    if mode not in policy["modes"]:
        raise Denied(f"unknown mode {mode!r}")
    tool = call.get("tool_name", "")
    tool_input = call.get("tool_input") or {}
    if tool not in tool_set(policy, mode):
        raise Denied(f"{tool} is not allowed in {mode}")
    _check_input(root, mode, policy, tool, tool_input)


def _check_input(
    root: Path, mode: str, policy: dict[str, Any], tool: str, tool_input: dict[str, Any]
) -> None:
    """The per-tool checks on an allowed tool's input: constrained keys, edit paths, Bash argv."""
    if tool in policy.get("constrained", {}):
        check_post(root, tool_input, policy["constrained"][tool])
    elif tool in EDIT_TOOLS:
        check_path(root, tool_input.get(EDIT_TOOLS[tool], ""), mode, policy)
    elif tool == "Bash":
        check_bash(tool_input.get("command", ""), mode, policy)


def log_denial(root: Path, mode: str | None, call: dict[str, Any], reason: str) -> None:
    digest = hashlib.sha256(json.dumps(call.get("tool_input"), sort_keys=True).encode()).hexdigest()
    common.append_jsonl(
        root / "state" / "denied_calls.jsonl",
        {
            "at": common.now().isoformat(),
            "mode": mode,
            "run_id": os.environ.get("JARVIS_RUN_ID"),
            "tool": call.get("tool_name"),
            "reason": reason,
            "input_sha256": digest,
        },
    )


def permission_rules(root: Path, mode: str) -> list[str]:
    """Claude Code --allowedTools rules for a mode: the second layer, independent of the hooks loading."""
    policy = load_policy(root)
    mode_policy = policy["modes"][mode]
    rules: list[str] = []
    for tool in sorted(tool_set(policy, mode)):
        rules += [rule for rule in _tool_rules(tool, mode_policy) if rule not in rules]
    return rules


def _tool_rules(tool: str, mode_policy: dict[str, Any]) -> list[str]:
    # Claude Code matches path rules for every file-editing tool through Edit(path) only; Write(path),
    # MultiEdit(path), and NotebookEdit(path) rules are ignored with a warning.
    if tool in EDIT_TOOLS:
        return [f"Edit({pattern})" for pattern in mode_policy["write"]]
    if tool != "Bash":
        return [tool]
    if mode_policy["bash"] == "any":
        return ["Bash"]
    return [f"Bash({' '.join(prefix)}:*)" for prefix in mode_policy["bash"]]


def main(argv: list[str]) -> int:
    root = common.repo_root()
    if argv[:1] == ["rules"] and len(argv) == 2:
        print("\n".join(permission_rules(root, argv[1])))
        return 0
    mode = os.environ.get("JARVIS_MODE")
    call: dict[str, Any] = {}
    try:
        call = json.loads(sys.stdin.read())
        decide(root, mode, call)
    except Denied as denial:
        log_denial(root, mode, call, str(denial))
        print(f"Jarvis guard denied this call: {denial}", file=sys.stderr)
        return 2
    except Exception as error:
        reason = f"guard error: {type(error).__name__}: {error}"
        log_denial(root, mode, call, reason)
        print(f"Jarvis guard denied this call: {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
