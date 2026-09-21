"""Frame-to-records helpers shared by the formulas.

Every formula takes a `pandas.DataFrame` — that is the `calc/` contract — but a
formula body is much easier to read, and much easier to hand-check a test
against, over plain dicts. These four helpers are the whole conversion: they
check the columns a formula needs are present, coerce the numbers, and refuse a
value that arithmetic would silently corrupt.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from typing import Any

import pandas as pd

from agent.calc.registry import CalcError

Row = dict[str, Any]


def records(frame: pd.DataFrame, required: Sequence[str], *, what: str) -> list[Row]:
    """Rows as dicts, after asserting `required` columns exist and rows are present."""
    if not isinstance(frame, pd.DataFrame):  # pragma: no cover - defensive
        raise CalcError(f"{what} must be a DataFrame, got {type(frame).__name__}")
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise CalcError(f"{what} is missing required column(s): {', '.join(sorted(missing))}")
    if frame.empty:
        raise CalcError(f"{what} is empty — there is nothing to calculate")
    return [
        {str(key): cell(value) for key, value in record.items()}
        for record in frame.to_dict(orient="records")
    ]


def cell(value: Any) -> Any:
    """One DataFrame cell as a plain Python value.

    Three conversions, each of which bit something before it was here:

    * **NaN and NA become None.** A row that simply has no `cpc_low_usd` reads
      as NaN once pandas has aligned the frame, and NaN is not JSON — it would
      reach `registry.canonical`, which refuses non-finite numbers because a
      *computed* NaN must never be persisted as if it were an answer. An absent
      input is a different thing from a broken output, and this is where the two
      are told apart.
    * **numpy scalars become Python scalars.** `numpy.int64(5)` would otherwise
      be stringified into the inputs hash as `"5"`, so the same input read from
      a frame and from a literal would hash differently.
    * **Timestamps become ISO strings**, which is what a `month` label is
      anyway, and is stable across pandas versions.
    """
    if isinstance(value, str | bytes | dict | list | tuple | set):
        return value
    if value is None:
        return None
    try:
        if bool(pd.isna(value)):
            return None
    except (TypeError, ValueError):  # pragma: no cover - non-scalar, handled above
        return value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    item = getattr(value, "item", None)
    if callable(item) and type(value).__module__.startswith("numpy"):
        converted: Any = item()
        return converted
    return value


def number(row: Row, key: str, *, default: float | None = None) -> float:
    """A finite float from `row[key]`, or `default` when absent/blank/NaN."""
    value = row.get(key)
    if value is None or (isinstance(value, float) and math.isnan(value)) or value == "":
        if default is None:
            raise CalcError(f"{key} is required and was missing")
        return float(default)
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise CalcError(f"{key}={value!r} is not a number") from exc
    if math.isnan(parsed) or math.isinf(parsed):
        raise CalcError(f"{key}={value!r} is not finite")
    return parsed


def text(row: Row, key: str, *, default: str = "") -> str:
    """A stripped string from `row[key]`. NaN and None both read as absent."""
    value = row.get(key)
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return default
    return str(value).strip() or default


#: Strings a spreadsheet or a CSV upload uses for "no". `bool("False")` is
#: True, which is how a column of `false` strings silently becomes a column of
#: yeses, so a flag never goes through `bool()` directly.
FALSEY = frozenset({"", "0", "0.0", "false", "f", "no", "n", "none", "null", "nan"})


def flag(row: Row, key: str, *, default: bool = False) -> bool:
    """A boolean from `row[key]`, reading spreadsheet spellings of no as no."""
    value = row.get(key)
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in FALSEY
    if isinstance(value, int | float):
        return value != 0
    return bool(value)


def total(values: Iterable[float]) -> float:
    """Exactly-rounded summation.

    `math.fsum` rather than `sum` for a narrower reason than it first looks:
    CPython 3.12 gave `sum()` Neumaier compensation, so on this interpreter the
    two agree on any ordinary column of money. `fsum` is the one whose result is
    *guaranteed* exactly rounded, and an allocation that has to add up to its own
    envelope to the cent should not depend on an interpreter optimisation that
    arrived one version ago and applies only to floats.
    """
    return math.fsum(values)
