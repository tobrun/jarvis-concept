"""Business-day arithmetic over the working weekdays in config/jarvis.yaml and config/holidays.md.

Usage:
  bizdays.py add <YYYY-MM-DD> <n>        the date n business days later
  bizdays.py between <from> <to>         business days after <from> up to and including <to>
  bizdays.py is <YYYY-MM-DD>             prints true or false
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Collection
from datetime import date, timedelta

import common


def is_business_day(day: date, holidays: Collection[date]) -> bool:
    """A working weekday (common.DaysOff carries them; Monday to Friday otherwise) that is no holiday."""
    return day.weekday() in getattr(holidays, "weekdays", common.MON_TO_FRI) and day not in holidays


def add_business_days(start: date, count: int, holidays: Collection[date]) -> date:
    """The date `count` business days after `start`; `start` itself never counts."""
    day = start
    remaining = count
    while remaining > 0:
        day += timedelta(days=1)
        if is_business_day(day, holidays):
            remaining -= 1
    return day


def business_days_between(start: date, end: date, holidays: Collection[date]) -> int:
    """Business days in the half-open range (start, end]; zero when end is not after start."""
    count = 0
    day = start
    while day < end:
        day += timedelta(days=1)
        if is_business_day(day, holidays):
            count += 1
    return count


def _add(args: list[str], holidays: Collection[date]) -> str:
    return add_business_days(date.fromisoformat(args[0]), int(args[1]), holidays).isoformat()


def _between(args: list[str], holidays: Collection[date]) -> str:
    return str(business_days_between(date.fromisoformat(args[0]), date.fromisoformat(args[1]), holidays))


def _is(args: list[str], holidays: Collection[date]) -> str:
    return "true" if is_business_day(date.fromisoformat(args[0]), holidays) else "false"


# command -> (argument count, handler printing its answer)
COMMANDS: dict[str, tuple[int, Callable[[list[str], Collection[date]], str]]] = {
    "add": (2, _add),
    "between": (2, _between),
    "is": (1, _is),
}


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    holidays = common.holidays()
    command, args = argv[0], argv[1:]
    if command not in COMMANDS or len(args) != COMMANDS[command][0]:
        print(__doc__, file=sys.stderr)
        return 2
    print(COMMANDS[command][1](args, holidays))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
