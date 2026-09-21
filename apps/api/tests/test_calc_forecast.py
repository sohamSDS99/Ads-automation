"""`forecast.traffic_v1` (PRD §9.2, node 2.2.1).

Hand-checked against `calc_support.DEMAND` at the shipped
`forecast.impression_share_target_pct` of 45:

    US  100,000 x 1.0 x 45% = 45,000 impressions
        45,000 x 4%   = 1,800 clicks -> x $6 = $10,800 -> x 3% = 54 conv -> $200 CPA
    DE   40,000 x 45%  = 18,000 impressions
        18,000 x 3.5% =   630 clicks -> x $4 =  $2,520 -> x 2.5% = 15.75 conv -> $160 CPA
"""

from __future__ import annotations

import pandas as pd
import pytest

from agent.calc import forecast
from agent.calc.registry import CalcError
from tests.calc_support import CONSTANTS, DEMAND, frame


def calc() -> dict:
    return forecast.traffic_v1(frame(DEMAND), constants=CONSTANTS).result


def test_the_chain_from_search_volume_to_cpa() -> None:
    us = next(r for r in calc()["forecast"] if r["market"] == "US")
    assert us["impressions"] == 45_000
    assert us["clicks"] == 1_800
    assert us["cost_usd"] == 10_800
    assert us["conversions"] == 54
    assert us["cpa_usd"] == 200


def test_the_second_market_too() -> None:
    de = next(r for r in calc()["forecast"] if r["market"] == "DE")
    assert de["impressions"] == 18_000
    assert de["clicks"] == 630
    assert de["cost_usd"] == 2_520
    assert de["conversions"] == 15.75
    assert de["cpa_usd"] == 160


def test_totals_are_the_sum_of_the_visible_rows() -> None:
    """A media plan is read as a table; a table that does not foot is a defect."""
    result = calc()
    totals = result["totals"]
    for column in ("impressions", "clicks", "conversions", "cost_usd"):
        assert totals[column] == pytest.approx(sum(row[column] for row in result["forecast"])), (
            column
        )


def test_rates_at_the_total_level_are_re_derived_not_averaged() -> None:
    totals = calc()["totals"]
    assert totals["ctr_pct"] == 3.86  # 2,430 / 63,000, not (4 + 3.5) / 2
    assert totals["avg_cpc_usd"] == 5.48  # 13,320 / 2,430, not (6 + 4) / 2
    assert totals["cvr_pct"] == 2.87  # 69.75 / 2,430
    assert totals["cpa_usd"] == 190.97  # 13,320 / 69.75


def test_the_confidence_band_comes_from_the_supplied_cpc_range() -> None:
    band = calc()["confidence_band"]
    assert band["basis"] == "cpc_range"
    # low: 1,800 x $5 + 630 x $4 = $11,520 against $13,320
    assert band["low_pct"] == -13.51
    # high: 1,800 x $8 + 630 x $4 = $16,920
    assert band["high_pct"] == 27.03


def test_no_cpc_range_means_a_zero_band_that_says_so() -> None:
    rows = [
        {k: v for k, v in row.items() if k not in {"cpc_low_usd", "cpc_high_usd"}} for row in DEMAND
    ]
    band = forecast.traffic_v1(frame(rows), constants=CONSTANTS).result["confidence_band"]
    assert band == {"basis": "point_estimate", "low_pct": 0.0, "high_pct": 0.0}


def test_impression_share_headroom_is_weighted_by_impressions() -> None:
    # US 70% headroom over 45,000 impressions, DE 90% over 18,000
    # (70 x 45,000 + 90 x 18,000) / 63,000 = 75.71
    assert calc()["impression_share_headroom_pct"] == 75.71


def test_no_current_share_means_no_headroom_claim_rather_than_zero() -> None:
    rows = [{k: v for k, v in row.items() if k != "current_impression_share_pct"} for row in DEMAND]
    result = forecast.traffic_v1(frame(rows), constants=CONSTANTS).result
    assert result["impression_share_headroom_pct"] is None


def test_seasonality_scales_impressions() -> None:
    rows = [{**DEMAND[0], "seasonality_index": 1.5}]
    row = forecast.traffic_v1(frame(rows), constants=CONSTANTS).result["forecast"][0]
    assert row["impressions"] == 67_500  # 100,000 x 1.5 x 45%
    assert row["clicks"] == 2_700


def test_a_google_forecast_does_not_apply_the_share_target_twice() -> None:
    """Keyword Planner already answers "impressions we would win"."""
    rows = [{**DEMAND[0], "impressions": 45_000}]
    row = forecast.traffic_v1(frame(rows), constants=CONSTANTS, method="google_forecast").result[
        "forecast"
    ][0]
    assert row["impressions"] == 45_000
    assert row["clicks"] == 1_800


def test_a_google_forecast_without_an_impressions_column_says_which_column() -> None:
    with pytest.raises(CalcError, match="missing required column\\(s\\): impressions"):
        forecast.traffic_v1(frame(DEMAND), constants=CONSTANTS, method="google_forecast")


def test_the_method_is_recorded_on_the_result() -> None:
    assert calc()["method"] == "derived_arithmetic"


def test_an_unknown_method_is_refused() -> None:
    with pytest.raises(CalcError, match="method must be one of"):
        forecast.traffic_v1(frame(DEMAND), constants=CONSTANTS, method="vibes")  # type: ignore[arg-type]


def test_monthly_totals_keep_the_order_the_months_arrived_in() -> None:
    """`month` is the caller's label — sorting it would reorder their plan."""
    rows = [
        {**DEMAND[0], "month": "wave 2"},
        {**DEMAND[1], "month": "wave 1"},
    ]
    result = forecast.traffic_v1(frame(rows), constants=CONSTANTS).result
    assert [row["month"] for row in result["monthly_totals"]] == ["wave 2", "wave 1"]
    assert result["month_count"] == 2


def test_a_month_rollup_foots_to_its_own_rows() -> None:
    rows = [DEMAND[0], {**DEMAND[1], "month": "2027-02"}]
    result = forecast.traffic_v1(frame(rows), constants=CONSTANTS).result
    january = next(r for r in result["monthly_totals"] if r["month"] == "2027-01")
    assert january["cost_usd"] == 10_800
    assert result["totals"]["cost_usd"] == 13_320


def test_zero_conversions_reports_no_cpa_rather_than_dividing_by_zero() -> None:
    rows = [{**DEMAND[0], "cvr_pct": 0}]
    row = forecast.traffic_v1(frame(rows), constants=CONSTANTS).result["forecast"][0]
    assert row["conversions"] == 0
    assert row["cpa_usd"] is None


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("ctr_pct", -1, "negative rate or volume"),
        ("search_volume", -5, "negative rate or volume"),
        ("ctr_pct", 101, "above 100"),
        ("cvr_pct", 150, "above 100"),
        ("impression_share_target_pct", 0, "outside (0, 100]"),
        ("impression_share_target_pct", 140, "outside (0, 100]"),
    ],
)
def test_an_impossible_row_is_excluded_with_its_reason(
    field: str, value: float, fragment: str
) -> None:
    rows = [{**DEMAND[0], field: value}, DEMAND[1]]
    result = forecast.traffic_v1(frame(rows), constants=CONSTANTS)
    assert [row["market"] for row in result.result["forecast"]] == ["DE"]
    assert fragment in result.excluded[0]["reason"]
    assert result.excluded[0]["row"] == "core/US/2027-01"


def test_every_row_being_impossible_raises() -> None:
    rows = [{**row, "ctr_pct": -1} for row in DEMAND]
    with pytest.raises(CalcError, match="no demand row produced a usable forecast"):
        forecast.traffic_v1(frame(rows), constants=CONSTANTS)


def test_a_missing_cpc_range_on_one_row_only_does_not_break_the_band() -> None:
    """The DE row has no range; the US row does. Real data looks like this."""
    band = calc()["confidence_band"]
    assert band["basis"] == "cpc_range"
    assert band["low_pct"] < 0 < band["high_pct"]


def test_an_absent_optional_column_reads_as_absent_not_as_nan() -> None:
    """pandas fills the DE row's missing cpc_low_usd with NaN, which is not JSON."""
    calc_result = forecast.traffic_v1(frame(DEMAND), constants=CONSTANTS)
    de_input = next(row for row in calc_result.inputs["demand"] if row["market"] == "DE")
    assert de_input["cpc_low_usd"] is None


def test_a_broken_share_target_is_refused_before_any_arithmetic() -> None:
    with pytest.raises(CalcError, match="impression_share_target_pct must be in"):
        forecast.traffic_v1(
            frame(DEMAND),
            constants=CONSTANTS.merged({"forecast.impression_share_target_pct": 0}),
        )


def test_the_summary_names_the_method() -> None:
    calc_result = forecast.traffic_v1(frame(DEMAND), constants=CONSTANTS)
    assert calc_result.summary.startswith("derived_arithmetic:")
    assert "$13,320.00" in calc_result.summary


def test_a_non_frame_is_refused() -> None:
    with pytest.raises(CalcError, match="must be a DataFrame"):
        forecast.traffic_v1(DEMAND, constants=CONSTANTS)  # type: ignore[arg-type]


def test_an_empty_frame_raises() -> None:
    empty = pd.DataFrame(columns=[*DEMAND[0]])
    with pytest.raises(CalcError, match="is empty"):
        forecast.traffic_v1(empty, constants=CONSTANTS)
