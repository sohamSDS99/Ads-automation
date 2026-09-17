"""The cron engine. Parsing, firing, timezones, and the sentence a person reads.

The timezone cases are the reason this file is long. Everything else here is
arithmetic that fails loudly; a schedule that quietly fires an hour late for
half the year is the failure nobody reports and everybody plans around.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from agent.scheduling import cron

COPENHAGEN = "Europe/Copenhagen"


def at(text: str) -> datetime:
    return datetime.fromisoformat(text)


def next_utc(expression: str, after: str, timezone: str = "UTC") -> datetime:
    return cron.next_fire(cron.parse(expression), after=at(after), timezone=timezone)


# ---------------------------------------------------------------------------
# parsing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "expected"),
    [
        ("@hourly", "0 * * * *"),
        ("@daily", "0 0 * * *"),
        ("@midnight", "0 0 * * *"),
        ("@weekly", "0 0 * * 0"),
        ("@monthly", "0 0 1 * *"),
        ("@yearly", "0 0 1 1 *"),
    ],
)
def test_macros_expand(expression: str, expected: str) -> None:
    assert cron.parse(expression).raw == expected


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "0 3 * *",
        "0 3 * * * *",
        "60 3 * * *",
        "-1 3 * * *",
        "0 24 * * *",
        "0 3 * * 9",
        "0 3 0 * *",
        "0 3 32 * *",
        "0 3 * 13 *",
        "0 3 31 2 *",
        "a b c d e",
        "0 3 5-1 * *",
        "*/0 * * * *",
    ],
)
def test_bad_expressions_are_refused(expression: str) -> None:
    with pytest.raises(cron.CronError):
        cron.parse(expression)


def test_names_are_accepted_for_months_and_days() -> None:
    parsed = cron.parse("0 9 * jan-mar mon")
    assert parsed.months == frozenset({1, 2, 3})
    assert parsed.days_of_week == frozenset({1})


def test_seven_and_zero_both_mean_sunday() -> None:
    assert cron.parse("0 0 * * 7").days_of_week == cron.parse("0 0 * * 0").days_of_week


def test_a_step_from_a_value_runs_to_the_end_of_the_field() -> None:
    """`5/15` is "from 5, every 15", not "5 only" — the classic cron surprise."""
    assert cron.parse("5/15 * * * *").minutes == frozenset({5, 20, 35, 50})


def test_a_step_on_a_range_stays_inside_it() -> None:
    assert cron.parse("0-30/10 * * * *").minutes == frozenset({0, 10, 20, 30})


# ---------------------------------------------------------------------------
# firing
# ---------------------------------------------------------------------------


def test_next_fire_is_strictly_after_the_given_instant() -> None:
    """Feeding a schedule its own `next_at` has to advance it, or the poller loops."""
    expression = cron.parse("0 3 * * *")
    first = cron.next_fire(expression, after=at("2026-03-01T00:00:00+00:00"), timezone="UTC")
    second = cron.next_fire(expression, after=first, timezone="UTC")
    assert first == at("2026-03-01T03:00:00+00:00")
    assert second == at("2026-03-02T03:00:00+00:00")


def test_exactly_on_a_firing_returns_the_next_one_not_this_one() -> None:
    assert next_utc("0 3 * * *", "2026-03-01T03:00:00+00:00") == at("2026-03-02T03:00:00+00:00")


def test_seconds_do_not_swallow_a_firing() -> None:
    """02:59:30 must still catch 03:00 — truncation, not rounding."""
    assert next_utc("0 3 * * *", "2026-03-01T02:59:30+00:00") == at("2026-03-01T03:00:00+00:00")


def test_day_of_month_and_day_of_week_are_ored_when_both_are_set() -> None:
    """Vixie's rule. `0 0 1 * 1` fires on the 1st AND on every Monday."""
    expression = cron.parse("0 0 1 * 1")
    # 1 March 2026 is a Sunday, so the 1st is not a Monday — both branches fire.
    assert cron.next_fire(expression, after=at("2026-02-25T00:00:00+00:00"), timezone="UTC") == at(
        "2026-03-01T00:00:00+00:00"
    )
    assert cron.next_fire(expression, after=at("2026-03-01T00:00:00+00:00"), timezone="UTC") == at(
        "2026-03-02T00:00:00+00:00"
    )


def test_a_narrowed_day_of_month_alone_is_anded_with_the_weekday_wildcard() -> None:
    assert next_utc("0 0 15 * *", "2026-03-01T00:00:00+00:00") == at("2026-03-15T00:00:00+00:00")


def test_february_29th_finds_the_next_leap_year() -> None:
    assert next_utc("0 0 29 2 *", "2026-01-01T00:00:00+00:00") == at("2028-02-29T00:00:00+00:00")


# ---------------------------------------------------------------------------
# timezones — the part that is easy to get quietly wrong
# ---------------------------------------------------------------------------


def test_a_local_schedule_keeps_its_wall_clock_across_a_dst_change() -> None:
    """09:00 in Copenhagen is 09:00 there in January and in July.

    The UTC instant moves; the local time does not. That is the entire reason
    `Schedule.timezone` exists.
    """
    expression = cron.parse("0 9 * * *")
    winter = cron.next_fire(expression, after=at("2026-01-10T00:00:00+00:00"), timezone=COPENHAGEN)
    summer = cron.next_fire(expression, after=at("2026-07-10T00:00:00+00:00"), timezone=COPENHAGEN)
    zone = ZoneInfo(COPENHAGEN)
    assert winter.astimezone(zone).hour == 9
    assert summer.astimezone(zone).hour == 9
    assert winter.hour == 8  # UTC+1
    assert summer.hour == 7  # UTC+2


def test_a_wall_clock_time_that_does_not_exist_is_skipped_not_shifted() -> None:
    """Spring forward: 02:30 never happens on 29 March 2026 in Copenhagen.

    Firing at 03:30 instead would silently move the schedule for that one day,
    which is worse than missing it — nobody would ever notice the drift.
    """
    fired = next_utc("30 2 * * *", "2026-03-28T12:00:00+00:00", COPENHAGEN)
    local = fired.astimezone(ZoneInfo(COPENHAGEN))
    assert local.date().isoformat() == "2026-03-30"
    assert (local.hour, local.minute) == (2, 30)


def test_an_ambiguous_wall_clock_time_fires_once() -> None:
    """Fall back: 02:30 happens twice on 25 October 2026. Once is what was asked for."""
    first = next_utc("30 2 * * *", "2026-10-24T12:00:00+00:00", COPENHAGEN)
    second = cron.next_fire(cron.parse("30 2 * * *"), after=first, timezone=COPENHAGEN)
    assert first.astimezone(ZoneInfo(COPENHAGEN)).date().isoformat() == "2026-10-25"
    assert second.astimezone(ZoneInfo(COPENHAGEN)).date().isoformat() == "2026-10-26"


def test_an_unknown_timezone_is_reported_as_the_user_s_mistake() -> None:
    with pytest.raises(cron.CronError, match="IANA"):
        cron.load_timezone("Europe/Nowhere")


def test_upcoming_returns_consecutive_firings() -> None:
    found = cron.upcoming(
        cron.parse("0 7 * * 1-5"), after=at("2026-03-01T00:00:00+00:00"), timezone="UTC", count=3
    )
    assert [instant.isoformat() for instant in found] == [
        "2026-03-02T07:00:00+00:00",
        "2026-03-03T07:00:00+00:00",
        "2026-03-04T07:00:00+00:00",
    ]


# ---------------------------------------------------------------------------
# description — cosmetic, but it is what an admin checks the schedule against
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("expression", "contains"),
    [
        ("0 3 * * *", "03:00"),
        ("*/15 * * * *", "every 15 minutes"),
        ("0 * * * *", "every hour"),
        ("* * * * *", "every minute"),
        ("30 9 * * 1", "Monday"),
        ("0 7 * * 1-5", "weekdays"),
        ("0 9 * * 0,6", "weekends"),
        ("0 6 1 * *", "1st"),
        ("0 6 2 * *", "2nd"),
        ("0 6 3 * *", "3rd"),
        ("0 6 11 * *", "11th"),
        ("0 0 1 1 *", "January"),
    ],
)
def test_descriptions_say_what_the_expression_means(expression: str, contains: str) -> None:
    # Case-insensitive: the sentence is capitalised, and which word lands first
    # is a rendering detail rather than something to pin per expression.
    assert contains.lower() in cron.describe(cron.parse(expression)).lower()


def test_the_description_names_the_timezone() -> None:
    assert COPENHAGEN in cron.describe(cron.parse("0 3 * * *"), timezone=COPENHAGEN)


def test_a_frequency_description_does_not_also_say_every_day() -> None:
    """ "Every 15 minutes every day" says the same thing twice."""
    assert cron.describe(cron.parse("*/15 * * * *")).lower().count("every") == 1


def test_an_ored_day_rule_says_or() -> None:
    """The OR is surprising, so the sentence has to carry it rather than pick one."""
    assert " or " in cron.describe(cron.parse("0 0 1 * 1"))


def test_every_description_starts_with_a_capital() -> None:
    for expression in ("0 3 * * *", "*/15 * * * *", "* * * * *", "0 0 1 * 1"):
        assert cron.describe(cron.parse(expression))[0].isupper()


def test_now_is_not_baked_into_a_parsed_expression() -> None:
    """Parsing is pure: two parses of one string are interchangeable."""
    assert cron.parse("0 3 * * *") == cron.parse("0 3 * * *")


def test_next_fire_accepts_an_aware_instant_in_any_zone() -> None:
    from_utc = cron.next_fire(
        cron.parse("0 3 * * *"), after=datetime(2026, 3, 1, 12, tzinfo=UTC), timezone="UTC"
    )
    from_local = cron.next_fire(
        cron.parse("0 3 * * *"),
        after=datetime(2026, 3, 1, 13, tzinfo=ZoneInfo(COPENHAGEN)),
        timezone="UTC",
    )
    assert from_utc == from_local
