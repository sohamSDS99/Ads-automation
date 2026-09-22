"""`allocation.split_v1` and `allocation.whatif_v1` (PRD §9.2, gate G3).

The baseline split, worked out by hand at an envelope of $20,000:

    unit             target/forecast CPA  efficiency  weight  raw    share
    brand/US         200 / 80             2.5000      1.0     2.5000  45.60%
    nonbrand/US      200 / 260            0.7692      2.0     1.5385  28.06%
    nonbrand/DE      200 / 180            1.1111      1.0     1.1111  20.27%
    remarketing/US   200 / 120            1.6667      0.2     0.3333   6.08%
                                                      sum     5.4829

The property that matters most is the last line of `test_the_split_sums_to_the
_envelope_exactly`: §12 invariant 4 allows +/-0.5%, and this is exact.
"""

from __future__ import annotations

import time

import pytest

from agent.calc import allocation
from agent.calc.registry import CalcError
from tests.calc_support import CONSTANTS, frame

UNITS = [
    {
        "campaign_ref": "brand",
        "market": "US",
        "funnel_stage": "bottom",
        "forecast_cpa_usd": 80,
        "target_cpa_usd": 200,
        "strategic_weight": 1.0,
        "avg_cpc_usd": 3.0,
    },
    {
        "campaign_ref": "nonbrand",
        "market": "US",
        "funnel_stage": "mid",
        "forecast_cpa_usd": 260,
        "target_cpa_usd": 200,
        "strategic_weight": 2.0,
        "avg_cpc_usd": 6.0,
    },
    {
        "campaign_ref": "nonbrand",
        "market": "DE",
        "funnel_stage": "mid",
        "forecast_cpa_usd": 180,
        "target_cpa_usd": 200,
        "strategic_weight": 1.0,
        "avg_cpc_usd": 4.0,
    },
    {
        "campaign_ref": "remarketing",
        "market": "US",
        "funnel_stage": "bottom",
        "forecast_cpa_usd": 120,
        "target_cpa_usd": 200,
        "strategic_weight": 0.2,
        "avg_cpc_usd": 2.0,
    },
]


def split(units: list[dict] | None = None, envelope: float = 20_000) -> dict:
    return allocation.split_v1(
        frame(units if units is not None else UNITS), constants=CONSTANTS, envelope_usd=envelope
    ).result


def unit(result: dict, ref: str, market: str) -> dict:
    return next(
        row
        for row in result["allocation"]
        if row["campaign_ref"] == ref and row["market"] == market
    )


def test_efficiency_is_target_over_forecast_cpa() -> None:
    result = split()
    assert unit(result, "brand", "US")["efficiency"] == 2.5
    assert unit(result, "nonbrand", "US")["efficiency"] == 0.7692
    assert unit(result, "nonbrand", "DE")["efficiency"] == 1.1111
    assert unit(result, "remarketing", "US")["efficiency"] == 1.6667


def test_the_hand_checked_split() -> None:
    result = split()
    assert unit(result, "brand", "US")["usd"] == 9_119.25
    assert unit(result, "nonbrand", "US")["usd"] == 5_611.85
    assert unit(result, "nonbrand", "DE")["usd"] == 4_053.00
    assert unit(result, "remarketing", "US")["usd"] == 1_215.90
    assert unit(result, "brand", "US")["pct"] == 45.60


def test_the_split_sums_to_the_envelope_exactly() -> None:
    result = split()
    assert sum(row["usd"] for row in result["allocation"]) == 20_000
    assert result["allocated_usd"] == 20_000
    assert result["unallocated_usd"] == 0


def test_the_residual_cent_lands_on_one_unit_rather_than_vanishing() -> None:
    """An envelope that cannot divide cleanly still adds up."""
    for envelope in (10_000.01, 33_333.33, 4_567.89, 999_999.99):
        result = split(envelope=envelope)
        assert sum(row["usd"] for row in result["allocation"]) == pytest.approx(
            envelope, abs=0.005
        ), envelope


def test_a_better_forecast_cpa_earns_more_budget() -> None:
    result = split()
    assert unit(result, "brand", "US")["usd"] > unit(result, "nonbrand", "DE")["usd"]


def test_efficiency_is_bounded_so_one_suspicious_forecast_cannot_take_everything() -> None:
    units = [{**UNITS[0], "forecast_cpa_usd": 0.5}, UNITS[1]]
    result = split(units, envelope=20_000)
    assert unit(result, "brand", "US")["efficiency"] == allocation.MAX_EFFICIENCY


def test_conversions_and_clicks_are_forecast_from_the_awarded_budget() -> None:
    brand = unit(split(), "brand", "US")
    assert brand["est_conv"] == 113.99  # 9,119.25 / 80
    assert brand["est_clicks"] == 3_039.75  # 9,119.25 / 3


def test_no_cpc_means_no_click_forecast_rather_than_a_zero() -> None:
    units = [{k: v for k, v in row.items() if k != "avg_cpc_usd"} for row in UNITS]
    assert unit(split(units), "brand", "US")["est_clicks"] is None


def test_a_unit_below_the_floor_is_raised_to_it_and_the_rest_renormalised() -> None:
    result = split(envelope=5_000)
    remarketing = unit(result, "remarketing", "US")
    assert remarketing["usd"] == 1_000  # the $1,000 monthly floor
    assert remarketing["floor_applied"] is True
    assert sum(row["usd"] for row in result["allocation"]) == 5_000
    assert not any(row["below_floor"] for row in result["allocation"])


def test_raising_one_unit_to_the_floor_can_pull_another_under_it_and_that_settles() -> None:
    """The fixed point the solver iterates to, not a single pass."""
    result = split(envelope=4_100)
    floored = [row for row in result["allocation"] if row["floor_applied"]]
    assert len(floored) >= 2
    assert sum(row["usd"] for row in result["allocation"]) == 4_100
    assert all(row["usd"] >= 1_000 for row in result["allocation"])


def test_an_envelope_too_small_for_every_floor_defers_the_weakest_units() -> None:
    result = split(envelope=2_500)
    assert len(result["allocation"]) == 2
    assert result["deferred"]
    deferred_keys = {row["unit"] for row in result["deferred"]}
    assert "remarketing/US/bottom" in deferred_keys
    assert all(row["usd"] >= 1_000 for row in result["allocation"])


def test_the_weakest_unit_is_the_one_deferred() -> None:
    result = split(envelope=3_000)
    assert "remarketing/US/bottom" in {row["unit"] for row in result["deferred"]}
    assert {row["campaign_ref"] for row in result["allocation"]} <= {"brand", "nonbrand"}


def test_an_envelope_below_one_floor_raises_rather_than_funding_nothing() -> None:
    with pytest.raises(CalcError, match="cannot fund a single campaign"):
        split(envelope=500)


def test_a_unit_with_a_spend_cap_is_pinned_there_and_the_surplus_redistributed() -> None:
    units = [{**UNITS[0], "max_spend_usd": 2_000}, *UNITS[1:]]
    result = split(units)
    brand = unit(result, "brand", "US")
    assert brand["usd"] == 2_000
    assert brand["cap_applied"] is True
    assert sum(row["usd"] for row in result["allocation"]) == 20_000


def test_the_residual_never_pushes_a_unit_over_its_cap() -> None:
    """A cap is a real ceiling; a floor is a minimum, so only floors may be exceeded."""
    # Caps just above each unit's proportional share, so nothing is *pinned* at
    # a cap and the naive rule ("skip units pinned at a cap") would hand the
    # stray cent to a unit that then exceeds its ceiling by 0.006.
    units = [{**row, "max_spend_usd": 4_000.004} for row in UNITS]
    result = split(units, envelope=16_000.01)
    assert all(row["usd"] <= 4_000.004 for row in result["allocation"])
    # Cap capacity is 16,000.016 against a 16,000.01 envelope, so almost all of
    # it is spendable — and the fraction that is not is reported rather than
    # absorbed into a row that then contradicts its own ceiling.
    assert result["allocated_usd"] == 16_000
    assert result["unallocated_usd"] == 0.01


def test_a_floored_unit_absorbs_surplus_once_the_caps_are_what_binds() -> None:
    """The fixed point pins a floored unit; it must not then be frozen there.

    Every unit is capped at $4,000.004 against a $16,000.01 envelope. The naive
    solver holds `remarketing` at its $1,000 floor — pinned before the units
    above it hit their caps — and leaves $3,000 of the envelope unspendable.
    """
    units = [{**row, "max_spend_usd": 4_000.004} for row in UNITS]
    result = split(units, envelope=16_000.01)
    assert unit(result, "remarketing", "US")["usd"] == 4_000
    assert result["allocated_usd"] == 16_000


def test_every_unit_capped_leaves_the_remainder_reported_not_hidden() -> None:
    units = [{**row, "max_spend_usd": 1_500} for row in UNITS]
    result = split(units)
    assert result["allocated_usd"] == 6_000
    assert result["unallocated_usd"] == 14_000


def test_the_solver_is_deterministic() -> None:
    assert split() == split()
    shuffled = [UNITS[2], UNITS[0], UNITS[3], UNITS[1]]
    first = {(row["campaign_ref"], row["market"]): row["usd"] for row in split()["allocation"]}
    second = {
        (row["campaign_ref"], row["market"]): row["usd"] for row in split(shuffled)["allocation"]
    }
    assert first == second


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("forecast_cpa_usd", 0),
        ("target_cpa_usd", 0),
        ("strategic_weight", 0),
        ("strategic_weight", -1),
    ],
)
def test_an_unallocatable_unit_is_excluded_with_its_reason(field: str, value: float) -> None:
    units = [{**UNITS[0], field: value}, *UNITS[1:]]
    calc = allocation.split_v1(frame(units), constants=CONSTANTS, envelope_usd=20_000)
    assert len(calc.result["allocation"]) == 3
    assert calc.excluded[0]["unit"] == "brand/US/bottom"
    assert "positive" in calc.excluded[0]["reason"]


def test_every_unit_being_unallocatable_raises() -> None:
    units = [{**row, "forecast_cpa_usd": 0} for row in UNITS]
    with pytest.raises(CalcError, match="no unit could be allocated to"):
        allocation.split_v1(frame(units), constants=CONSTANTS, envelope_usd=20_000)


def test_a_non_positive_envelope_is_refused() -> None:
    with pytest.raises(CalcError, match="envelope_usd must be positive"):
        split(envelope=0)


# --- what-if ----------------------------------------------------------------

EDITED = [
    {
        "campaign_ref": "brand",
        "market": "US",
        "funnel_stage": "bottom",
        "edited_usd": 4_000,
        "baseline_usd": 9_119.25,
        "forecast_cpa_usd": 80,
        "avg_cpc_usd": 3.0,
        "max_spend_usd": 3_000,
    },
    {
        "campaign_ref": "nonbrand",
        "market": "US",
        "funnel_stage": "mid",
        "edited_usd": 14_000,
        "baseline_usd": 5_611.85,
        "forecast_cpa_usd": 260,
        "avg_cpc_usd": 6.0,
    },
    {
        "campaign_ref": "remarketing",
        "market": "US",
        "funnel_stage": "bottom",
        "edited_usd": 500,
        "baseline_usd": 1_215.90,
        "forecast_cpa_usd": 120,
        "avg_cpc_usd": 2.0,
    },
]


def whatif(rows: list[dict] | None = None, envelope: float = 20_000, **kwargs: object) -> dict:
    return allocation.whatif_v1(
        frame(rows if rows is not None else EDITED),
        constants=CONSTANTS,
        envelope_usd=envelope,
        **kwargs,  # type: ignore[arg-type]
    ).result


def test_an_edit_that_breaks_the_envelope_reports_the_delta() -> None:
    """PRD S2-P3: the gate returns 422 with the delta, so the delta has to exist."""
    result = whatif()
    assert result["requested_usd"] == 18_500
    assert result["delta_usd"] == -1_500
    assert result["delta_pct"] == -7.5
    assert result["envelope_breach"] is True


def test_an_edit_inside_the_tolerance_is_not_a_breach() -> None:
    rows = [{**EDITED[0], "edited_usd": 5_950, "max_spend_usd": 99_999}, *EDITED[1:]]
    result = whatif(rows)
    assert result["requested_usd"] == 20_450
    assert result["delta_pct"] == 2.25
    assert result["envelope_breach"] is True
    assert whatif(rows, tolerance_pct=5)["envelope_breach"] is False


def test_the_default_tolerance_is_the_one_invariant_4_names() -> None:
    assert allocation.DEFAULT_TOLERANCE_PCT == 0.5
    assert whatif()["tolerance_pct"] == 0.5


def test_a_unit_edited_above_what_it_can_absorb_is_forecast_on_the_capped_amount() -> None:
    """Forecasting the requested figure would promise conversions that are not for sale."""
    brand = next(row for row in whatif()["allocation"] if row["campaign_ref"] == "brand")
    assert brand["requested_usd"] == 4_000
    assert brand["usd"] == 3_000
    assert brand["wasted_usd"] == 1_000
    assert brand["est_conv"] == 37.5  # 3,000 / 80, not 4,000 / 80
    assert brand["cap_applied"] is True
    assert whatif()["capped"] == [
        {"unit": "brand/US/bottom", "requested_usd": 4_000, "absorbable_usd": 3_000}
    ]


def test_the_deltas_are_against_the_approved_baseline() -> None:
    nonbrand = next(row for row in whatif()["allocation"] if row["campaign_ref"] == "nonbrand")
    assert nonbrand["delta_usd"] == 8_388.15  # 14,000 - 5,611.85
    assert nonbrand["delta_pct"] == 149.47


def test_no_baseline_means_no_percentage_delta_rather_than_a_division_by_zero() -> None:
    rows = [{k: v for k, v in row.items() if k != "baseline_usd"} for row in EDITED]
    assert all(row["delta_pct"] is None for row in whatif(rows)["allocation"])


def test_a_unit_edited_under_the_floor_is_named() -> None:
    result = whatif()
    assert result["below_floor"] == [
        {"unit": "remarketing/US/bottom", "usd": 500, "floor_usd": 1_000}
    ]


def test_zero_is_a_legitimate_edit_and_is_not_below_the_floor() -> None:
    """Switching a campaign off is an allocation decision, not an underfunded one."""
    rows = [{**EDITED[0], "edited_usd": 0, "max_spend_usd": 99_999}, *EDITED[1:]]
    result = whatif(rows)
    brand = next(row for row in result["allocation"] if row["campaign_ref"] == "brand")
    assert brand["usd"] == 0
    assert brand["below_floor"] is False
    assert brand["est_conv"] == 0
    assert result["below_floor"] == [
        {"unit": "remarketing/US/bottom", "usd": 500, "floor_usd": 1_000}
    ]


def test_the_totals_use_effective_spend_not_requested() -> None:
    result = whatif()
    assert result["effective_usd"] == 17_500
    assert result["wasted_usd"] == 1_000
    assert result["est_conv"] == 95.52  # 37.5 + 53.85 + 4.17
    assert result["est_cpa_usd"] == 183.21  # 17,500 / 95.52


def test_a_negative_edit_is_excluded_with_its_reason() -> None:
    rows = [{**EDITED[0], "edited_usd": -100}, *EDITED[1:]]
    calc = allocation.whatif_v1(frame(rows), constants=CONSTANTS, envelope_usd=20_000)
    assert len(calc.result["allocation"]) == 2
    assert "non-negative edited_usd" in calc.excluded[0]["reason"]


def test_every_edit_being_unusable_raises() -> None:
    rows = [{**row, "forecast_cpa_usd": 0} for row in EDITED]
    with pytest.raises(CalcError, match="no edited unit could be re-forecast"):
        allocation.whatif_v1(frame(rows), constants=CONSTANTS, envelope_usd=20_000)


def test_a_negative_tolerance_is_refused() -> None:
    with pytest.raises(CalcError, match="tolerance_pct must not be negative"):
        whatif(tolerance_pct=-1)


def test_a_non_positive_envelope_is_refused_for_a_what_if_too() -> None:
    with pytest.raises(CalcError, match="envelope_usd must be positive"):
        whatif(envelope=0)


#: §17 PF3: "Budget what-if recalculation round trip ≤ 2 s for 40 campaigns ×
#: 4 markets". A ceiling, not a benchmark — the calculation is a single pandas
#: pass and takes milliseconds, so the only thing a bound this generous can
#: catch is somebody making it quadratic, which is exactly the regression PF3
#: is about. Wall clock is a bad assertion in general; it is the right one
#: here because the threshold *is* wall clock.
PF3_BUDGET_S = 2.0


def test_a_what_if_over_forty_campaigns_by_four_markets_is_still_one_pass() -> None:
    """PF3, including its number. The shape is O(n) and it stays under 2 s."""
    rows = [
        {
            "campaign_ref": f"c{index}",
            "market": market,
            "funnel_stage": "mid",
            "edited_usd": 125,
            "baseline_usd": 125,
            "forecast_cpa_usd": 100 + index,
            "avg_cpc_usd": 5,
        }
        for index in range(40)
        for market in ("US", "DE", "UK", "FR")
    ]
    started = time.perf_counter()
    result = whatif(rows, envelope=20_000)
    elapsed = time.perf_counter() - started

    assert result["unit_count"] == 160
    assert result["requested_usd"] == 20_000
    assert result["envelope_breach"] is False
    assert elapsed < PF3_BUDGET_S, (
        f"the recalculation took {elapsed:.2f}s against PF3's {PF3_BUDGET_S}s for "
        "40 campaigns x 4 markets"
    )


def test_the_summary_says_breach_when_it_is_one() -> None:
    calc = allocation.whatif_v1(frame(EDITED), constants=CONSTANTS, envelope_usd=20_000)
    assert "BREACH" in calc.summary
    assert "-7.50%" in calc.summary


# ---------------------------------------------------------------------------
# allocation.share_v1
# ---------------------------------------------------------------------------

#: The approved split, re-grouped the way stages 2.3 and 2.4 need to read it.
#: Worked out by hand at an envelope of $20,000:
#:
#:     group            lines          usd     pct      daily      target CPA
#:     search|US        6,000 + 3,000  9,000   45.00%   296.05     216.67
#:     pmax|US          5,000          5,000   25.00%   164.47     300.00
#:     search|DE        4,000          4,000   20.00%   131.58     220.00
#:     remarketing|US   2,000          2,000   10.00%    65.79     120.00
#:                                    20,000  100.00%
#:
#: `search|US`'s target CPA is the one worth checking: the unweighted mean of
#: 200 and 250 is 225, and the spend-weighted answer is 216.67. A campaign that
#: is three quarters brand does not inherit the non-brand ceiling.
SHARE_LINES = [
    {
        "group": "search|US",
        "campaign_ref": "brand",
        "usd": 6_000,
        "target_cpa_usd": 200,
        "est_conv": 30,
    },
    {
        "group": "search|US",
        "campaign_ref": "nonbrand",
        "usd": 3_000,
        "target_cpa_usd": 250,
        "est_conv": 12,
    },
    {
        "group": "pmax|US",
        "campaign_ref": "pmax",
        "usd": 5_000,
        "target_cpa_usd": 300,
        "est_conv": 15,
    },
    {
        "group": "search|DE",
        "campaign_ref": "nonbrand",
        "usd": 4_000,
        "target_cpa_usd": 220,
        "est_conv": 16,
    },
    {
        "group": "remarketing|US",
        "campaign_ref": "remarketing",
        "usd": 2_000,
        "target_cpa_usd": 120,
        "est_conv": 14,
    },
]


def share(rows: list[dict[str, object]] | None = None, *, envelope: float = 20_000) -> dict:
    calc = allocation.share_v1(
        frame(rows if rows is not None else SHARE_LINES),
        constants=CONSTANTS,
        envelope_usd=envelope,
    )
    return calc.result


def test_share_totals_each_group_and_its_pct_of_the_envelope() -> None:
    assert [(row["group"], row["usd"], row["pct"]) for row in share()["groups"]] == [
        ("search|US", 9_000.0, 45.0),
        ("pmax|US", 5_000.0, 25.0),
        ("search|DE", 4_000.0, 20.0),
        ("remarketing|US", 2_000.0, 10.0),
    ]


def test_the_target_cpa_of_a_group_is_spend_weighted_not_averaged() -> None:
    """216.67, not the 225 an unweighted mean of 200 and 250 would give."""
    assert share()["groups"][0]["target_cpa_usd"] == 216.67


def test_the_daily_budget_is_the_monthly_over_googles_month_not_thirty() -> None:
    """9,000 / 30.4 = 296.05. Dividing by 30 would say 300.00 and overspend."""
    assert [row["daily_usd"] for row in share()["groups"]] == [296.05, 164.47, 131.58, 65.79]


def test_the_shares_sum_to_one_hundred_percent_when_the_lines_fill_the_envelope() -> None:
    result = share()
    assert result["total_usd"] == 20_000.0
    assert result["unallocated_usd"] == 0.0
    assert sum(row["pct"] for row in result["groups"]) == 100.0


def test_an_envelope_larger_than_the_lines_reports_the_remainder() -> None:
    """The shares are of the envelope, so they must not silently re-base to the lines."""
    result = share(envelope=25_000)
    assert result["unallocated_usd"] == 5_000.0
    assert sum(row["pct"] for row in result["groups"]) == 80.0


def test_the_conversions_of_a_group_are_summed() -> None:
    assert share()["groups"][0]["est_conv"] == 42.0


def test_a_line_with_no_group_is_excluded_with_its_reason() -> None:
    rows = [{"group": "  ", "usd": 1_000}, *SHARE_LINES]
    calc = allocation.share_v1(frame(rows), constants=CONSTANTS, envelope_usd=20_000)
    assert calc.excluded[0]["reason"] == "group is blank"
    assert calc.result["total_usd"] == 20_000.0


def test_a_line_with_a_negative_amount_is_excluded_rather_than_netted_off() -> None:
    rows = [{"group": "search|US", "usd": -500}, *SHARE_LINES]
    calc = allocation.share_v1(frame(rows), constants=CONSTANTS, envelope_usd=20_000)
    assert "must not be negative" in calc.excluded[0]["reason"]
    assert calc.result["groups"][0]["usd"] == 9_000.0


def test_a_non_positive_envelope_is_refused_for_a_share_too() -> None:
    with pytest.raises(CalcError, match="envelope_usd must be positive"):
        share(envelope=0)


def test_no_usable_line_raises_rather_than_returning_an_empty_split() -> None:
    with pytest.raises(CalcError, match="no allocation line could be grouped"):
        share([{"group": "", "usd": 10}])
