"""Render, install, and uninstall the scheduled jobs that start Jarvis runs, for launchd, systemd, or cron.

Usage:
  scheduler.py render       write the units for the configured backend to config/scheduler/
                            (scripts/bootstrap.py render does this too)
  scheduler.py install      install the rendered units for the current user and start them
  scheduler.py uninstall    stop and remove them
  scheduler.py status       print the backend and the rendered unit files

Jobs: morning, midday, eod (working days at their slot), consolidate (working days at consolidate_minute of
every hour in consolidate_hours), and dream (daily). Each runs `/bin/sh <root>/scripts/run.sh <mode>` and
logs to <root>/state/scheduler-<mode>.log.

launchd and cron fire on the host's local time; systemd timers carry the owner's timezone. Whatever fires,
scripts/schedule.py decides the effective mode in the owner's timezone, so a host in another zone fires at
shifted wall times but still plans correctly.

Nothing here installs during bootstrap. Installing starts real runs that read every source and deliver
reports, so the owner runs `install` by hand.
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import common

JOBS = ("morning", "midday", "eod", "consolidate", "dream")
BACKENDS = ("launchd", "systemd", "cron", "none")
DEFAULT_PATH = "/opt/homebrew/bin:/usr/local/bin:{home}/.local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
WEEKDAY_NUMBERS = {"mon": 1, "tue": 2, "wed": 3, "thu": 4, "fri": 5, "sat": 6, "sun": 0}
SYSTEMD_DAYS = {
    "mon": "Mon",
    "tue": "Tue",
    "wed": "Wed",
    "thu": "Thu",
    "fri": "Fri",
    "sat": "Sat",
    "sun": "Sun",
}
CRON_BEGIN = "# BEGIN jarvis {label}"
CRON_END = "# END jarvis {label}"


def default_backend() -> str:
    return "launchd" if sys.platform == "darwin" else "systemd"


def settings(profile: dict[str, Any]) -> dict[str, Any]:
    """The scheduler section of config/jarvis.yaml, from the profile's `scheduler` with defaults."""
    spec = profile.get("scheduler") or {}
    home = str(Path.home())
    return {
        "backend": spec.get("backend") or default_backend(),
        "label": spec.get("label") or "jarvis",
        "path": spec.get("path") or DEFAULT_PATH.format(home=home),
    }


def _hour_minute(text: str) -> tuple[int, int]:
    hour, minute = str(text).split(":")
    return int(hour), int(minute)


def job_times(mode: str, schedule: dict[str, Any]) -> list[tuple[int, int]]:
    """The (hour, minute) firings of a job on a day it runs."""
    if mode == "consolidate":
        minute = int(schedule["consolidate_minute"])
        return [(int(hour), minute) for hour in schedule["consolidate_hours"]]
    return [_hour_minute(schedule[mode])]


def job_days(mode: str, config: dict[str, Any]) -> list[str] | None:
    """The weekdays a job runs on, or None for every day (dream)."""
    return (
        None if mode == "dream" else list(config.get("working_days") or ["mon", "tue", "wed", "thu", "fri"])
    )


def _name(config: dict[str, Any], mode: str) -> str:
    return f"{config['scheduler']['label']}-{mode}"


def _command(root: Path, mode: str) -> list[str]:
    return ["/bin/sh", str(root / "scripts" / "run.sh"), mode]


def _log(root: Path, mode: str) -> str:
    return str(root / "state" / f"scheduler-{mode}.log")


# launchd


def launchd_label(config: dict[str, Any], mode: str) -> str:
    label = config["scheduler"]["label"]
    return f"{label}.{mode}" if "." in label else f"local.{label}.{mode}"


def launchd_plist(root: Path, config: dict[str, Any], mode: str) -> bytes:
    days = job_days(mode, config)
    times = job_times(mode, config["schedule"])
    intervals = [
        {**({"Weekday": WEEKDAY_NUMBERS[day]} if days is not None else {}), "Hour": hour, "Minute": minute}
        for day in (days if days is not None else [None])
        for hour, minute in times
    ]
    plist = {
        "Label": launchd_label(config, mode),
        "ProgramArguments": _command(root, mode),
        "StartCalendarInterval": intervals,
        "EnvironmentVariables": {"PATH": config["scheduler"]["path"], "HOME": str(Path.home())},
        "WorkingDirectory": str(root),
        "StandardOutPath": _log(root, mode),
        "StandardErrorPath": _log(root, mode),
        "RunAtLoad": False,
    }
    return plistlib.dumps(plist, sort_keys=True)


# systemd


def systemd_units(root: Path, config: dict[str, Any], mode: str) -> dict[str, str]:
    name = _name(config, mode)
    days = job_days(mode, config)
    day_spec = ",".join(SYSTEMD_DAYS[day] for day in days) + " " if days is not None else ""
    calendars = "\n".join(
        f"OnCalendar={day_spec}*-*-* {hour:02d}:{minute:02d}:00 {config['timezone']}"
        for hour, minute in job_times(mode, config["schedule"])
    )
    service = (
        "[Unit]\n"
        f"Description=Jarvis {mode} run\n\n"
        "[Service]\n"
        "Type=oneshot\n"
        f"WorkingDirectory={root}\n"
        f"Environment=PATH={config['scheduler']['path']}\n"
        f"ExecStart={' '.join(_command(root, mode))}\n"
        f"StandardOutput=append:{_log(root, mode)}\n"
        f"StandardError=append:{_log(root, mode)}\n"
    )
    timer = (
        "[Unit]\n"
        f"Description=Jarvis {mode} schedule\n\n"
        "[Timer]\n"
        f"{calendars}\n"
        "Persistent=true\n\n"
        "[Install]\n"
        "WantedBy=timers.target\n"
    )
    return {f"{name}.service": service, f"{name}.timer": timer}


# cron


def cron_lines(root: Path, config: dict[str, Any]) -> str:
    """A crontab block for every job; host local time, so the owner's zone should match the host's."""
    label = config["scheduler"]["label"]
    lines = [CRON_BEGIN.format(label=label), f"PATH={config['scheduler']['path']}"]
    for mode in JOBS:
        days = job_days(mode, config)
        day_field = ",".join(str(WEEKDAY_NUMBERS[day]) for day in days) if days is not None else "*"
        for hour, minute in job_times(mode, config["schedule"]):
            command = " ".join(_command(root, mode))
            lines.append(f"{minute} {hour} * * {day_field} {command} >> {_log(root, mode)} 2>&1")
    lines.append(CRON_END.format(label=label))
    return "\n".join(lines) + "\n"


def render(root: Path, config: dict[str, Any]) -> dict[str, str]:
    """The unit files for the configured backend, as config/scheduler/<file> -> text."""
    if not config.get("bootstrapped"):
        return {}
    backend = config["scheduler"]["backend"]
    files: dict[str, str] = {}
    if backend == "launchd":
        for mode in JOBS:
            files[f"config/scheduler/{launchd_label(config, mode)}.plist"] = launchd_plist(
                root, config, mode
            ).decode()
    elif backend == "systemd":
        for mode in JOBS:
            files.update({f"config/scheduler/{n}": t for n, t in systemd_units(root, config, mode).items()})
    elif backend == "cron":
        files["config/scheduler/crontab"] = cron_lines(root, config)
    return files


def _units(root: Path) -> list[Path]:
    folder = root / "config" / "scheduler"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def _run(command: list[str], stdin: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(command, input=stdin, capture_output=True, text=True, check=False)


def _crontab_without(label: str) -> str:
    current = _run(["crontab", "-l"]).stdout
    kept, skipping = [], False
    for line in current.splitlines():
        if line == CRON_BEGIN.format(label=label):
            skipping = True
        elif line == CRON_END.format(label=label):
            skipping = False
        elif not skipping:
            kept.append(line)
    return "\n".join(kept) + ("\n" if kept else "")


def install(root: Path, config: dict[str, Any]) -> list[str]:
    backend = config["scheduler"]["backend"]
    units = _units(root)
    if backend != "none" and not units:
        return ["no rendered units in config/scheduler/; run uv run scripts/bootstrap.py render"]
    problems: list[str] = []
    if backend == "launchd":
        agents = Path.home() / "Library" / "LaunchAgents"
        agents.mkdir(parents=True, exist_ok=True)
        for unit in units:
            target = agents / unit.name
            _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)])
            shutil.copy2(unit, target)
            done = _run(["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)])
            if done.returncode != 0:
                problems.append(f"launchctl bootstrap {unit.name}: {done.stderr.strip()}")
    elif backend == "systemd":
        user_dir = Path.home() / ".config" / "systemd" / "user"
        user_dir.mkdir(parents=True, exist_ok=True)
        for unit in units:
            shutil.copy2(unit, user_dir / unit.name)
        _run(["systemctl", "--user", "daemon-reload"])
        for unit in units:
            if unit.suffix == ".timer":
                done = _run(["systemctl", "--user", "enable", "--now", unit.name])
                if done.returncode != 0:
                    problems.append(f"systemctl enable {unit.name}: {done.stderr.strip()}")
    elif backend == "cron":
        block = (root / "config" / "scheduler" / "crontab").read_text()
        done = _run(["crontab", "-"], stdin=_crontab_without(config["scheduler"]["label"]) + block)
        if done.returncode != 0:
            problems.append(f"crontab: {done.stderr.strip()}")
    return problems


def uninstall(root: Path, config: dict[str, Any]) -> list[str]:
    backend = config["scheduler"]["backend"]
    if backend == "launchd":
        for unit in _units(root):
            target = Path.home() / "Library" / "LaunchAgents" / unit.name
            _run(["launchctl", "bootout", f"gui/{os.getuid()}", str(target)])
            target.unlink(missing_ok=True)
    elif backend == "systemd":
        user_dir = Path.home() / ".config" / "systemd" / "user"
        for unit in _units(root):
            if unit.suffix == ".timer":
                _run(["systemctl", "--user", "disable", "--now", unit.name])
            (user_dir / unit.name).unlink(missing_ok=True)
        _run(["systemctl", "--user", "daemon-reload"])
    elif backend == "cron":
        _run(["crontab", "-"], stdin=_crontab_without(config["scheduler"]["label"]))
    return []


def main(argv: list[str]) -> int:
    root = common.repo_root()
    config = common.jarvis_config(root)
    if not config.get("bootstrapped"):
        print("not bootstrapped: run scripts/jarvis bootstrap first", file=sys.stderr)
        return 1
    match argv:
        case ["render"]:
            for name, text in render(root, config).items():
                (root / name).parent.mkdir(parents=True, exist_ok=True)
                (root / name).write_text(text)
                print(f"wrote {name}")
            return 0
        case ["install"]:
            problems = install(root, config)
        case ["uninstall"]:
            problems = uninstall(root, config)
        case ["status"]:
            print(f"backend: {config['scheduler']['backend']}")
            for unit in _units(root):
                print(unit.relative_to(root))
            return 0
        case _:
            print(__doc__, file=sys.stderr)
            return 2
    for problem in problems:
        print(problem, file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
