"""`scenarios.envelope_v1` (PRD §9.2, node 2.2.3).

Baseline: a two-month forecast costing $12,000 then $13,200, so expected is the
$12,600 monthly mean. At the shipped constants (cautious 25%, aggressive 40%,
reserve 10%, floor $1,000/campaign):

    cautious   12,600 x 0.75 = 9,450
    expected                   12,600
    aggressive 12,600 x 1.40 = 17,640, capped by headroom
"""

from __future__ import annotations

import pytest

from agent.calc import scenarios
from agent.calc.registry import CalcError
from tests.calc_support import CONSTANTS, frame

FORECAST = [
    {"month": "2027-01", "clicks": 2_000, "conversions": 60, "cost_usd": 12_000},
    {"month": "2027-02", "clicks": 2_200, "conversions": 66, "cost_usd": 13_200},
]


def calc(**kwargs: object) -> dict:
    options: dict = {"campaign_count": 4}
    options.update(kwargs)
    return scenarios.envelope_v1(frame(FORECAST), constants=CONSTANTS, **options).result  # type: ignore[arg-type]


def named(result: dict, name: str) -> dict:
    return next(row for row in result["scenarios"] if row["name"] == name)


def test_expected_is_the_monthly_mean_of_the_forecast() -> None:
    result = calc()
    assert result["month_count"] == 2
    assert result["forecast_monthly_usd"] == 12_600  # 25,200 / 2
    assert named(result, "expected")["monthly_total_usd"] == 12_600


def test_the_steps_come_from_the_constants_file() -> None:
    result = calc()
    assert named(result, "cautious")["monthly_total_usd"] == 9_450
    assert named(result, "aggressive")["monthly_total_usd"] == 17_640


def test_the_experiment_reserve_is_carved_out_of_every_scenario() -> None:
    expected = named(calc(), "expected")
    assert expected["working_budget_usd"] == 11_340  # 12,600 x 90%
    assert expected["experiment_reserve_usd"] == 1_260
    assert expected["experiment_reserve_pct"] == 10


def test_aggressive_is_capped_by_measured_impression_share_headroom() -> None:
    """You cannot spend into impressions that do not exist."""
    aggressive = named(calc(headroom_pct=25), "aggressive")
    assert aggressive["monthly_total_usd"] == 15_750  # 12,600 x 1.25, not x 1.40
    assert aggressive["headroom_capped"] is True
    assert any("headroom" in risk for risk in aggressive["risks"])


def test_headroom_above_the_step_does_not_cap_anything() -> None:
    aggressive = named(calc(headroom_pct=80), "aggressive")
    assert aggressive["monthly_total_usd"] == 17_640
    assert aggressive["headroom_capped"] is False


def test_unmeasured_headroom_is_flagged_as_a_risk_rather_than_assumed_infinite() -> None:
    aggressive = named(calc(), "aggressive")
    assert aggressive["headroom_capped"] is False
    assert any("no impression-share headroom was measured" in risk for risk in aggressive["risks"])


def test_traffic_scales_with_spend_and_the_assumption_is_stated() -> None:
    result = calc()
    expected, cautious = named(result, "expected"), named(result, "cautious")
    assert expected["est_clicks"] == 2_100  # 4,200 / 2 months
    assert expected["est_conv"] == 63
    assert cautious["scale_vs_expected"] == 0.75
    assert cautious["est_clicks"] == 1_575
    assert cautious["est_conv"] == 47.25
    assert any("scale linearly" in note for note in cautious["assumptions"])


def test_every_scenario_shares_one_cpa_because_scaling_is_linear() -> None:
    """The reason `aggressive` can never be recommended by arithmetic alone."""
    result = calc()
    assert {named(result, name)["est_cpa"] for name in ("cautious", "expected", "aggressive")} == {
        200
    }


def test_the_floor_is_per_campaign_and_raises_a_scenario_that_falls_under_it() -> None:
    tiny = [{"month": "2027-01", "clicks": 100, "conversions": 3, "cost_usd": 600}]
    result = scenarios.envelope_v1(frame(tiny), constants=CONSTANTS, campaign_count=4).result
    assert result["floor_total_usd"] == 4_000  # 4 campaigns x $1,000
    cautious = named(result, "cautious")
    assert cautious["monthly_total_usd"] == 4_000
    assert cautious["floor_applied"] is True
    assert any("floor" in note for note in cautious["assumptions"])


def test_the_floor_scales_with_the_campaign_count() -> None:
    assert calc(campaign_count=1)["floor_total_usd"] == 1_000
    assert calc(campaign_count=12)["floor_total_usd"] == 12_000


def test_a_forecast_cpa_over_target_recommends_cautious_and_says_why() -> None:
    result = calc(target_cpa_usd=150)
    assert result["recommended"] == "cautious"
    assert "above the $150.00 target" in result["recommendation_reason"]


def test_a_forecast_cpa_within_target_recommends_expected() -> None:
    result = calc(target_cpa_usd=250)
    assert result["recommended"] == "expected"
    assert "within the $250.00 target" in result["recommendation_reason"]


def test_no_target_recommends_expected_and_admits_it_had_nothing_to_test_against() -> None:
    result = calc()
    assert result["recommended"] == "expected"
    assert "no target CPA" in result["recommendation_reason"]


def test_aggressive_is_never_recommended_automatically() -> None:
    for target in (None, 100, 200, 1_000, 10_000):
        assert calc(target_cpa_usd=target)["recommended"] != "aggressive"


def test_pipeline_value_appears_only_when_an_acv_is_supplied() -> None:
    assert "est_pipeline_usd" not in named(calc(), "expected")
    expected = named(calc(acv_usd=20_000), "expected")
    assert expected["est_pipeline_usd"] == 1_260_000  # 63 conversions x $20,000


def test_a_zero_campaign_count_is_refused() -> None:
    with pytest.raises(CalcError, match="campaign_count must be at least 1"):
        calc(campaign_count=0)


def test_negative_headroom_is_refused() -> None:
    with pytest.raises(CalcError, match="headroom_pct must not be negative"):
        calc(headroom_pct=-10)


def test_a_non_positive_acv_is_refused() -> None:
    with pytest.raises(CalcError, match="acv_usd must be positive"):
        calc(acv_usd=0)


def test_a_forecast_that_costs_nothing_has_no_envelope() -> None:
    free = [{"month": "2027-01", "clicks": 0, "conversions": 0, "cost_usd": 0}]
    with pytest.raises(CalcError, match="costs nothing"):
        scenarios.envelope_v1(frame(free), constants=CONSTANTS, campaign_count=1)


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("budget.cautious_step_pct", 100, "cautious_step_pct must be in"),
        ("budget.aggressive_step_pct", -5, "aggressive_step_pct must not be negative"),
        ("budget.experiment_reserve_pct", 100, "experiment_reserve_pct must be in"),
    ],
)
def test_a_broken_constant_is_refused_before_any_arithmetic(
    key: str, value: float, fragment: str
) -> None:
    with pytest.raises(CalcError, match=fragment):
        scenarios.envelope_v1(
            frame(FORECAST), constants=CONSTANTS.merged({key: value}), campaign_count=4
        )


def test_the_summary_carries_all_three_totals_and_the_recommendation() -> None:
    calc_result = scenarios.envelope_v1(
        frame(FORECAST), constants=CONSTANTS, campaign_count=4, headroom_pct=25, target_cpa_usd=250
    )
    assert "cautious $9,450.00" in calc_result.summary
    assert "aggressive $15,750.00" in calc_result.summary
    assert "recommending expected" in calc_result.summary
