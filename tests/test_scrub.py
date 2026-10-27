"""The kit carries no trace of the Jarvis it was extracted from: no owner, company, ids, or local paths."""

from __future__ import annotations

import hashlib
import re

from conftest import REPO

# Hashed so this file does not name what it forbids: the original owner, company, internal
# project keys and conversation ids, local user, and config directory. sha256 of the lowercased
# word, first 16 hex digits.
FORBIDDEN_WORDS = frozenset(
    {
        "b6551d554a88c381",
        "d82a5b234cd2e5aa",
        "d0f65a6c5bbfb68f",
        "77b7a3add8830234",
        "19b1b9c13d9f4d65",
        "40ac8f96a38af43e",
        "1fca8f6ca1ddb5d4",
        "09185625f06bec33",
        "33347c71b099cb55",
        "7c59f71bb7ccfdb0",
    }
)
WORD = re.compile(r"[a-z0-9]+")
# Anything shaped like a real Slack user, channel, or DM id must be one of the kit's made-up ones.
SLACK_ID = re.compile(r"\b[UCDGW]0[A-Z0-9]{6,10}\b")
EXAMPLE_SLACK_IDS = frozenset({"U0EXAMPLE01", "U0123456789"})


def digest(word):
    return hashlib.sha256(word.encode()).hexdigest()[:16]


# What the kit ships. An owner's fork adds their profile and rendered files, which name them by design.
KIT_PATHS = (".claude", "adapters", "docs", "examples", "scripts", "templates", "tests", "tools")
KIT_FILES = ("README.md", "CLAUDE.md", "pyproject.toml", ".gitignore")


def tracked_text_files():
    paths = [REPO / name for name in KIT_FILES]
    paths += [
        p for name in KIT_PATHS for p in sorted((REPO / name).rglob("*")) if "__pycache__" not in p.parts
    ]
    for path in paths:
        if path.is_file():
            try:
                yield path.relative_to(REPO), path.read_text()
            except UnicodeDecodeError:
                continue


def test_no_file_names_the_original_owner_company_or_their_ids():
    hits = [
        f"{relative}:{number}"
        for relative, text in tracked_text_files()
        for number, line in enumerate(text.splitlines(), 1)
        if any(digest(word) in FORBIDDEN_WORDS for word in WORD.findall(line.lower()))
    ]
    assert hits == []


def test_slack_ids_are_made_up():
    found = {match for _, text in tracked_text_files() for match in SLACK_ID.findall(text)}
    assert found <= EXAMPLE_SLACK_IDS


def test_no_file_uses_an_em_dash():
    assert [str(relative) for relative, text in tracked_text_files() if "\u2014" in text] == []
