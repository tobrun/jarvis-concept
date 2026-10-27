"""Shared helpers for the Jarvis harness: repo paths, the clock, config readers, and jsonl ledgers."""

from __future__ import annotations

import fcntl
import functools
import json
import os
import re
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml

WEEKDAY_INDEX = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
MON_TO_FRI = frozenset(range(5))

KV_LINE = re.compile(r"^([a-z_][a-z0-9_]*):\s*(\S.*?)\s*$")
DATE_LINE = re.compile(r"^\s*(?:[-*]\s+)?(\d{4}-\d{2}-\d{2})\b")


def repo_root() -> Path:
    """The repo a script acts on: JARVIS_ROOT, then the Claude project dir, then this file's repo."""
    for var in ("JARVIS_ROOT", "CLAUDE_PROJECT_DIR"):
        value = os.environ.get(var)
        if value:
            return Path(value).resolve()
    return Path(__file__).resolve().parent.parent


def tz() -> ZoneInfo:
    """The owner's timezone: JARVIS_TZ, then `timezone` in config/jarvis.yaml, then UTC.

    Every date decision (raw file dates, business days, slots, ISO weeks) is made in this zone.
    """
    name = os.environ.get("JARVIS_TZ")
    if not name:
        path = config_dir() / "jarvis.yaml"
        name = (load_yaml(path) or {}).get("timezone") if path.exists() else None
    return _zone(name or "UTC")


@functools.cache
def _zone(name: str) -> ZoneInfo:
    return ZoneInfo(name)


def parse_ts(text: str) -> datetime:
    """Parse an ISO timestamp; one without an offset is local time in the owner's timezone."""
    zone = tz()
    moment = datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=zone)
    return moment.astimezone(zone)


def now() -> datetime:
    """The current instant in the owner's timezone, overridden by JARVIS_NOW for replay and tests."""
    fixed = os.environ.get("JARVIS_NOW")
    if fixed:
        return parse_ts(fixed)
    return datetime.now(tz())


def _scalar(text: str) -> Any:
    for kind in (int, float):
        try:
            return kind(text)
        except ValueError:
            pass
    return text


def read_kv(path: Path) -> dict[str, Any]:
    """Read `key: value` lines from a markdown config; prose, headings, and malformed lines are skipped."""
    values: dict[str, Any] = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        match = KV_LINE.match(line)
        if match:
            value = match.group(2).split("  #", 1)[0].strip()
            values[match.group(1)] = _scalar(value)
    return values


def read_holidays(path: Path) -> set[date]:
    """Dates listed one per line as `YYYY-MM-DD name`, optionally as list items."""
    found: set[date] = set()
    if not path.exists():
        return found
    for line in path.read_text().splitlines():
        match = DATE_LINE.match(line)
        if match:
            found.add(date.fromisoformat(match.group(1)))
    return found


def load_yaml(path: Path) -> Any:
    with path.open() as handle:
        return yaml.safe_load(handle)


def config_dir(root: Path | None = None) -> Path:
    return (root or repo_root()) / "config"


def thresholds(root: Path | None = None) -> dict[str, Any]:
    return read_kv(config_dir(root) / "thresholds.md")


class DaysOff(frozenset):
    """The holidays in config/holidays.md, carrying the owner's working weekdays (Monday is 0)."""

    weekdays: frozenset[int] = MON_TO_FRI

    def __new__(cls, dates: set[date], weekdays: frozenset[int] = MON_TO_FRI) -> DaysOff:
        days_off = super().__new__(cls, dates)
        days_off.weekdays = weekdays
        return days_off


def working_weekdays(root: Path | None = None) -> frozenset[int]:
    """`working_days` in config/jarvis.yaml as weekday numbers; Monday to Friday when unset."""
    path = config_dir(root) / "jarvis.yaml"
    names = (load_yaml(path) or {}).get("working_days") if path.exists() else None
    return frozenset(WEEKDAY_INDEX[name] for name in names) if names else MON_TO_FRI


def holidays(root: Path | None = None) -> DaysOff:
    return DaysOff(read_holidays(config_dir(root) / "holidays.md"), working_weekdays(root))


def jarvis_config(root: Path | None = None) -> dict[str, Any]:
    return load_yaml(config_dir(root) / "jarvis.yaml") or {}


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line under an exclusive lock as a single write, so appenders never interleave."""
    path.parent.mkdir(parents=True, exist_ok=True)
    line = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode()
    while True:
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            # A rewrite (raw.py tombstone) may have renamed a new file over this path while we waited.
            if os.fstat(fd).st_ino != os.stat(path).st_ino:
                continue
            os.write(fd, line)
            return
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def jsonl_lines(text: str) -> list[str]:
    """The lines of a jsonl text, split on "\\n" only.

    str.splitlines also splits on U+2028, U+2029, U+0085 and other separators, which a line written with
    ensure_ascii=False keeps raw inside a JSON string, so it would cut one record in two.
    """
    return text.removesuffix("\n").split("\n") if text else []


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a jsonl file, skipping a truncated or corrupt line instead of failing the whole read."""
    records: list[dict[str, Any]] = []
    if not path.exists():
        return records
    for line in jsonl_lines(path.read_text()):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            print(f"skipping unreadable line in {path}", file=sys.stderr)
    return records


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    """Write JSON atomically: a temp file in the same directory, then a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.tmp")
    temp.write_text(json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    temp.replace(path)
