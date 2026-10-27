"""The rendered prompt: every path it names exists, its style rules hold, and CLAUDE.md loads it."""

import re

from conftest import REPO

PATH = re.compile(
    r"(?<![\w/.~])((?:scripts|config|adapters|templates)/[A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)*/?)"
)


def test_every_script_and_config_path_named_in_the_prompt_exists(rendered):
    prompt = (rendered / "JARVIS.md").read_text()
    named = {match.rstrip("./") for match in PATH.findall(prompt)}
    assert "scripts/guard.py" in named
    missing = sorted(path for path in named if not (rendered / path).exists())
    assert missing == []


def test_prompt_and_owner_file_have_no_em_dash_and_no_unfilled_placeholder(rendered):
    for name in ("JARVIS.md", "OWNER.md"):
        text = (rendered / name).read_text()
        assert "\u2014" not in text, name
        assert "{{" not in text and "}}" not in text, name


def test_claude_md_imports_the_prompt_and_the_owner_file():
    assert (REPO / "CLAUDE.md").read_text().split() == ["@JARVIS.md", "@OWNER.md"]
