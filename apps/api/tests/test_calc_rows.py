"""`calc/rows.py` — frame cells to plain Python, and the refusals.

Small module, and the one every formula reads its inputs through, so the
conversions it performs are worth pinning down directly rather than inferring
from nine formulas' behaviour. Two of the three exist because they bit
something: NaN reaching `registry.canonical`, and `bool("false")`.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from agent.calc import rows
from agent.calc.registry import CalcError


def test_records_returns_plain_dicts_with_string_keys() -> None:
    frame = pd.DataFrame([{"a": 1, "b": "x"}, {"a": 2, "b": "y"}])
    assert rows.records(frame, ("a", "b"), what="thing") == [
        {"a": 1, "b": "x"},
        {"a": 2, "b": "y"},
    ]


def test_a_missing_column_is_named_and_sorted_so_the_message_is_stable() -> None:
    frame = pd.DataFrame([{"a": 1}])
    with pytest.raises(CalcError, match=r"thing is missing required column\(s\): b, c"):
        rows.records(frame, ("a", "c", "b"), what="thing")


def test_an_empty_frame_is_refused_rather_than_producing_a_zero() -> None:
    with pytest.raises(CalcError, match="thing is empty"):
        rows.records(pd.DataFrame(columns=["a"]), ("a",), what="thing")


def test_a_non_frame_is_refused_with_the_type_it_got() -> None:
    with pytest.raises(CalcError, match="thing must be a DataFrame, got list"):
        rows.records([{"a": 1}], ("a",), what="thing")  # type: ignore[arg-type]


# --- cell -------------------------------------------------------------------


def test_a_missing_cell_becomes_none_not_nan() -> None:
    """NaN is not JSON, and `registry.canonical` refuses non-finite numbers."""
    frame = pd.DataFrame([{"a": 1, "b": 2}, {"a": 3}])
    assert rows.records(frame, ("a",), what="f")[1]["b"] is None


def test_pandas_na_also_becomes_none() -> None:
    assert rows.cell(pd.NA) is None
    assert rows.cell(float("nan")) is None
    assert rows.cell(None) is None


def test_numpy_scalars_become_python_scalars() -> None:
    """`numpy.int64(5)` would otherwise be stringified into the inputs hash.

    The scalars are taken out of a Series rather than built with `numpy`
    directly: numpy is pandas' dependency, not this project's, and a test that
    imports it is a test that pins a version nobody declared.
    """
    integer = pd.Series([5], dtype="int64").iloc[0]
    floating = pd.Series([1.5], dtype="float64").iloc[0]
    boolean = pd.Series([True], dtype="bool").iloc[0]
    assert type(integer).__module__.startswith("numpy")

    assert rows.cell(integer) == 5
    assert isinstance(rows.cell(integer), int)
    assert rows.cell(floating) == 1.5
    assert isinstance(rows.cell(floating), float)
    assert rows.cell(boolean) is True


def test_a_timestamp_becomes_an_iso_string() -> None:
    assert rows.cell(pd.Timestamp("2027-01-31")) == "2027-01-31T00:00:00"


def test_containers_and_strings_pass_straight_through() -> None:
    for value in ("x", b"x", {"a": 1}, [1, 2], (1, 2), {1, 2}):
        assert rows.cell(value) is value


def test_a_plain_python_value_is_left_alone() -> None:
    assert rows.cell(5) == 5
    assert rows.cell(date(2027, 1, 1)) == date(2027, 1, 1)


# --- number -----------------------------------------------------------------


def test_number_coerces_what_a_spreadsheet_gives_it() -> None:
    assert rows.number({"a": "5"}, "a") == 5.0
    assert rows.number({"a": 5}, "a") == 5.0
    assert rows.number({"a": True}, "a") == 1.0


def test_number_falls_back_to_the_default_for_an_absent_or_blank_cell() -> None:
    for row in ({}, {"a": None}, {"a": float("nan")}, {"a": ""}):
        assert rows.number(row, "a", default=7.0) == 7.0


def test_number_with_no_default_names_the_column_it_needed() -> None:
    with pytest.raises(CalcError, match="a is required and was missing"):
        rows.number({}, "a")


def test_number_refuses_a_value_that_is_not_a_number() -> None:
    with pytest.raises(CalcError, match="a='lots' is not a number"):
        rows.number({"a": "lots"}, "a")
    with pytest.raises(CalcError, match="is not a number"):
        rows.number({"a": [1]}, "a")


def test_number_refuses_an_infinity_that_arrived_as_a_string() -> None:
    with pytest.raises(CalcError, match="is not finite"):
        rows.number({"a": "inf"}, "a")


# --- text and flag ----------------------------------------------------------


def test_text_strips_and_falls_back() -> None:
    assert rows.text({"a": "  x  "}, "a") == "x"
    assert rows.text({"a": "   "}, "a", default="-") == "-"
    assert rows.text({"a": None}, "a", default="-") == "-"
    assert rows.text({"a": float("nan")}, "a", default="-") == "-"
    assert rows.text({}, "a", default="-") == "-"
    assert rows.text({"a": 5}, "a") == "5"


@pytest.mark.parametrize("value", ["false", "FALSE", " no ", "0", "n", "none", "nan", ""])
def test_every_spreadsheet_spelling_of_no_reads_as_no(value: str) -> None:
    assert rows.flag({"a": value}, "a") is False


@pytest.mark.parametrize("value", ["true", "TRUE", "yes", "1", "y", "anything else"])
def test_anything_else_reads_as_yes(value: str) -> None:
    assert rows.flag({"a": value}, "a") is True


def test_flag_handles_the_types_that_are_already_booleans_or_numbers() -> None:
    assert rows.flag({"a": True}, "a") is True
    assert rows.flag({"a": False}, "a") is False
    assert rows.flag({"a": 1}, "a") is True
    assert rows.flag({"a": 0}, "a") is False
    assert rows.flag({"a": 0.0}, "a") is False
    assert rows.flag({"a": 2.5}, "a") is True
    assert rows.flag({"a": [1]}, "a") is True
    assert rows.flag({"a": []}, "a") is False


def test_flag_defaults_when_the_column_is_absent() -> None:
    assert rows.flag({}, "a") is False
    assert rows.flag({"a": None}, "a", default=True) is True


# --- total ------------------------------------------------------------------


def test_total_is_exactly_rounded_where_plain_sum_is_only_nearly_so() -> None:
    """Worth stating precisely, because the obvious justification is now wrong.

    CPython 3.12 gave `sum()` Neumaier compensation, so on this interpreter the
    two agree on every ordinary money column — `sum([0.1, 0.2, 0.3])` is 0.6
    here, not 0.6000000000000001. `fsum` is still the one that is *guaranteed*
    exactly rounded, which is what the exact-envelope invariant in
    `allocation.split_v1` leans on, and this is a case where the difference is
    still visible.
    """
    ordinary = [0.1, 0.2, 0.3]
    assert rows.total(ordinary) == sum(ordinary) == 0.6

    catastrophic = [1, 1e100, 1, -1e100] * 10
    assert rows.total(catastrophic) == 20.0
    assert sum(catastrophic) == 9.0
