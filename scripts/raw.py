"""The raw layer: resolve cited ids, and tombstone or forget items already captured.

Usage:
  raw.py resolve <raw_id>                         print the latest raw line holding the item, as JSON
  raw.py tombstone <raw_id> --reason hr|personal  register the item and redact it in raw/<source>/ and in
                                                  every golden copy, eval/golden/*/raw/<source>/
  raw.py forget <raw_id>                          tombstone with reason forget and stage a retraction

Ids are `<source>:<native id>` for an item, in the form its adapter declares as `id` in config/sources.yaml
(slack:<channel>:<ts>, github:<repo>#<number>, ...; a source not listed there has one part), or
`<source>:call:<hash>` for a whole captured call. The adapter's `match` picks how a line is searched:
`parts` (below) or `github`.
An item resolves to the newest line in raw/<source>/*.jsonl whose payload or input holds every id part as a
whole token of a key or scalar value: equal to it, or inside a text (a permalink, plain text, JSON in a text
content block) with no word character, and no `.` or `-` joined to one, on either side. So jira:PLAT-12 does
not resolve from PLAT-123, nor github:o/r#5 from #15. A github-matched id names an issue or pull by repo and
number together: an object is the item when its own number (its `number` beside an issue or pull url, else the
/issues/<n> or /pull/<n> of its url fields) is the number and the repository its own url fields, full_name,
or nested repository name is the repo, falling back to its enclosing objects and the call input when it names
none; a plain text outside such objects holds it as <owner>/<repo>#<n>, as its github url, or as #<n> beside a
mention of the repo in the text or the call input. A call id resolves by exact raw_id.

`resolve(root, raw_id)` returns a `Resolution` whose `status` is one of:
  found       `line` is the raw line
  tombstoned  `tombstone` is the registry entry from state/tombstones.jsonl; the item must not be cited
  not_found   no raw line holds the item
  invalid     the id is not a well-formed raw id
The CLI exits 0 found, 1 not found, 2 usage or invalid id, 3 tombstoned.

Tombstoning appends {raw_id, content_sha256, reason, at} to state/tombstones.jsonl, then rewrites every line
holding the item, in raw/ and in every golden week's copy: each JSON object that carries the item (its last
id part as a value, the other parts tokens in it or its enclosing objects or the call input; for github, the
issue or pull object itself) becomes
{tombstone, content_sha256, reason}; JSON inside a text content block is parsed and rewritten in place; when
no object matches (plain text, or a call id) the whole payload becomes {tombstone, scope: call,
content_sha256, reason}. An item held only in a golden week is tombstoned there too. capture.py applies the
same registry before every write, and freeze_golden.py and replay.py before every copy, so a re-fetch or a
copy never lands verbatim.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import common

RAW_ID = re.compile(r"^([a-z][a-z0-9_]*):(.+)$")
GITHUB_ITEM_URL = re.compile(r"/(?:issues|pull|pulls)/(\d+)(?!\w)")
GITHUB_REPO_URL = re.compile(r"(?:github\.com/repos/|github\.com/|/repos/)([\w.-]+/[\w.-]+)")
GITHUB_REPO_KEYS = ("full_name", "nameWithOwner")
REASONS = ("hr", "personal")


@dataclass(frozen=True)
class RawId:
    source: str
    parts: tuple[str, ...]
    call: bool
    text: str
    match: str = "parts"

    @property
    def github(self) -> bool:
        return self.match == "github"


@dataclass(frozen=True)
class Resolution:
    status: str
    line: dict[str, Any] | None = None
    tombstone: dict[str, Any] | None = None

    @property
    def found(self) -> bool:
        return self.status == "found"


def id_pattern(template: str) -> re.Pattern[str]:
    """The regex for the part after `<source>:` of an id template: each `<part>` a group, the rest literal.

    Every part but the last is the shortest run up to the literal that follows it; the last takes the rest.
    """
    rest = template.split(":", 1)[1]
    pieces = re.split(r"<[a-z_]+>", rest)
    count = len(pieces) - 1
    body = "".join(
        re.escape(literal) + ("" if index == count else "(.+)" if index == count - 1 else "(.+?)")
        for index, literal in enumerate(pieces)
    )
    return re.compile(rf"^{body}$")


@lru_cache(maxsize=16)
def _id_specs(path: str, mtime_ns: int) -> dict[str, tuple[re.Pattern[str], str]]:
    del mtime_ns  # part of the cache key only: an edited config/sources.yaml is read again
    sources = (common.load_yaml(Path(path)) or {}).get("sources") or {}
    return {
        source: (id_pattern(spec["id"]), spec.get("match", "parts"))
        for source, spec in sources.items()
        if isinstance(spec, dict) and spec.get("id")
    }


def id_specs(root: Path | None = None) -> dict[str, tuple[re.Pattern[str], str]]:
    """Source -> (id pattern, match strategy) from config/sources.yaml; empty when it does not exist."""
    path = common.config_dir(root) / "sources.yaml"
    if not path.exists():
        return {}
    return _id_specs(str(path), path.stat().st_mtime_ns)


def parse_raw_id(text: str, root: Path | None = None) -> RawId:
    """Split a raw id into its source and the parts a line must contain; ValueError when malformed."""
    match = RAW_ID.match(text)
    if not match:
        raise ValueError(f"not a raw id: {text!r}")
    source, rest = match.group(1), match.group(2)
    if rest.startswith("call:"):
        return RawId(source, (), True, text)
    pattern, strategy = id_specs(root).get(source, (None, "parts"))
    parts = (rest,) if pattern is None else _split(pattern, rest, text)
    if not all(parts):
        raise ValueError(f"not a raw id: {text!r}")
    return RawId(source, parts, False, text, strategy)


def _split(pattern: re.Pattern[str], rest: str, text: str) -> tuple[str, ...]:
    found = pattern.match(rest)
    if not found:
        raise ValueError(f"not a raw id of its source's form: {text!r}")
    return found.groups()


def serialize(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def sha256(value: Any) -> str:
    return hashlib.sha256(serialize(value).encode()).hexdigest()


@lru_cache(maxsize=256)
def _token(part: str) -> re.Pattern[str]:
    """`part` as a whole token: not glued to a word character, nor to a `.` or `-` that joins one."""
    return re.compile(rf"(?<!\w)(?<!\w[.-]){re.escape(part)}(?!\w)(?![.-]\w)")


def holds_token(part: str, text: str) -> bool:
    return _token(part).search(text) is not None


def _strings(value: Any) -> Iterator[str]:
    """Every key and scalar value under `value`, as strings; texts holding JSON are searched whole."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif _is_scalar(value):
        yield str(value)


def _is_scalar(value: Any) -> bool:
    return isinstance(value, str | int | float) and not isinstance(value, bool)


def line_holds(line: dict[str, Any], rid: RawId) -> bool:
    """Whether a raw line carries the item: exact raw_id for a call, each id part a token in its data."""
    if rid.call:
        return line.get("raw_id") == rid.text
    if rid.github:
        return bool(matching_items(line, rid)) or _github_mentioned(line, rid)
    # A channel read keeps the channel in the input and each message ts in the payload, so search both.
    strings = [*_strings(line.get("payload")), *_strings(line.get("input"))]
    return all(any(holds_token(part, s) for s in strings) for part in rid.parts)


def _scalars(value: dict[str, Any]) -> list[str]:
    return [str(v) for v in value.values() if isinstance(v, str | int | float) and not isinstance(v, bool)]


def _as_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return None


def _redact(value: Any, rid: RawId, context: list[str], marker: dict, hits: list) -> Any:
    if isinstance(value, dict):
        return _redact_object(value, rid, context, marker, hits)
    if isinstance(value, list):
        return [_redact(item, rid, context, marker, hits) for item in value]
    if isinstance(value, str):
        return _redact_text(value, rid, context, marker, hits)
    return value


def _carries(value: dict[str, Any], context: list[str], rid: RawId) -> bool:
    """Whether an object is the item: the last id part is its own value, the others are tokens in scope."""
    if rid.github:
        return _github_carries(value, context, rid.parts)
    own = _scalars(value)
    scope = context + own
    return rid.parts[-1] in own and all(any(holds_token(part, s) for s in scope) for part in rid.parts[:-1])


def _scope(value: dict[str, Any], context: list[str], rid: RawId) -> list[str]:
    """What an object adds for the objects inside it: its scalars, or for github the repos it names."""
    return context + (_github_repos(value) if rid.github else _scalars(value))


def _redact_object(value: dict[str, Any], rid: RawId, context: list[str], marker: dict, hits: list) -> Any:
    if _carries(value, context, rid):
        hits.append(value)
        return dict(marker)
    scope = _scope(value, context, rid)
    return {key: _redact(item, rid, scope, marker, hits) for key, item in value.items()}


def _redact_text(value: str, rid: RawId, context: list[str], marker: dict, hits: list) -> str:
    """JSON inside a string is rewritten in place; the string is unchanged when it holds no match."""
    parsed = _as_json(value)
    if parsed is None:
        return value
    before = len(hits)
    rewritten = _redact(parsed, rid, context, marker, hits)
    return json.dumps(rewritten, ensure_ascii=False) if len(hits) > before else value


def _urls(value: dict[str, Any]) -> list[str]:
    return [v for k, v in value.items() if (k == "url" or str(k).endswith("_url")) and isinstance(v, str)]


def _url_matches(pattern: re.Pattern[str], value: dict[str, Any]) -> list[str]:
    """The first group of `pattern` in each url field of an object that holds it."""
    return [match.group(1) for url in _urls(value) if (match := pattern.search(url))]


def _github_number(value: dict[str, Any]) -> str | None:
    """The issue or pull number an object stands for: its `number` unless its urls point elsewhere (a
    milestone), else the number in an issue or pull url (a comment's issue_url); None for anything else."""
    linked = _url_matches(GITHUB_ITEM_URL, value)
    if not _is_scalar(value.get("number")) or (_urls(value) and not linked):
        return next(iter(linked), None)
    return str(value["number"])


def _github_repos(value: dict[str, Any]) -> list[str]:
    """The repositories an object names by its own fields: url fields, full_name, a nested repository."""
    nested = value.get("repository")
    holders = [value, nested] if isinstance(nested, dict) else [value]
    names = [holder.get(key) for holder in holders for key in GITHUB_REPO_KEYS]
    return _url_matches(GITHUB_REPO_URL, value) + [name for name in names if isinstance(name, str)]


def _github_carries(value: dict[str, Any], context: list[str], parts: tuple[str, ...]) -> bool:
    """Whether an object is the issue or pull: its own number, and its own repo, else one named around it."""
    repo, number = parts
    if _github_number(value) != number:
        return False
    repos = _github_repos(value)
    return repo in repos if repos else any(holds_token(repo, s) for s in context)


def _children(value: Any, context: list[str]) -> tuple[list[Any], list[str]]:
    """The values inside a list, or inside an object that is no issue or pull, with the repos it adds."""
    if isinstance(value, list):
        return value, context
    if isinstance(value, dict) and _github_number(value) is None:
        return list(value.values()), context + _github_repos(value)
    return [], context


def _free_texts(value: Any, context: list[str]) -> Iterator[tuple[str, list[str]]]:
    """Every plain text outside an issue or pull object, with the repos its enclosing objects name."""
    parsed = _as_json(value) if isinstance(value, str) else value
    if isinstance(value, str) and parsed is None:
        yield value, context
        return
    items, scope = _children(parsed, context)
    for item in items:
        yield from _free_texts(item, scope)


def _github_mentioned(line: dict[str, Any], rid: RawId) -> bool:
    """Whether a plain text names the item: <owner>/<repo>#<n>, its url, or #<n> beside the repo."""
    repo, number = rid.parts
    url = re.compile(
        rf"(?:github\.com/(?:repos/)?|/repos/){re.escape(repo)}/(?:issues|pull|pulls)/{re.escape(number)}(?!\w)"
    )
    texts = _free_texts(line.get("payload"), _input_context(line))
    return any(_names_item(text, [text, *context], repo, number, url) for text, context in texts)


def _names_item(text: str, scope: list[str], repo: str, number: str, url: re.Pattern[str]) -> bool:
    if holds_token(f"{repo}#{number}", text) or url.search(text):
        return True
    return holds_token(f"#{number}", text) and any(holds_token(repo, s) for s in scope)


def matching_items(line: dict[str, Any], rid: RawId) -> list[Any]:
    """The JSON objects in a line's payload that carry the item; empty for a call id or plain text."""
    if rid.call:
        return []
    hits: list[Any] = []
    _redact(line.get("payload"), rid, _input_context(line), {}, hits)
    return hits


def _input_context(line: dict[str, Any]) -> list[str]:
    """The call input's scalars, plus <owner>/<repo> when it names a github repo by those two keys."""
    value = line.get("input")
    if not isinstance(value, dict):
        return []
    owner, repo = value.get("owner"), value.get("repo")
    named = [f"{owner}/{repo}"] if isinstance(owner, str) and isinstance(repo, str) else []
    return _scalars(value) + named


def redact_line(line: dict[str, Any], rid: RawId, content_sha256: str, reason: str) -> dict[str, Any]:
    """The line with the item tombstoned; unchanged when the line does not carry the item."""
    if not line_holds(line, rid):
        return line
    marker = {"tombstone": True, "content_sha256": content_sha256, "reason": reason}
    hits: list[Any] = []
    payload = None
    if not rid.call:
        payload = _redact(line.get("payload"), rid, _input_context(line), marker, hits)
    if not hits:
        if content_sha256 and content_sha256 in serialize(line.get("payload")):
            return line  # already redacted; a leftover mention of the id must not widen it to the call
        payload = {**marker, "scope": "call"}
    return {**line, "payload": payload}


def apply_registry(
    line: dict[str, Any], registry: list[dict[str, Any]], root: Path | None = None
) -> dict[str, Any]:
    """Redact every registered tombstone of the line's source out of a line about to be written."""
    for entry in registry:
        try:
            rid = parse_raw_id(entry["raw_id"], root)
        except (KeyError, ValueError):
            continue
        if rid.source == line.get("source"):
            line = redact_line(line, rid, entry.get("content_sha256", ""), entry.get("reason", ""))
    return line


def tombstones_path(root: Path) -> Path:
    return root / "state" / "tombstones.jsonl"


def registry(root: Path) -> list[dict[str, Any]]:
    return common.read_jsonl(tombstones_path(root))


def apply_registry_text(text: str, entries: list[dict[str, Any]], root: Path | None = None) -> str:
    """A raw file's text with the registry applied per line; unparsable or untouched lines stay verbatim."""
    out: list[str] = []
    for item in common.jsonl_lines(text):
        try:
            line = json.loads(item)
        except json.JSONDecodeError:
            out.append(item)
            continue
        redacted = apply_registry(line, entries, root) if isinstance(line, dict) else line
        out.append(item if redacted == line else serialize(redacted))
    return "".join(item + "\n" for item in out)


def _source_files(root: Path, source: str) -> list[Path]:
    return sorted((root / "raw" / source).glob("*.jsonl"), reverse=True)


def _golden_files(root: Path, source: str) -> list[Path]:
    """The source's raw files in every golden week; a tombstone must reach these copies too."""
    return sorted((root / "eval" / "golden").glob(f"*/raw/{source}/*.jsonl"), reverse=True)


def resolve(root: Path, raw_id: str) -> Resolution:
    """Find the latest raw line for an id; see the module docstring for the statuses."""
    try:
        rid = parse_raw_id(raw_id, root)
    except ValueError:
        return Resolution("invalid")
    for entry in registry(root):
        if entry.get("raw_id") == raw_id:
            return Resolution("tombstoned", tombstone=entry)
    line = _latest_line(_source_files(root, rid.source), rid)
    return Resolution("found", line=line) if line is not None else Resolution("not_found")


def _latest_line(paths: list[Path], rid: RawId) -> dict[str, Any] | None:
    for path in paths:
        for line in reversed(common.read_jsonl(path)):
            if line_holds(line, rid):
                return line
    return None


def _rewrite(path: Path, rid: RawId, content_sha256: str, reason: str) -> None:
    """Redact the item in one raw file: temp file plus rename, under the same lock appenders take."""
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        text = _redacted_text(path.read_text(), rid, content_sha256, reason)
        if text is not None:
            temp = path.with_name(f".{path.name}.tmp")
            temp.write_text(text)
            temp.replace(path)
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _redacted_text(text: str, rid: RawId, content_sha256: str, reason: str) -> str | None:
    """A raw file's text with the item redacted, or None when no line carries it; the rest stays verbatim."""
    changed = False
    out: list[str] = []
    for item in common.jsonl_lines(text):
        try:
            line = json.loads(item)
        except json.JSONDecodeError:
            out.append(item)
            continue
        redacted = redact_line(line, rid, content_sha256, reason)
        if redacted is line:
            out.append(item)
        else:
            out.append(serialize(redacted))
            changed = True
    return "".join(item + "\n" for item in out) if changed else None


def tombstone(root: Path, raw_id: str, reason: str) -> Resolution:
    """Register and redact an item; an already tombstoned id is redacted again with its first hash."""
    found = _find_anywhere(root, raw_id)
    if found.status in ("invalid", "not_found"):
        return found
    rid = parse_raw_id(raw_id, root)
    if found.status == "tombstoned":
        entry = found.tombstone or {}
    else:
        entry = _register(root, rid, found.line or {}, reason)
    for path in _source_files(root, rid.source) + _golden_files(root, rid.source):
        _rewrite(path, rid, entry.get("content_sha256", ""), entry.get("reason", reason))
    return Resolution("tombstoned", tombstone=entry)


def _find_anywhere(root: Path, raw_id: str) -> Resolution:
    """Resolve in raw/, then in the golden weeks: an item already gone from raw/ is tombstoned there too."""
    found = resolve(root, raw_id)
    if found.status != "not_found":
        return found
    rid = parse_raw_id(raw_id, root)
    line = _latest_line(_golden_files(root, rid.source), rid)
    return found if line is None else Resolution("found", line=line)


def _register(root: Path, rid: RawId, line: dict[str, Any], reason: str) -> dict[str, Any]:
    """Append the registry entry; the hash covers the matched objects, else the whole payload."""
    items = matching_items(line, rid)
    content = items if items else line.get("payload")
    entry = {
        "raw_id": rid.text,
        "content_sha256": sha256(content),
        "reason": reason,
        "at": common.now().isoformat(),
    }
    common.append_jsonl(tombstones_path(root), entry)
    return entry


def forget(root: Path, raw_id: str) -> Resolution:
    """Tombstone with reason forget and stage the id in state/retractions.jsonl for claim retraction."""
    result = tombstone(root, raw_id, "forget")
    if result.status == "tombstoned":
        common.append_jsonl(
            root / "state" / "retractions.jsonl", {"raw_id": raw_id, "at": common.now().isoformat()}
        )
    return result


EXIT = {"found": 0, "not_found": 1, "invalid": 2, "tombstoned": 3}


def _report(result: Resolution, raw_id: str) -> None:
    if result.status == "invalid":
        print(f"invalid raw id: {raw_id}", file=sys.stderr)
    elif result.status == "not_found":
        print(f"not found: {raw_id}", file=sys.stderr)


def _resolve_command(root: Path, raw_id: str) -> int:
    result = resolve(root, raw_id)
    if result.found:
        print(serialize(result.line))
    elif result.status == "tombstoned":
        print(serialize(result.tombstone), file=sys.stderr)
    _report(result, raw_id)
    return EXIT[result.status]


def main(argv: list[str]) -> int:
    root = common.repo_root()
    match argv:
        case ["resolve", raw_id]:
            return _resolve_command(root, raw_id)
        case ["tombstone", raw_id, "--reason", reason] if reason in REASONS:
            result = tombstone(root, raw_id, reason)
        case ["forget", raw_id]:
            result = forget(root, raw_id)
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    if result.status == "tombstoned":
        print(serialize(result.tombstone))
        return 0
    _report(result, raw_id)
    return EXIT[result.status]


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
