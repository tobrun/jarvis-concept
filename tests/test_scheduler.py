"""Scheduled job units for launchd, systemd, and cron (scripts/scheduler.py)."""

from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

import common
import pytest
import scheduler


def config_for(repo: Path, backend: str, **changes) -> dict:
    config = common.jarvis_config(repo)
    config["scheduler"] = {**config["scheduler"], "backend": backend}
    config.update(changes)
    return config


def test_the_example_renders_one_launchd_plist_per_job(rendered):
    units = sorted(p.name for p in (rendered / "config" / "scheduler").iterdir())
    assert units == [f"com.example.jarvis.{mode}.plist" for mode in sorted(scheduler.JOBS)]


def test_launchd_jobs_fire_on_working_days_at_their_slots(repo):
    files = scheduler.render(repo, config_for(repo, "launchd"))
    morning = plistlib.loads(files["config/scheduler/com.example.jarvis.morning.plist"].encode())
    assert morning["ProgramArguments"] == ["/bin/sh", str(repo / "scripts" / "run.sh"), "morning"]
    assert morning["StartCalendarInterval"] == [
        {"Weekday": d, "Hour": 8, "Minute": 0} for d in (1, 2, 3, 4, 5)
    ]
    dream = plistlib.loads(files["config/scheduler/com.example.jarvis.dream.plist"].encode())
    assert dream["StartCalendarInterval"] == [{"Hour": 2, "Minute": 0}]
    consolidate = plistlib.loads(files["config/scheduler/com.example.jarvis.consolidate.plist"].encode())
    assert len(consolidate["StartCalendarInterval"]) == 5 * 11
    assert {e["Minute"] for e in consolidate["StartCalendarInterval"]} == {30}


@pytest.mark.skipif(shutil.which("plutil") is None, reason="plutil is macOS only")
def test_rendered_plists_pass_plutil_lint(rendered):
    for unit in (rendered / "config" / "scheduler").glob("*.plist"):
        result = subprocess.run(["plutil", "-lint", str(unit)], capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stdout


def test_a_label_without_a_dot_gets_a_reverse_dns_prefix(repo):
    config = config_for(repo, "launchd")
    config["scheduler"]["label"] = "jarvis"
    assert "config/scheduler/local.jarvis.eod.plist" in scheduler.render(repo, config)


def test_systemd_timers_carry_the_owner_timezone_and_working_days(repo):
    config = config_for(repo, "systemd", working_days=["sun", "mon", "tue", "wed", "thu"])
    files = scheduler.render(repo, config)
    timer = files["config/scheduler/com.example.jarvis-morning.timer"]
    assert "OnCalendar=Sun,Mon,Tue,Wed,Thu *-*-* 08:00:00 Europe/Amsterdam" in timer
    assert "Persistent=true" in timer
    service = files["config/scheduler/com.example.jarvis-morning.service"]
    assert f"ExecStart=/bin/sh {repo}/scripts/run.sh morning" in service
    dream = files["config/scheduler/com.example.jarvis-dream.timer"]
    assert "OnCalendar=*-*-* 02:00:00 Europe/Amsterdam" in dream
    assert len(files) == 2 * len(scheduler.JOBS)


def test_cron_renders_one_marked_block(repo):
    files = scheduler.render(repo, config_for(repo, "cron"))
    block = files["config/scheduler/crontab"]
    lines = block.splitlines()
    assert lines[0] == "# BEGIN jarvis com.example.jarvis" and lines[-1] == "# END jarvis com.example.jarvis"
    assert (
        f"0 8 * * 1,2,3,4,5 /bin/sh {repo}/scripts/run.sh morning >> {repo}/state/scheduler-morning.log 2>&1"
        in lines
    )
    assert sum(1 for line in lines if "run.sh consolidate" in line) == 11


def test_backend_none_and_an_unbootstrapped_config_render_nothing(repo):
    assert scheduler.render(repo, config_for(repo, "none")) == {}
    assert scheduler.render(repo, config_for(repo, "launchd", bootstrapped=False)) == {}


def test_crontab_without_keeps_everything_outside_the_block(monkeypatch):
    current = "MAILTO=me\n# BEGIN jarvis x\n0 8 * * 1 job\n# END jarvis x\n5 * * * * other\n"

    def fake_run(command, stdin=None):
        return subprocess.CompletedProcess(command, 0, stdout=current, stderr="")

    monkeypatch.setattr(scheduler, "_run", fake_run)
    assert scheduler._crontab_without("x") == "MAILTO=me\n5 * * * * other\n"
