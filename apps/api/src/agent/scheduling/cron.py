"""Five-field cron: parse it, say when it next fires, and say it in English.

Written here rather than pulled in as a dependency because two of the three jobs
are ours anyway. `croniter` computes next-fire times but does not describe an
expression, and a description is half of what PRD §13.4 E asks for ("cron +
timezone, human-readable preview"). Carrying one implementation that does both,
with the timezone rules tested in the open, beats a dependency plus a second
hand-written describer that can disagree with it.

Three semantics worth stating because they are easy to get wrong:

1. **Day-of-month and day-of-week are OR'd when both are restricted.** `0 0 1 *
   1` fires on the 1st *and* on every Monday, not on Mondays that fall on the
   1st. That is Vixie cron's rule and every scheduler a user has met behaves
   this way.
2. **Search is over local wall-clock time**, then converted to UTC. A schedule
   set to 09:00 Europe/Copenhagen fires at 09:00 there in January and in July,
   which is the whole reason `Schedule.timezone` exists.
3. **A wall-clock time that does not exist is skipped, not shifted.** On the
   spring-forward morning 02:30 never happens; firing at 03:30 instead would
   silently move the schedule for that one day. A time that happens *twice*
   fires on its first occurrence (`fold=0`) — once, which is what the user
   asked for.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

#: How far ahead to look before declaring an expression unreachable. Four years
#: clears the longest real gap (`0 0 29 2 *` — 29 February, which needs a leap
#: year) with room to spare.
MAX_LOOKAHEAD_DAYS = 366 * 4

_MONTH_NAMES = {
    name: index
    for index, name in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"),
        start=1,
    )
}
_DAY_NAMES = {
    name: index for index, name in enumerate(("sun", "mon", "tue", "wed", "thu", "fri", "sat"))
}

_MACROS = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_FIELD_RE = re.compile(r"^(?:\*|\d+|[a-z]{3})(?:-(?:\d+|[a-z]{3}))?(?:/\d+)?$")

_WEEKDAY_LABELS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_MONTH_LABELS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


class CronError(ValueError):
    """The expression cannot be parsed, or names a time that can never arrive."""


@dataclass(frozen=True, slots=True)
class CronExpression:
    """A parsed five-field expression: minute hour day-of-month month day-of-week."""

    raw: str
    minutes: frozenset[int]
    hours: frozenset[int]
    days_of_month: frozenset[int]
    months: frozenset[int]
    days_of_week: frozenset[int]
    #: Whether each day field was narrowed from `*`. Both narrowed means OR.
    dom_restricted: bool
    dow_restricted: bool

    def matches_day(self, moment: datetime) -> bool:
        if moment.month not in self.months:
            return False
        dom_hit = moment.day in self.days_of_month
        # `datetime.weekday()` is Monday=0; cron is Sunday=0.
        dow_hit = ((moment.weekday() + 1) % 7) in self.days_of_week
        if self.dom_restricted and self.dow_restricted:
            return dom_hit or dow_hit
        return dom_hit and dow_hit


def parse(expression: str) -> CronExpression:
    """Parse a five-field expression or a `@macro`. Raises `CronError` on anything else."""
    text = expression.strip().lower()
    if not text:
        raise CronError("A schedule needs a cron expression.")
    text = _MACROS.get(text, text)

    fields = text.split()
    if len(fields) != 5:
        raise CronError(
            f"Expected 5 fields (minute hour day-of-month month day-of-week), got {len(fields)}: "
            f"{expression!r}"
        )

    minute, hour, dom, month, dow = fields
    parsed = CronExpression(
        raw=" ".join(fields),
        minutes=_field(minute, 0, 59, "minute"),
        hours=_field(hour, 0, 23, "hour"),
        days_of_month=_field(dom, 1, 31, "day-of-month"),
        months=_field(month, 1, 12, "month", names=_MONTH_NAMES),
        days_of_week=_normalise_sunday(_field(dow, 0, 7, "day-of-week", names=_DAY_NAMES)),
        dom_restricted=dom != "*",
        dow_restricted=dow != "*",
    )
    # A day that cannot exist is a typo, not a schedule. Catching it here means
    # the API rejects it with a 422 instead of storing a row that never fires.
    if not _any_day_reachable(parsed):
        raise CronError(f"{expression!r} names a date that never occurs.")
    return parsed


def next_fire(expression: CronExpression, *, after: datetime, timezone: str) -> datetime:
    """The first firing strictly after `after`, as an aware UTC datetime.

    `after` may be in any timezone; it is converted. The returned instant is
    always `> after`, so feeding a schedule its own `next_at` advances it.
    """
    zone = load_timezone(timezone)
    # Seconds and microseconds are not part of cron, and leaving them on would
    # make 03:00:30 count as "after 03:00" and skip the 03:00 firing.
    local = after.astimezone(zone).replace(second=0, microsecond=0) + timedelta(minutes=1)

    day = local.date()
    minute_of_day = local.hour * 60 + local.minute
    for offset in range(MAX_LOOKAHEAD_DAYS):
        probe = day + timedelta(days=offset)
        naive_midnight = datetime(probe.year, probe.month, probe.day)
        if not expression.matches_day(naive_midnight):
            continue
        floor = minute_of_day if offset == 0 else 0
        for hour in sorted(expression.hours):
            for minute in sorted(expression.minutes):
                if hour * 60 + minute < floor:
                    continue
                candidate = _resolve_wall_clock(
                    probe.year, probe.month, probe.day, hour, minute, zone
                )
                if candidate is None:
                    continue  # this wall time does not exist today (spring forward)
                if candidate > after:
                    return candidate
    years = MAX_LOOKAHEAD_DAYS // 366
    raise CronError(f"{expression.raw!r} has no firing within {years} years of {after:%Y-%m-%d}.")


def upcoming(
    expression: CronExpression, *, after: datetime, timezone: str, count: int
) -> list[datetime]:
    """The next `count` firings. Used for the preview, so it never raises past the first."""
    found: list[datetime] = []
    cursor = after
    for _ in range(max(count, 0)):
        try:
            cursor = next_fire(expression, after=cursor, timezone=timezone)
        except CronError:
            break
        found.append(cursor)
    return found


def load_timezone(name: str) -> ZoneInfo:
    """Resolve an IANA zone, reporting an unknown one as the user's mistake."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise CronError(
            f"{name!r} is not an IANA timezone (try 'Europe/Copenhagen' or 'UTC')."
        ) from exc


# ---------------------------------------------------------------------------
# description
# ---------------------------------------------------------------------------


def describe(expression: CronExpression, *, timezone: str = "UTC") -> str:
    """One sentence a non-engineer can check the schedule against.

    Deliberately conservative: where the shape is common it reads naturally, and
    where it is not it falls back to listing the values rather than guessing at
    prose that could misdescribe when the job runs.
    """
    time_phrase = _time_phrase(expression)
    day_phrase = _day_phrase(expression)
    month_phrase = _month_phrase(expression)

    parts = [time_phrase]
    # "Every 15 minutes every day" says the same thing twice. A frequency phrase
    # already implies every day; a clock time does not.
    if day_phrase == "every day" and time_phrase.startswith("every"):
        day_phrase = ""
    if day_phrase:
        parts.append(day_phrase)
    if month_phrase:
        parts.append(month_phrase)
    sentence = " ".join(parts)
    return f"{sentence[0].upper()}{sentence[1:]} ({timezone})"


def _time_phrase(expression: CronExpression) -> str:
    every_minute = len(expression.minutes) == 60
    every_hour = len(expression.hours) == 24

    if every_minute and every_hour:
        return "every minute"
    step = _step_of(expression.minutes, 0, 59)
    if every_hour and step is not None and step > 1:
        return f"every {step} minutes"
    if every_minute:
        return f"every minute of {_hours_clause(expression.hours)}"
    if len(expression.minutes) == 1 and every_hour:
        minute = next(iter(expression.minutes))
        return "every hour, on the hour" if minute == 0 else f"every hour at {minute:02d} past"
    times = [
        f"{hour:02d}:{minute:02d}"
        for hour in sorted(expression.hours)
        for minute in sorted(expression.minutes)
    ]
    if len(times) <= 4:
        return f"at {_join(times)}"
    return f"at {len(times)} times a day ({times[0]}, {times[1]}, …)"


def _hours_clause(hours: frozenset[int]) -> str:
    listed = [f"{hour:02d}:00" for hour in sorted(hours)]
    return _join(listed) if len(listed) <= 4 else f"{len(listed)} hours"


def _day_phrase(expression: CronExpression) -> str:
    dom, dow = expression.dom_restricted, expression.dow_restricted
    if not dom and not dow:
        return "every day"
    clauses = []
    if dow:
        names = [_WEEKDAY_LABELS[day] for day in sorted(expression.days_of_week)]
        if set(expression.days_of_week) == {1, 2, 3, 4, 5}:
            clauses.append("on weekdays")
        elif set(expression.days_of_week) == {0, 6}:
            clauses.append("at weekends")
        else:
            clauses.append(f"on {_join(names)}")
    if dom:
        clauses.append(
            f"on the {_join([_ordinal(day) for day in sorted(expression.days_of_month)])}"
        )
    # Both restricted is cron's OR, and saying "or" is the only honest rendering.
    return " or ".join(clauses)


def _month_phrase(expression: CronExpression) -> str:
    if len(expression.months) == 12:
        return ""
    names = [_MONTH_LABELS[month - 1] for month in sorted(expression.months)]
    return f"in {_join(names)}"


_ORDINAL_SUFFIXES = {1: "st", 2: "nd", 3: "rd"}


def _ordinal(value: int) -> str:
    if 11 <= value % 100 <= 13:
        return f"{value}th"
    return f"{value}{_ORDINAL_SUFFIXES.get(value % 10, 'th')}"


def _join(items: list[str]) -> str:
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return f"{', '.join(items[:-1])} and {items[-1]}"


def _step_of(values: frozenset[int], low: int, high: int) -> int | None:
    """The step of an evenly spaced set starting at `low`, or None if it is not one."""
    ordered = sorted(values)
    if len(ordered) < 2 or ordered[0] != low:
        return None
    step = ordered[1] - ordered[0]
    expected = list(range(low, high + 1, step))
    return step if ordered == expected else None


# ---------------------------------------------------------------------------
# parsing internals
# ---------------------------------------------------------------------------


def _field(
    text: str, low: int, high: int, label: str, *, names: dict[str, int] | None = None
) -> frozenset[int]:
    values: set[int] = set()
    for part in text.split(","):
        part = part.strip()
        if not part:
            raise CronError(f"Empty {label} field in {text!r}.")
        if not _FIELD_RE.match(part):
            raise CronError(f"{part!r} is not a valid {label} (in {text!r}).")
        values |= _range(part, low, high, label, names)
    if not values:
        raise CronError(f"{text!r} selects no {label}.")
    return frozenset(values)


def _range(part: str, low: int, high: int, label: str, names: dict[str, int] | None) -> set[int]:
    step = 1
    if "/" in part:
        part, _, raw_step = part.partition("/")
        step = int(raw_step)
        if step < 1:
            raise CronError(f"Step must be at least 1 in {label} {part!r}.")

    if part == "*":
        start, end = low, high
    elif "-" in part:
        raw_start, _, raw_end = part.partition("-")
        start, end = _value(raw_start, label, names), _value(raw_end, label, names)
        if start > end:
            raise CronError(f"{part!r} runs backwards in {label}.")
    else:
        start = _value(part, label, names)
        # `5/15` means "from 5 to the end of the field, every 15" — not "5 only".
        end = high if step > 1 else start

    if not (low <= start <= high and low <= end <= high):
        raise CronError(f"{part!r} is outside {low}-{high} for {label}.")
    return set(range(start, end + 1, step))


def _value(text: str, label: str, names: dict[str, int] | None) -> int:
    if names is not None and text in names:
        return names[text]
    try:
        return int(text)
    except ValueError as exc:
        raise CronError(f"{text!r} is not a valid {label}.") from exc


def _normalise_sunday(days: frozenset[int]) -> frozenset[int]:
    """Cron accepts both 0 and 7 for Sunday; the rest of this module uses 0."""
    return frozenset(0 if day == 7 else day for day in days)


def _any_day_reachable(expression: CronExpression) -> bool:
    """Whether some day in a four-year window matches, so `30 2 *` is caught at parse time."""
    day = datetime(2024, 1, 1)  # a leap year, so 29 February is in the window
    for offset in range(MAX_LOOKAHEAD_DAYS):
        if expression.matches_day(day + timedelta(days=offset)):
            return True
    return False


def _resolve_wall_clock(
    year: int, month: int, day: int, hour: int, minute: int, zone: ZoneInfo
) -> datetime | None:
    """A local wall time as a UTC instant, or None when that wall time is skipped by DST.

    The round trip is the test: if localising and coming back does not reproduce
    the hour and minute we asked for, the clock jumped over it this morning.
    """
    try:
        local = datetime(year, month, day, hour, minute, tzinfo=zone)
    except ValueError:
        return None  # e.g. 31 April — the day-matcher allows it, the calendar does not
    as_utc = local.astimezone(UTC)
    if as_utc.astimezone(zone).replace(second=0, microsecond=0) != local.replace(
        second=0, microsecond=0
    ):
        return None
    return as_utc
