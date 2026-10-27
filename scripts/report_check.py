"""Check a report against JARVIS.md section 9 before it may be posted.

Usage:
  report_check.py <report file>           check a top-level report; prints one problem per line
  report_check.py --reply <text file>     check a thread reply: no em dash, no exclamation mark, ids resolve

Exit 0 when clean, 1 with problems.
The guard imports `check` and `check_reply` and denies a self-DM post that has any problem.

Report shape:
  line 1     `Jarvis, <morning|midday|eod>, <YYYY-MM-DD> <HH:MM>` optionally followed by `  (late by ...)`
  lines 2..  every open alert in state/alerts.json, by its `text`, before the first section
  sections   Today, Slipping, Waiting on you, Waiting on others, FYI - all five, in that order; a section is
             either its name alone on a line followed by item lines, or `<name>: <one word>` when empty
  items      every non-blank line inside a section; lines indented 4 or more spaces continue the item above.
             Each item carries a `[<source>:<id>]` that resolves in raw/, not tombstoned. FYI: at most 5.
  Sources    a line starting `Sources:` after FYI; anything after it is the weekly addendum and is free form
  overall    under 60 lines, no em dash, no exclamation mark

state/alerts.json is written by run.py: {"alerts": [{"id": str, "kind": str, "text": str, "at": ISO}]}.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import common
import raw

SECTIONS = ("Today", "Slipping", "Waiting on you", "Waiting on others", "FYI")
MAX_LINES = 59
MAX_FYI = 5
HEADER = re.compile(r"^Jarvis, (morning|midday|eod), \d{4}-\d{2}-\d{2} \d{2}:\d{2}(\s+\(late by [^)]+\))?$")
RAW_ID = re.compile(r"\[([a-z]+:[^\]\s]+)\]")
EMPTY_SECTION = re.compile(r"^(?P<name>[A-Za-z ]+):\s+\S+$")
EM_DASH = "\u2014"


def _section_name(line: str) -> str | None:
    stripped = line.strip()
    if line[:1].isspace():
        return None
    if stripped in SECTIONS:
        return stripped
    empty = EMPTY_SECTION.match(stripped)
    if empty and empty.group("name") in SECTIONS:
        return empty.group("name")
    return None


def _id_problems(line_number: int, text: str, root: Path | None) -> list[str]:
    if root is None:
        return []
    problems = []
    for raw_id in RAW_ID.findall(text):
        status = raw.resolve(root, raw_id).status
        if status == "not_found":
            problems.append(f"line {line_number}: [{raw_id}] does not resolve in raw/")
        elif status == "tombstoned":
            problems.append(f"line {line_number}: [{raw_id}] is tombstoned and must not be cited")
        elif status == "invalid":
            problems.append(f"line {line_number}: [{raw_id}] is not a well-formed raw id")
    return problems


def _style_problems(lines: list[str]) -> list[str]:
    problems = []
    for number, line in enumerate(lines, start=1):
        if EM_DASH in line:
            problems.append(f"line {number}: em dash; use a plain dash")
        if "!" in line:
            problems.append(f"line {number}: exclamation mark")
    return problems


def open_alerts(root: Path | None) -> list[str]:
    if root is None:
        return []
    alerts = common.read_json(root / "state" / "alerts.json", {"alerts": []})
    return [alert["text"] for alert in alerts.get("alerts", [])]


def check(text: str, root: Path | None = None) -> list[str]:
    """Problems with a top-level report; empty means it may be posted. `root` enables id and alert checks."""
    lines = text.rstrip("\n").split("\n")
    problems = []
    if len(lines) > MAX_LINES:
        problems.append(f"report has {len(lines)} lines; it must stay under 60")
    if not HEADER.match(lines[0]):
        problems.append("line 1: header must be `Jarvis, <mode>, <YYYY-MM-DD> <HH:MM>`")
    problems += _style_problems(lines)
    seen, items, sources_at = _walk(lines)
    problems += _item_problems(items, root)
    fyi_items = sum(1 for _, _, section in items if section == "FYI")
    problems += _structure_problems(seen, fyi_items, sources_at)
    return problems + _alert_problems(lines, root)


def _walk(lines: list[str]) -> tuple[list[str], list[tuple[int, str, str]], int | None]:
    """The sections in order, the item lines as (line number, line, section), and the Sources line number."""
    seen: list[str] = []
    items: list[tuple[int, str, str]] = []
    current = None
    for number, line in enumerate(lines[1:], start=2):
        if line.startswith("Sources:"):
            return seen, items, number
        name = _section_name(line)
        if name:
            seen.append(name)
            current = name
        elif current is not None and _is_item(line):
            items.append((number, line, current))
    return seen, items, None


def _is_item(line: str) -> bool:
    """A non-blank line that does not continue the item above."""
    return bool(line.strip()) and not line.startswith("    ")


def _item_problems(items: list[tuple[int, str, str]], root: Path | None) -> list[str]:
    problems = []
    for number, line, _ in items:
        if not RAW_ID.search(line):
            problems.append(f"line {number}: item has no [raw_id]")
        problems += _id_problems(number, line, root)
    return problems


def _structure_problems(seen: list[str], fyi_items: int, sources_at: int | None) -> list[str]:
    problems = []
    if seen != list(SECTIONS):
        problems.append(
            f"sections must be {', '.join(SECTIONS)} in that order; found {', '.join(seen) or 'none'}"
        )
    if fyi_items > MAX_FYI:
        problems.append(f"FYI has {fyi_items} items; at most {MAX_FYI}")
    if sources_at is None:
        problems.append("missing the Sources line after FYI")
    return problems


def _alert_problems(lines: list[str], root: Path | None) -> list[str]:
    """Every open alert must appear between the header and the first section."""
    first_section = next((i for i, line in enumerate(lines) if _section_name(line)), len(lines))
    preamble = "\n".join(lines[1:first_section])
    return [
        f"open alert missing before Today: {alert}" for alert in open_alerts(root) if alert not in preamble
    ]


def check_reply(text: str, root: Path | None = None) -> list[str]:
    """Problems with a thread reply: style, and every cited id must resolve."""
    lines = text.rstrip("\n").split("\n")
    problems = _style_problems(lines)
    for number, line in enumerate(lines, start=1):
        problems += _id_problems(number, line, root)
    return problems


def main(argv: list[str]) -> int:
    reply = "--reply" in argv
    paths = [arg for arg in argv if arg != "--reply"]
    if len(paths) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    text = Path(paths[0]).read_text()
    root = common.repo_root()
    return report(check_reply(text, root) if reply else check(text, root))


def report(problems: list[str]) -> int:
    """Print one problem per line; the exit code is 1 when there are any."""
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
