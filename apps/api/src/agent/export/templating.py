"""The Jinja environment the markdown and print-HTML templates share.

One place, because the two templates must agree on how a number is formatted.
They are the same document in two skins: if `4.87` renders as `4.87` in the
markdown and `4.9` in the PDF, PRD §12's acceptance — "both contain every
section present in the JSON" — is technically met and practically violated.

Every filter here is total. A template that hits `None` prints the em dash
placeholder rather than raising, because a renderer that throws on a missing
optional field turns one absent number into a failed export.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

TEMPLATE_DIR = Path(__file__).parent / "templates"

#: What an absent optional value prints as, in every format.
EMPTY = "—"

MONTH_NAMES = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def fmt_number(value: Any, places: int = 0) -> str:
    """`1234.5` → `1,235`. Thousands-separated, fixed places, never scientific."""
    if value is None or value == "":
        return EMPTY
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number:,.{places}f}"


def fmt_money(value: Any, currency: str = "", places: int = 2) -> str:
    """A money amount with an optional currency code *after* it.

    The code trails the number because the report spans markets — `14,200 GBP`
    is unambiguous where `£14,200` in a German section is not.
    """
    if value is None or value == "":
        return EMPTY
    rendered = fmt_number(value, places)
    return f"{rendered} {currency}".strip()


def fmt_pct(value: Any, places: int = 0) -> str:
    """`82.0` → `82%`. The value is already a percentage, not a fraction."""
    if value is None or value == "":
        return EMPTY
    return f"{fmt_number(value, places)}%"


def fmt_ratio_pct(value: Any, places: int = 0) -> str:
    """`0.14` → `14%`. For fields that carry a fraction, like `trend_yoy`."""
    if value is None or value == "":
        return EMPTY
    try:
        return f"{float(value) * 100:,.{places}f}%"
    except (TypeError, ValueError):
        return str(value)


def fmt_date(value: Any) -> str:
    """ISO date, UTC. Times are dropped: no reader of this document needs seconds."""
    if value is None or value == "":
        return EMPTY
    if isinstance(value, datetime):
        return value.date().isoformat()
    return str(value)


def fmt_datetime(value: Any) -> str:
    """ISO minute precision — used on the cover page and nowhere else."""
    if value is None or value == "":
        return EMPTY
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M UTC")
    return str(value)


def fmt_months(values: Any) -> str:
    """`[1, 2, 9]` → `Jan, Feb, Sep`. Out-of-range numbers pass through unharmed."""
    if not values:
        return EMPTY
    names = []
    for value in values:
        try:
            index = int(value)
        except (TypeError, ValueError):
            names.append(str(value))
            continue
        names.append(MONTH_NAMES[index - 1] if 1 <= index <= 12 else str(value))
    return ", ".join(names)


def fmt_list(values: Any, separator: str = ", ") -> str:
    if not values:
        return EMPTY
    return separator.join(str(value) for value in values)


def fmt_text(value: Any) -> str:
    """A scalar that may be absent."""
    if value is None or value == "":
        return EMPTY
    return str(value)


def fmt_days(value: Any) -> str:
    """`41` → `41 days`, `None` → `—`.

    The unit has to travel with the number: a cell rendering `— days` for an
    action that has never converted reads as a measurement, and it is the
    absence of one.
    """
    if value is None or value == "":
        return EMPTY
    return f"{fmt_number(value)} day" + ("" if value == 1 else "s")


def fmt_bool(value: Any, yes: str = "Yes", no: str = "No") -> str:
    if value is None:
        return EMPTY
    return yes if value else no


def md_cell(value: Any) -> str:
    """Make a value safe inside a markdown table cell.

    A pipe in a keyword — and keywords come from search queries, so one will
    turn up eventually — silently adds a column and corrupts every row after it.
    Newlines do the same to the row itself.
    """
    if value is None or value == "":
        return EMPTY
    return str(value).replace("|", "\\|").replace("\n", " ").replace("\r", "")


def _install(env: Environment) -> Environment:
    env.filters.update(
        {
            "num": fmt_number,
            "money": fmt_money,
            "pct": fmt_pct,
            "ratio_pct": fmt_ratio_pct,
            "date": fmt_date,
            "datetime": fmt_datetime,
            "months": fmt_months,
            "commalist": fmt_list,
            "text": fmt_text,
            "yesno": fmt_bool,
            "days": fmt_days,
            "cell": md_cell,
        }
    )
    env.globals["EMPTY"] = EMPTY
    return env


def markdown_env() -> Environment:
    """No autoescaping: the output *is* markup, and `&amp;` in a heading is a bug.

    `keep_trailing_newline` and the whitespace controls matter more than they
    look — the golden test compares bytes, and a template that emits a different
    number of blank lines after an edit is a diff nobody can review.
    """
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        # Markdown output is markup. Escaping it would print `&amp;` in a
        # heading; the one injection surface — a pipe inside a table cell —
        # is handled by the `cell` filter, which is what actually protects
        # the document here.
        autoescape=False,  # noqa: S701
    )
    return _install(env)


def html_env() -> Environment:
    """Autoescaped. The same values, rendered into a document WeasyPrint prints."""
    env = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
        keep_trailing_newline=True,
        autoescape=select_autoescape(default_for_string=True, default=True),
    )
    return _install(env)
