from datetime import date

import bizdays
import common
from bizdays import add_business_days, business_days_between, is_business_day

NO_HOLIDAYS: set[date] = set()


def test_two_business_days_after_friday_is_tuesday():
    assert add_business_days(date(2026, 10, 30), 2, NO_HOLIDAYS) == date(2026, 11, 3)


def test_listed_monday_holiday_is_skipped():
    assert add_business_days(date(2026, 10, 30), 1, {date(2026, 11, 2)}) == date(2026, 11, 3)


def test_business_days_between_monday_and_wednesday():
    assert business_days_between(date(2026, 10, 26), date(2026, 10, 28), NO_HOLIDAYS) == 2


def test_sunday_is_not_a_business_day():
    assert is_business_day(date(2026, 11, 1), NO_HOLIDAYS) is False
    assert is_business_day(date(2026, 10, 25), NO_HOLIDAYS) is False


def test_ascension_day_from_the_rendered_calendar_is_not_a_business_day(repo):
    # The example profile renders Dutch public holidays; Ascension Day 2026 is Thursday 14 May.
    assert is_business_day(date(2026, 5, 14), common.holidays(repo)) is False
    assert is_business_day(date(2026, 5, 15), common.holidays(repo)) is True


def test_working_days_from_the_config_replace_monday_to_friday(repo):
    config = repo / "config" / "jarvis.yaml"
    config.write_text(
        config.read_text().replace(
            "working_days: [mon, tue, wed, thu, fri]", "working_days: [sun, mon, tue, wed, thu]"
        )
    )
    days_off = common.holidays(repo)
    assert is_business_day(date(2026, 10, 25), days_off) is True
    assert is_business_day(date(2026, 10, 30), days_off) is False


def run_cli(capsys, *argv):
    code = bizdays.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out.strip(), captured.err


def test_cli_answers_add_between_and_is_from_the_rendered_calendar(repo, capsys):
    assert run_cli(capsys, "add", "2026-05-13", "1")[:2] == (0, "2026-05-15")
    assert run_cli(capsys, "between", "2026-05-13", "2026-05-15")[:2] == (0, "1")
    assert run_cli(capsys, "is", "2026-05-14")[:2] == (0, "false")
    assert run_cli(capsys, "is", "2026-05-15")[:2] == (0, "true")


def test_cli_with_an_unknown_command_or_wrong_argument_count_prints_usage_and_exits_2(repo, capsys):
    for argv in (["is"], ["when", "2026-11-12"], ["is", "2026-11-12", "2026-11-13"], ["add", "2026-11-12"]):
        code, out, err = run_cli(capsys, *argv)
        assert (code, out) == (2, "")
        assert "Usage:" in err
