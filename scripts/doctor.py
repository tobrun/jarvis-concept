"""Check that this Jarvis can run: Claude Code, guard, profile, connectors, readers, index, and schedule.

Usage:
  doctor.py                 every check; exit 1 when any check fails
  doctor.py --offline       every check except the ones that start Claude Code (probe and canary)
  doctor.py --canary        also run the live hook canary a scheduled run does in preflight
  doctor.py tools           start one short headless session and print the MCP servers it sees, as JSON

Each line is `ok`, `warn`, or `FAIL` and one sentence. Nothing here writes outside state/logs/.

The probe is one headless `claude -p` turn on the canary model with no tool allowed. Its stream-json `init`
event lists every MCP server with a status (connected, pending, needs-auth, failed) and the tools of the
servers already connected. Servers still `pending` at that instant are probed again, up to PROBE_ATTEMPTS
sessions in all; one still pending after that is reported as reachable with its tool names unverified.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import adapters
import bootstrap
import common
import guard_pin

PROBE_PROMPT = "Reply with the single word ok. Do not call any tool."
PROBE_TIMEOUT_SECONDS = 180
PROBE_ATTEMPTS = 3
STATUS_RANK = {"connected": 0, "pending": 1}
SERVER_CHARS = re.compile(r"[^A-Za-z0-9_-]")


@dataclass
class Report:
    lines: list[tuple[str, str]] = field(default_factory=list)

    def ok(self, text: str) -> None:
        self.lines.append(("ok", text))

    def warn(self, text: str) -> None:
        self.lines.append(("warn", text))

    def fail(self, text: str) -> None:
        self.lines.append(("FAIL", text))

    @property
    def failed(self) -> bool:
        return any(level == "FAIL" for level, _ in self.lines)


def tool_prefix(server_name: str) -> str:
    """The `mcp__<prefix>__` part Claude Code gives a server's tools: every other character becomes `_`."""
    return SERVER_CHARS.sub("_", server_name)


def probe(root: Path, config: dict[str, Any]) -> dict[str, Any]:
    """One headless turn; returns {servers: [{name, prefix, status, tools}]} from its init event."""
    env = {
        **os.environ,
        "JARVIS_MODE": "bootstrap",
        "JARVIS_RUN_ID": "doctor-probe-canary",
        "JARVIS_ROOT": str(root),
    }
    env.pop("CLAUDECODE", None)
    if config.get("claude_config_dir"):
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config["claude_config_dir"])
    command = [
        os.path.expanduser(config["claude_bin"]),
        "-p",
        "--model",
        str(config.get("canary_model", "haiku")),
        "--max-turns",
        "1",
        "--permission-mode",
        "default",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    result = subprocess.run(
        command,
        input=PROBE_PROMPT,
        capture_output=True,
        text=True,
        env=env,
        cwd=root,
        timeout=PROBE_TIMEOUT_SECONDS,
        check=False,
    )
    return parse_init(result.stdout)


def merge(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Two probes as one: per server the best status seen (connected, then pending, then the rest) and
    every tool either saw."""
    merged: dict[str, dict[str, Any]] = {}
    for server in first["servers"] + second["servers"]:
        known = merged.get(server["prefix"])
        if known is None:
            merged[server["prefix"]] = dict(server)
            continue
        if STATUS_RANK.get(server["status"], 9) < STATUS_RANK.get(known["status"], 9):
            known["status"] = server["status"]
        known["tools"] = sorted(set(known["tools"]) | set(server["tools"]))
    return {"servers": sorted(merged.values(), key=lambda s: s["prefix"])}


def settled_probe(root: Path, config: dict[str, Any], prefixes: set[str]) -> dict[str, Any]:
    """Probe until none of `prefixes` is still pending, at most PROBE_ATTEMPTS times."""
    seen = probe(root, config)
    for _ in range(PROBE_ATTEMPTS - 1):
        if not any(s["status"] == "pending" and s["prefix"] in prefixes for s in seen["servers"]):
            break
        seen = merge(seen, probe(root, config))
    return seen


def parse_init(stream: str) -> dict[str, Any]:
    """The servers and their tools from the first `system/init` event of a stream-json transcript."""
    for line in stream.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "system" and event.get("subtype") == "init":
            return _servers(event)
    raise RuntimeError("the session printed no init event; is claude_bin a working Claude Code?")


def _servers(event: dict[str, Any]) -> dict[str, Any]:
    tools: dict[str, list[str]] = {}
    for tool in event.get("tools") or []:
        parts = tool.split("__", 2)
        if len(parts) == 3 and parts[0] == "mcp":
            tools.setdefault(parts[1], []).append(parts[2])
    servers = []
    for server in event.get("mcp_servers") or []:
        prefix = tool_prefix(server["name"])
        servers.append(
            {
                "name": server["name"],
                "prefix": prefix,
                "status": server.get("status", "unknown"),
                "tools": sorted(tools.get(prefix, [])),
            }
        )
    return {"servers": sorted(servers, key=lambda s: s["prefix"])}


# Checks


def check_claude(config: dict[str, Any], report: Report) -> bool:
    binary = Path(os.path.expanduser(str(config.get("claude_bin", ""))))
    if not (binary.is_file() and os.access(binary, os.X_OK)):
        report.fail(f"claude_bin {binary} is not an executable; set runtime.claude_bin in the profile")
        return False
    version = subprocess.run([str(binary), "--version"], capture_output=True, text=True, check=False)
    report.ok(f"Claude Code {version.stdout.strip() or 'found'} at {binary}")
    return True


def check_guard(root: Path, report: Report) -> None:
    problems = guard_pin.verify(root)
    if problems:
        report.fail(
            "guard pin: " + "; ".join(problems) + " (review the diff, then: uv run scripts/guard_pin.py pin)"
        )
        return
    problems = guard_pin.self_test(root)
    if problems:
        report.fail("guard self-test: " + "; ".join(problems))
    else:
        report.ok("guard pinned and self-test passed")


def check_profile(root: Path, report: Report) -> adapters.Setup | None:
    try:
        setup = adapters.load(root)
    except adapters.InvalidSetup as error:
        for problem in error.problems:
            report.fail(f"profile: {problem}")
        return None
    if not setup.bootstrapped:
        report.fail("no profile yet: run scripts/jarvis bootstrap")
        return None
    stale = bootstrap.changed(root, bootstrap.outputs(root, setup, common.now().date()))
    if stale:
        report.fail(
            f"generated files are out of date ({', '.join(stale)}): uv run scripts/bootstrap.py render"
        )
    else:
        report.ok(f"profile valid: {len(setup.sources)} sources, delivery {setup.delivery.name}")
    return setup


def wanted_servers(setup: adapters.Setup) -> list[tuple[str, tuple[str, ...], tuple[str, ...]]]:
    """(label, acceptable server prefixes, read tool basenames) for every MCP server the setup needs."""
    wanted = []
    for source in setup.sources:
        for server in source.servers:
            wanted.append((f"{source.name} ({server.role})", server.names, server.tools))
    delivery = setup.delivery
    if delivery and delivery.posts:
        names = tuple(tool.split("__")[1] for tool in delivery.tools)
        basename = delivery.tools[0].split("__", 2)[2]
        wanted.append((f"delivery {delivery.name}", names, (basename,)))
    return wanted


def check_servers(setup: adapters.Setup, seen: dict[str, Any], report: Report) -> None:
    """Each needed server is loaded, authenticated, and (when connected) has the adapter's tools."""
    by_prefix = {server["prefix"]: server for server in seen["servers"]}
    for label, names, tools in wanted_servers(setup):
        present = [by_prefix[name] for name in names if name in by_prefix]
        usable = [s for s in present if s["status"] in ("connected", "pending")]
        if not present:
            report.fail(f"{label}: none of {', '.join(names)} is configured in headless Claude Code")
        elif not usable:
            states = ", ".join(f"{s['prefix']} {s['status']}" for s in present)
            report.fail(f"{label}: {states}; authenticate it in `claude` with /mcp")
        else:
            _check_tools(label, usable, tools, report)


def _check_tools(label: str, usable: list[dict[str, Any]], tools: tuple[str, ...], report: Report) -> None:
    connected = [s for s in usable if s["status"] == "connected"]
    if not connected:
        report.warn(f"{label}: {usable[0]['prefix']} kept connecting; its tool names are unverified")
        return
    missing = sorted(set(tools) - {t for s in connected for t in s["tools"]})
    if missing:
        report.warn(
            f"{label}: {connected[0]['prefix']} lacks {', '.join(missing)}; the adapter may be outdated"
        )
    else:
        report.ok(f"{label}: {connected[0]['prefix']} connected with every tool the adapter uses")


def check_readers(setup: adapters.Setup, report: Report) -> None:
    for source in setup.sources:
        for command in source.requires:
            if shutil.which(command) is None:
                report.fail(f"{source.name}: `{command}` is not on PATH")
            elif command == "gh":
                status = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
                if status.returncode != 0:
                    report.fail(f"{source.name}: gh is not logged in; run gh auth login")
                else:
                    report.ok(f"{source.name}: gh is logged in")
        if source.capture == "read" and source.folder:
            report.ok(f"{source.name}: reads {source.folder}/ (create it and add files when ready)")


def check_index(root: Path, config: dict[str, Any], report: Report) -> None:
    backend = (config.get("index") or {}).get("backend", "sqlite")
    if backend == "sqlite":
        report.ok("index: sqlite in state/index.sqlite, no server needed")
        return
    try:
        import index

        with index.connect(index.database_url(root)):
            report.ok("index: postgres reachable")
    except Exception as error:
        report.fail(f"index: postgres not reachable: {str(error).splitlines()[0]} (see scripts/db_setup.sh)")


def check_calendar(root: Path, config: dict[str, Any], report: Report) -> None:
    year = common.now().year
    if not any(day.year == year for day in common.holidays(root)):
        report.warn(f"config/holidays.md lists no date in {year}; set holidays.country or holidays.extra")
    host = subprocess.run(["date", "+%z"], capture_output=True, text=True, check=False).stdout.strip()
    owner = common.now().strftime("%z")
    backend = (config.get("scheduler") or {}).get("backend")
    if backend in ("launchd", "cron") and host and host != owner:
        report.warn(f"this host is at {host} and the owner at {owner}; {backend} fires on host time")
    units = (
        list((root / "config" / "scheduler").glob("*")) if (root / "config" / "scheduler").is_dir() else []
    )
    if backend != "none":
        report.ok(f"schedule: {len(units)} {backend} units rendered; uv run scripts/scheduler.py install")


def main(argv: list[str]) -> int:
    root = common.repo_root()
    config = common.jarvis_config(root) if (root / "config" / "jarvis.yaml").exists() else {}
    if argv == ["tools"]:
        print(json.dumps(probe(root, config), indent=2))
        return 0
    if set(argv) - {"--offline", "--canary"}:
        print(__doc__, file=sys.stderr)
        return 2
    report = Report()
    claude_ok = check_claude(config, report)
    check_guard(root, report)
    setup = check_profile(root, report)
    if setup is not None:
        if claude_ok and "--offline" not in argv:
            try:
                prefixes = {name for _, names, _ in wanted_servers(setup) for name in names}
                check_servers(setup, settled_probe(root, config, prefixes), report)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                report.fail(f"probe: {error}")
        check_readers(setup, report)
        check_index(root, config, report)
        check_calendar(root, config, report)
        if claude_ok and "--canary" in argv:
            import run

            problems = run.canary(root, config, "morning", "doctor")
            if problems:
                report.fail("canary: " + problems[0])
            else:
                report.ok("canary: the hooks are live")
    for level, text in report.lines:
        print(f"{level:<4} {text}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
