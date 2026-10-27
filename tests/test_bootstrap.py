"""Adapters, the profile, and scripts/bootstrap.py render: what a new owner's setup turns into."""

from __future__ import annotations

import copy
import shutil
from datetime import date
from pathlib import Path

import adapters
import bootstrap
import common
import guard
import pytest
import yaml
from conftest import EXAMPLE_PROFILE, RENDER_NOW, REPO, copy_shipped, run_script


def example() -> dict:
    return yaml.safe_load(EXAMPLE_PROFILE.read_text())


@pytest.fixture
def kit(tmp_path: Path) -> Path:
    """A fresh clone: the shipped kit with no profile and nothing rendered."""
    root = tmp_path / "kit"
    root.mkdir()
    copy_shipped(root)
    return root


def render_profile(root: Path, profile: dict) -> Path:
    (root / "profile").mkdir(exist_ok=True)
    (root / "profile" / "jarvis.profile.yaml").write_text(yaml.safe_dump(profile, sort_keys=False))
    result = run_script(root, "bootstrap.py", "render", env={"JARVIS_NOW": RENDER_NOW})
    assert result.returncode == 0, result.stderr
    return root


# Adapters


def test_every_shipped_adapter_and_the_example_profile_validate(kit):
    (kit / "profile").mkdir()
    shutil.copy2(EXAMPLE_PROFILE, kit / "profile" / "jarvis.profile.yaml")
    assert bootstrap.validate_all(kit) == []
    assert len(adapters.available(kit, "sources")) >= 10
    assert adapters.available(kit, "delivery") == ["file-only", "gmail-to-self", "slack-self-dm"]


def test_every_shipped_source_adapter_has_a_prompt_and_known_placeholders():
    for name in adapters.available(REPO, "sources"):
        source = adapters.load_source(REPO, name)
        assert source.prompt.strip(), name
        placeholders = set(bootstrap.PLACEHOLDER.findall(source.prompt))
        assert placeholders <= {"principal", "them", "their", "theirs", *source.params}, name


def test_no_source_adapter_lists_a_tool_as_both_read_and_forbidden():
    for name in adapters.available(REPO, "sources"):
        source = adapters.load_source(REPO, name)
        assert not set(source.tools) & set(source.forbidden), name


def test_the_template_adapter_validates_when_copied_under_its_own_name(kit):
    target = kit / "adapters" / "sources" / "example"
    shutil.copytree(kit / "adapters" / "sources" / "_template", target)
    source = adapters.load_source(kit, "example")
    assert source.tools == ["mcp__example_server__list_items", "mcp__example_server__get_item"]


@pytest.mark.parametrize(
    ("field", "value", "problem"),
    [
        ("id", "slack:<channel><ts>", "separated by literal text"),
        ("id", "other:<channel>:<ts>", "must equal the raw source"),
        ("match", "fuzzy", "match must be one of"),
        ("capture", "webhook", "capture must be one of"),
    ],
)
def test_a_malformed_adapter_is_refused_naming_the_problem(kit, field, value, problem):
    path = kit / "adapters" / "sources" / "slack" / "adapter.yaml"
    spec = yaml.safe_load(path.read_text())
    spec[field] = value
    path.write_text(yaml.safe_dump(spec))
    with pytest.raises(adapters.InvalidSetup, match=problem):
        adapters.load_source(kit, "slack")


def test_profile_server_names_replace_the_candidates():
    source = adapters.load_source(REPO, "gmail", {"servers": {"gmail": ["my_mail"]}})
    assert source.tools[0] == "mcp__my_mail__get_thread"
    assert all(tool.startswith("mcp__my_mail__") for tool in source.tools + source.forbidden)


def test_unknown_params_and_unknown_adapters_are_refused(kit):
    with pytest.raises(adapters.InvalidSetup, match="unknown params"):
        adapters.load_source(kit, "jira", {"params": {"board": "X"}})
    profile = example()
    profile["sources"]["teams"] = {}
    with pytest.raises(adapters.InvalidSetup, match="no sources adapter named 'teams'"):
        adapters.load(kit, profile)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda p: p["principal"].pop("role"), "principal.role is required"),
        (lambda p: p.update(timezone="Mars/Olympus"), "not an IANA zone"),
        (lambda p: p["working_hours"].update(days=["someday"]), "working_hours.days"),
        (lambda p: p["delivery"].pop("target"), "must set delivery.target"),
        (lambda p: p.update(delivery={}), "delivery.adapter is required"),
    ],
)
def test_a_malformed_profile_is_refused_naming_the_problem(kit, change, problem):
    profile = example()
    change(profile)
    with pytest.raises(adapters.InvalidSetup, match=problem):
        adapters.load(kit, profile)


# Render


def test_without_a_profile_render_writes_a_skeleton_only_bootstrap_and_dev_can_use(kit):
    result = run_script(kit, "bootstrap.py", "render", env={"JARVIS_NOW": RENDER_NOW})
    assert result.returncode == 0, result.stderr
    config = common.jarvis_config(kit)
    assert config["bootstrapped"] is False
    assert "not bootstrapped" in (kit / "JARVIS.md").read_text()
    assert not (kit / "OWNER.md").exists()
    policy = guard.load_policy(kit)
    assert policy["groups"]["sources"] == [] and policy["groups"]["post"] == []
    assert not list((kit / "config").glob("scheduler/*"))


def test_render_is_deterministic_and_diff_reports_up_to_date(kit):
    render_profile(kit, example())
    first = {p: p.read_bytes() for p in sorted(kit.rglob("*")) if p.is_file() and "__pycache__" not in str(p)}
    render_profile(kit, example())
    second = {
        p: p.read_bytes() for p in sorted(kit.rglob("*")) if p.is_file() and "__pycache__" not in str(p)
    }
    assert first == second
    result = run_script(kit, "bootstrap.py", "diff", env={"JARVIS_NOW": RENDER_NOW})
    assert (result.returncode, result.stdout.strip()) == (0, "up to date")


def test_owner_file_and_thresholds_survive_a_re_render_and_new_staleness_keys_are_appended(kit):
    profile = example()
    del profile["sources"]["github"]
    render_profile(kit, profile)
    (kit / "OWNER.md").write_text("# Owner\n\nmine\n")
    thresholds = kit / "config" / "thresholds.md"
    thresholds.write_text(thresholds.read_text().replace("surface_threshold: 0.6", "surface_threshold: 0.7"))
    assert "stale_pr_review_days" not in thresholds.read_text()

    render_profile(kit, example())
    assert (kit / "OWNER.md").read_text() == "# Owner\n\nmine\n"
    values = common.read_kv(thresholds)
    assert values["surface_threshold"] == 0.7
    assert values["stale_pr_review_days"] == 3


def test_sources_render_in_adapter_order_with_their_params_and_id_forms(rendered):
    prompt = (rendered / "JARVIS.md").read_text()
    calendar, jira, slack = (
        prompt.index(f"Cite as `{f}`") for f in ("calendar:<event>", "jira:<issue>", "slack:<channel>:<ts>")
    )
    assert calendar < jira < slack
    assert "Jira projects `PLAT`" in prompt
    assert "repos `example-org/platform`" in prompt
    sources = common.load_yaml(rendered / "config" / "sources.yaml")["sources"]
    assert sources["github"] == {
        "adapter": "github",
        "id": "github:<repo>#<number>",
        "match": "github",
        "bash": ["uv", "run", "scripts/gh_read.py"],
    }
    assert sources["notes"]["read"] == "notes"


def test_a_bash_reader_is_allowed_only_in_modes_that_read_sources(rendered):
    policy = guard.load_policy(rendered)
    reader = ["uv", "run", "scripts/gh_read.py"]
    for mode, spec in policy["modes"].items():
        if spec["bash"] == "any":
            continue
        assert (reader in spec["bash"]) == ("sources" in spec["tools"]), mode


def test_holidays_come_from_the_country_calendar_plus_the_profile_extras(rendered):
    days = common.read_holidays(rendered / "config" / "holidays.md")
    assert {date(2026, 4, 27), date(2026, 12, 24), date(2027, 4, 27)} <= days


def test_unquoted_yaml_clock_values_render_as_hh_mm(kit):
    profile = example()
    (kit / "profile").mkdir()
    text = yaml.safe_dump(profile, sort_keys=False).replace("midday: '13:00'", "midday: 13:00")
    (kit / "profile" / "jarvis.profile.yaml").write_text(text)
    assert run_script(kit, "bootstrap.py", "render", env={"JARVIS_NOW": RENDER_NOW}).returncode == 0
    assert common.jarvis_config(kit)["schedule"]["midday"] == "13:00"


@pytest.mark.parametrize(
    ("pronouns", "word"), [("they", "their plate"), ("he", "his plate"), ("she", "her plate")]
)
def test_pronouns_set_the_possessive_in_the_prompt(kit, pronouns, word):
    profile = example()
    profile["principal"]["pronouns"] = pronouns
    render_profile(kit, profile)
    assert f"Take work off {word}" in (kit / "JARVIS.md").read_text()


def test_personal_scope_changes_the_mission_and_the_privacy_rule(kit):
    profile = example()
    profile["principal"].update(scope="personal", role="runs a household of four")
    render_profile(kit, profile)
    prompt = (kit / "JARVIS.md").read_text()
    assert "personal life admin, not a job" in prompt
    assert "other than Ada's own, which you track only as far as Ada asks" in prompt


# Delivery variants


def deliver(kit: Path, delivery: dict) -> dict:
    profile = example()
    profile["delivery"] = delivery
    render_profile(kit, profile)
    return guard.load_policy(kit)


def test_file_only_delivery_has_no_post_tool_and_says_so(kit):
    policy = deliver(kit, {"adapter": "file-only"})
    assert policy["groups"]["post"] == [] and policy["constrained"] == {}
    assert common.jarvis_config(kit)["delivery"] == {"adapter": "file-only", "posts": False}
    prompt = (kit / "JARVIS.md").read_text()
    assert "You have no delivery tool at all." in prompt
    assert "Do not send the report anywhere." in prompt


def test_gmail_to_self_targets_the_principal_address_as_a_list(kit):
    policy = deliver(kit, {"adapter": "gmail-to-self"})
    spec = policy["constrained"]["mcp__claude_ai_Gmail__send_message"]
    assert spec["target"] == ["ada@example.com"]
    assert spec["keys"] == ["to", "subject", "body", "replyThreadId"]
    assert "an email to Ada's own address" in (kit / "JARVIS.md").read_text()


def test_slack_delivery_names_the_self_dm_and_both_server_names(rendered):
    policy = guard.load_policy(rendered)
    assert set(policy["constrained"]) == {
        "mcp__claude_ai_Slack__slack_send_message",
        "mcp__plugin_enterprise-search_slack__slack_send_message",
    }
    assert {spec["target"] for spec in policy["constrained"].values()} == {"U0EXAMPLE01"}


def test_render_does_not_mutate_the_example_profile():
    before = example()
    setup = adapters.load(REPO, copy.deepcopy(before))
    bootstrap.prompt_values(REPO, setup)
    assert example() == before
