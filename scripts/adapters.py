"""Source and delivery adapters, and the owner profile that selects and parameterizes them.

Adapters live in adapters/sources/<name>/ and adapters/delivery/<name>/: an adapter.yaml and a prompt.md.
The profile is profile/jarvis.profile.yaml. Nothing here writes files; scripts/bootstrap.py renders from
the `Setup` this module builds. docs/writing-an-adapter.md documents every field checked below.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import common

SOURCE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
ADAPTER_NAME = re.compile(r"^[a-z][a-z0-9-]*$")
ID_TEMPLATE = re.compile(r"^([a-z][a-z0-9_]*):(.+)$")
ID_PART = re.compile(r"<([a-z_]+)>")
SERVER_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
TOOL_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
MATCHERS = ("parts", "github")
CAPTURES = ("mcp", "bash", "read")
SCOPES = ("work", "personal", "mixed")
INDEX_BACKENDS = ("sqlite", "postgres")
SCHEDULERS = ("launchd", "systemd", "cron", "none")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


class InvalidSetup(Exception):
    """The profile or an adapter is malformed; the message lists every problem found."""

    def __init__(self, problems: list[str]):
        super().__init__("\n".join(problems))
        self.problems = problems


@dataclass(frozen=True)
class Server:
    role: str
    names: tuple[str, ...]
    tools: tuple[str, ...]
    forbidden: tuple[str, ...]

    def full(self, basenames: tuple[str, ...]) -> list[str]:
        return [f"mcp__{name}__{tool}" for name in self.names for tool in basenames]


@dataclass(frozen=True)
class Source:
    name: str
    title: str
    description: str
    order: int
    source: str
    id_template: str
    match: str
    capture: str
    servers: tuple[Server, ...]
    bash: tuple[str, ...]
    folder: str | None
    staleness: dict[str, dict[str, Any]]
    params: dict[str, Any]
    prompt: str
    requires: tuple[str, ...]

    @property
    def tools(self) -> list[str]:
        return [tool for server in self.servers for tool in server.full(server.tools)]

    @property
    def forbidden(self) -> list[str]:
        return [tool for server in self.servers for tool in server.full(server.forbidden)]


@dataclass(frozen=True)
class Delivery:
    name: str
    title: str
    tools: tuple[str, ...]
    keys: tuple[str, ...]
    target_key: str | None
    target_shape: str
    target: str | None
    message_key: str | None
    thread_key: str | None
    reactions: bool
    place: str
    prompt: str

    @property
    def posts(self) -> bool:
        return bool(self.tools)


@dataclass
class Setup:
    """Everything render needs: the profile, the enabled sources in reading order, and the delivery."""

    profile: dict[str, Any]
    sources: list[Source] = field(default_factory=list)
    delivery: Delivery | None = None

    @property
    def bootstrapped(self) -> bool:
        return bool(self.profile)


def adapters_dir(root: Path) -> Path:
    return root / "adapters"


def profile_path(root: Path) -> Path:
    return root / "profile" / "jarvis.profile.yaml"


def available(root: Path, kind: str) -> list[str]:
    """Adapter names of one kind (`sources` or `delivery`), the template excluded."""
    base = adapters_dir(root) / kind
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if (p / "adapter.yaml").is_file() and p.name != "_template")


def _read_adapter(root: Path, kind: str, name: str) -> tuple[dict[str, Any], str]:
    folder = adapters_dir(root) / kind / name
    if not (folder / "adapter.yaml").is_file():
        raise InvalidSetup([f"no {kind} adapter named {name!r} (adapters/{kind}/{name}/adapter.yaml)"])
    spec = common.load_yaml(folder / "adapter.yaml") or {}
    prompt = (folder / "prompt.md").read_text() if (folder / "prompt.md").is_file() else ""
    return spec, prompt


def _names(value: Any, label: str, pattern: re.Pattern[str], problems: list[str]) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or not all(isinstance(v, str) and pattern.match(v) for v in value):
        problems.append(f"{label} must be a list of names matching {pattern.pattern}")
        return ()
    return tuple(value)


def id_parts(template: str) -> list[str]:
    """The `<part>` names of an id template after its source prefix."""
    match = ID_TEMPLATE.match(template)
    return ID_PART.findall(match.group(2)) if match else []


def _check_id(name: str, source: str, template: Any, problems: list[str]) -> None:
    match = ID_TEMPLATE.match(template) if isinstance(template, str) else None
    if not match:
        problems.append(f"{name}: id must look like '<source>:<part>...', got {template!r}")
        return
    if match.group(1) != source:
        problems.append(f"{name}: id prefix {match.group(1)!r} must equal the raw source {source!r}")
    rest = match.group(2)
    parts = ID_PART.findall(rest)
    if not parts:
        problems.append(f"{name}: id {template!r} has no <part>")
    elif re.search(r">\s*<", rest) or not re.fullmatch(r"(?:<[a-z_]+>[^<>]+)*<[a-z_]+>", rest):
        problems.append(f"{name}: id parts must be separated by literal text and the id must end with a part")


def _servers(name: str, spec: Any, chosen: dict[str, Any], problems: list[str]) -> tuple[Server, ...]:
    if spec is None:
        return ()
    if not isinstance(spec, dict) or not spec:
        problems.append(f"{name}: servers must map a role to its candidates, tools, and forbidden tools")
        return ()
    servers = []
    for role, server in spec.items():
        label = f"{name}.servers.{role}"
        if not isinstance(server, dict):
            problems.append(f"{label} must be a mapping")
            continue
        candidates = _names(server.get("candidates"), f"{label}.candidates", SERVER_NAME, problems)
        names = _names(chosen.get(role), f"profile servers.{role} of {name}", SERVER_NAME, problems)
        tools = _names(server.get("tools"), f"{label}.tools", TOOL_NAME, problems)
        forbidden = _names(server.get("forbidden"), f"{label}.forbidden", TOOL_NAME, problems)
        if set(tools) & set(forbidden):
            problems.append(f"{label}: {sorted(set(tools) & set(forbidden))} are both tools and forbidden")
        if not (names or candidates):
            problems.append(f"{label} has no candidates and the profile names no server")
        servers.append(Server(role, names or candidates, tools, forbidden))
    return tuple(servers)


def _staleness(name: str, spec: Any, problems: list[str]) -> dict[str, dict[str, Any]]:
    spec = spec or {}
    if not isinstance(spec, dict):
        problems.append(f"{name}: staleness must map a loop kind to {{days, description}}")
        return {}
    for kind, rule in spec.items():
        if (
            not SOURCE_NAME.match(str(kind))
            or not isinstance(rule, dict)
            or not isinstance(rule.get("days"), int)
        ):
            problems.append(f"{name}: staleness.{kind} needs a snake_case kind and integer days")
    return spec


def load_source(root: Path, name: str, chosen: dict[str, Any] | None = None) -> Source:
    """One source adapter with the profile's choices (`servers`, `params`) applied; InvalidSetup when bad."""
    chosen = chosen or {}
    spec, prompt = _read_adapter(root, "sources", name)
    problems: list[str] = []
    source = spec.get("source") or name
    if spec.get("name") != name:
        problems.append(f"{name}: adapter.yaml name must be {name!r}")
    if not SOURCE_NAME.match(str(source)):
        problems.append(f"{name}: raw source {source!r} must match {SOURCE_NAME.pattern}; set `source:`")
    _check_id(name, str(source), spec.get("id"), problems)
    match = spec.get("match", "parts")
    if match not in MATCHERS:
        problems.append(f"{name}: match must be one of {MATCHERS}")
    capture = spec.get("capture", "mcp")
    if capture not in CAPTURES:
        problems.append(f"{name}: capture must be one of {CAPTURES}")
    servers = _servers(name, spec.get("servers"), chosen.get("servers") or {}, problems)
    bash = _names(spec.get("bash"), f"{name}.bash", re.compile(r"^\S+$"), problems)
    if capture == "mcp" and not servers:
        problems.append(f"{name}: capture mcp needs servers")
    if capture == "bash" and (not bash or bash[:2] != ("uv", "run") or not bash[2:3]):
        problems.append(f"{name}: capture bash needs `bash: [uv, run, scripts/<reader>.py]`")
    if capture == "read" and not isinstance(spec.get("folder"), str):
        problems.append(f"{name}: capture read needs `folder:`")
    params = _params(name, spec.get("params") or {}, chosen.get("params") or {}, problems)
    staleness = _staleness(name, spec.get("staleness"), problems)
    if problems:
        raise InvalidSetup(problems)
    return Source(
        name=name,
        title=str(spec.get("title", name)),
        description=str(spec.get("description", "")),
        order=int(chosen.get("order", spec.get("order", 100))),
        source=str(source),
        id_template=str(spec["id"]),
        match=match,
        capture=capture,
        servers=servers,
        bash=bash,
        folder=spec.get("folder"),
        staleness=staleness,
        params=params,
        prompt=prompt,
        requires=tuple(spec.get("requires") or ()),
    )


def _params(
    name: str, declared: dict[str, Any], given: dict[str, Any], problems: list[str]
) -> dict[str, Any]:
    """The profile's params for an adapter: only declared ones, lists where a list is declared."""
    unknown = set(given) - set(declared)
    if unknown:
        problems.append(f"{name}: unknown params {sorted(unknown)}; declared: {sorted(declared)}")
    values: dict[str, Any] = {}
    for key, rule in declared.items():
        value = given.get(key, (rule or {}).get("default", [] if (rule or {}).get("type") == "list" else ""))
        if (rule or {}).get("type") == "list" and not isinstance(value, list):
            problems.append(f"{name}: param {key} must be a list")
        values[key] = value
    return values


def load_delivery(root: Path, chosen: dict[str, Any], principal: dict[str, Any]) -> Delivery:
    name = chosen.get("adapter", "")
    spec, prompt = _read_adapter(root, "delivery", name)
    problems: list[str] = []
    server = spec.get("server")
    tools: tuple[str, ...] = ()
    if server:
        names = _names(
            chosen.get("servers") or server.get("candidates"), f"{name} servers", SERVER_NAME, problems
        )
        tools = tuple(f"mcp__{n}__{server.get('tool')}" for n in names)
        for key in ("keys", "target_key", "message_key"):
            if not spec.get(key):
                problems.append(f"delivery {name}: a posting adapter needs `{key}`")
    target = chosen.get("target")
    if server and not target and (spec.get("target") or {}).get("default") == "principal_email":
        target = next(iter(principal.get("emails") or []), None)
    if server and not target:
        problems.append(
            f"delivery {name}: the profile must set delivery.target: "
            + str((spec.get("target") or {}).get("description", ""))
        )
    if problems:
        raise InvalidSetup(problems)
    return Delivery(
        name=name,
        title=str(spec.get("title", name)),
        tools=tools,
        keys=tuple(spec.get("keys") or ()),
        target_key=spec.get("target_key"),
        target_shape=spec.get("target_shape", "scalar"),
        target=str(target) if target else None,
        message_key=spec.get("message_key"),
        thread_key=spec.get("thread_key"),
        reactions=bool(spec.get("reactions")),
        place=str(spec.get("place", name)),
        prompt=prompt,
    )


def _check_profile(profile: dict[str, Any]) -> list[str]:
    problems = []
    principal = profile.get("principal") or {}
    for key in ("name", "role"):
        if not principal.get(key):
            problems.append(f"profile: principal.{key} is required")
    if principal.get("scope", "work") not in SCOPES:
        problems.append(f"profile: principal.scope must be one of {SCOPES}")
    try:
        ZoneInfo(str(profile.get("timezone", "")))
    except (ValueError, KeyError, ZoneInfoNotFoundError):
        problems.append(f"profile: timezone {profile.get('timezone')!r} is not an IANA zone")
    days = (profile.get("working_hours") or {}).get("days", list(WEEKDAYS[:5]))
    if not days or not set(days) <= set(WEEKDAYS):
        problems.append(f"profile: working_hours.days must be a non-empty subset of {WEEKDAYS}")
    if (profile.get("index") or {}).get("backend", "sqlite") not in INDEX_BACKENDS:
        problems.append(f"profile: index.backend must be one of {INDEX_BACKENDS}")
    if (profile.get("scheduler") or {}).get("backend", "launchd") not in SCHEDULERS:
        problems.append(f"profile: scheduler.backend must be one of {SCHEDULERS}")
    if not isinstance(profile.get("sources") or {}, dict):
        problems.append("profile: sources must map adapter names to their settings")
    if not (profile.get("delivery") or {}).get("adapter"):
        problems.append("profile: delivery.adapter is required (file-only sends nothing)")
    return problems


def load(root: Path, profile: dict[str, Any] | None = None) -> Setup:
    """The setup for a profile (default: profile/jarvis.profile.yaml); an empty Setup before bootstrap."""
    if profile is None:
        path = profile_path(root)
        profile = (common.load_yaml(path) or {}) if path.exists() else {}
    if not profile:
        return Setup({})
    problems = _check_profile(profile)
    sources: list[Source] = []
    delivery = None
    for name, chosen in (profile.get("sources") or {}).items():
        try:
            sources.append(load_source(root, name, chosen or {}))
        except InvalidSetup as error:
            problems += error.problems
    try:
        delivery = load_delivery(root, profile.get("delivery") or {}, profile.get("principal") or {})
    except InvalidSetup as error:
        problems += error.problems
    raw_sources = [s.source for s in sources]
    duplicates = sorted({s for s in raw_sources if raw_sources.count(s) > 1})
    if duplicates:
        problems.append(f"profile: two adapters write the same raw source {duplicates}")
    if problems:
        raise InvalidSetup(problems)
    sources.sort(key=lambda s: (s.order, s.name))
    return Setup(profile, sources, delivery)
