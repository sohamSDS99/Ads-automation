"""`power.sample_size_v1` (PRD §9.2, node 2.5.3).

The anchor case is a 5% baseline tested for a 20% relative lift at the shipped
alpha 0.05 / power 0.80. Standard two-proportion z-test, two-sided:

    p1 = 0.05, p2 = 0.06, pbar = 0.055
    n = (1.959964 x sqrt(2 x 0.055 x 0.945) + 0.841621 x sqrt(0.0475 + 0.0564))^2
        / 0.01^2
      = (0.631918 + 0.271281)^2 / 0.0001
      = 8157.68  ->  8158 visitors per arm, 408 conversions per arm

8158 is what any published two-proportion calculator gives for those inputs,
which is the point: this number is checkable against something outside this
repository.
"""

from __future__ import annotations

import pytest

from agent.calc import power
from agent.calc.registry import CalcError
from tests.calc_support import CONSTANTS, frame

TESTS = [
    {"id": "T1", "baseline_cvr_pct": 5.0, "mde_pct": 20, "clicks_per_day": 300},
    {"id": "T2", "baseline_cvr_pct": 2.0, "clicks_per_day": 100},
    {"id": "T3", "baseline_cvr_pct": 90.0, "mde_pct": 20},
]


def size(rows: list[dict] | None = None) -> dict:
    return power.sample_size_v1(
        frame(rows if rows is not None else TESTS), constants=CONSTANTS
    ).result


def sized(result: dict, identifier: str) -> dict:
    return next(item for item in result["tests"] if item["id"] == identifier)


def test_the_anchor_case_matches_a_published_calculator() -> None:
    row = sized(size(), "T1")
    assert row["target_cvr_pct"] == 6.0
    assert row["absolute_lift_pp"] == 1.0
    assert row["required_visitors_per_arm"] == 8_158
    assert row["required_conv_per_arm"] == 408  # ceil(8157.68 x 0.05)


def test_the_quantiles_are_the_textbook_ones() -> None:
    result = size()
    assert result["z_alpha"] == 1.96  # two-sided at alpha 0.05
    assert result["z_power"] == 0.8416  # one-sided at power 0.80


def test_days_to_significance_counts_every_arm() -> None:
    row = sized(size(), "T1")
    # 8,158 per arm x 2 arms / 300 clicks a day
    assert row["required_visitors_total"] == 16_316
    assert row["est_days_to_significance"] == 55
    assert row["est_weeks_to_significance"] == 7.8571


def test_more_arms_take_proportionally_longer() -> None:
    rows = [{**TESTS[0], "arms": 4}]
    row = sized(size(rows), "T1")
    assert row["arms"] == 4
    assert row["required_visitors_total"] == 32_632
    assert row["est_days_to_significance"] == 109


def test_the_mde_defaults_to_the_constant_when_a_test_does_not_state_one() -> None:
    row = sized(size(), "T2")
    assert row["mde_pct"] == CONSTANTS.value("test.min_mde_pct")
    assert row["target_cvr_pct"] == 2.4  # 2% x 1.20


def test_a_lower_baseline_needs_far_more_traffic() -> None:
    """The reason a backlog has to be sized rather than guessed."""
    result = size()
    assert sized(result, "T2")["required_visitors_per_arm"] == 21_109
    assert (
        sized(result, "T2")["required_visitors_per_arm"]
        > sized(result, "T1")["required_visitors_per_arm"]
    )


def test_a_larger_lift_is_cheaper_to_detect() -> None:
    rows = [{**TESTS[0], "mde_pct": 50}]
    assert sized(size(rows), "T1")["required_visitors_per_arm"] < 8_158


def test_no_click_forecast_means_no_duration_claim_rather_than_a_zero() -> None:
    rows = [{k: v for k, v in TESTS[0].items() if k != "clicks_per_day"}]
    row = sized(size(rows), "T1")
    assert row["required_visitors_per_arm"] == 8_158
    assert row["est_days_to_significance"] is None
    assert row["est_weeks_to_significance"] is None


def test_a_lift_that_implies_a_conversion_rate_over_100_is_not_a_hypothesis() -> None:
    calc = power.sample_size_v1(frame(TESTS), constants=CONSTANTS)
    assert [row["id"] for row in calc.result["tests"]] == ["T1", "T2"]
    assert calc.excluded[0]["id"] == "T3"
    assert "108.0% conversion rate" in calc.excluded[0]["reason"]


def test_the_sample_size_rounds_up_never_down() -> None:
    """A test stopped one observation short is the mistake this formula prevents."""
    rows = [{"id": "T", "baseline_cvr_pct": 5.0, "mde_pct": 20, "clicks_per_day": 301}]
    row = sized(size(rows), "T")
    assert row["required_visitors_per_arm"] == 8_158  # not 8,157
    assert row["est_days_to_significance"] == 55  # ceil(54.2), not 54


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("baseline_cvr_pct", 0, "outside (0, 100)"),
        ("baseline_cvr_pct", 100, "outside (0, 100)"),
        ("baseline_cvr_pct", -1, "outside (0, 100)"),
        ("mde_pct", 0, "outside (0, 500]"),
        ("mde_pct", 600, "outside (0, 500]"),
        ("arms", 1, "whole number >= 2"),
        ("arms", 2.5, "whole number >= 2"),
    ],
)
def test_an_unsizeable_test_is_excluded_with_its_reason(
    field: str, value: float, fragment: str
) -> None:
    rows = [{**TESTS[0], field: value}, TESTS[1]]
    calc = power.sample_size_v1(frame(rows), constants=CONSTANTS)
    assert [row["id"] for row in calc.result["tests"]] == ["T2"]
    assert calc.excluded[0]["id"] == "T1"
    assert fragment in calc.excluded[0]["reason"]


def test_no_test_being_sizeable_raises() -> None:
    rows = [{**row, "baseline_cvr_pct": 0} for row in TESTS]
    with pytest.raises(CalcError, match="no test could be sized"):
        power.sample_size_v1(frame(rows), constants=CONSTANTS)


@pytest.mark.parametrize(
    ("key", "value", "fragment"),
    [
        ("test.alpha", 1, "alpha must be in"),
        ("test.alpha", 0, "alpha must be in"),
        ("test.power", 1, "power must be in"),
        ("test.min_mde_pct", 0, "min_mde_pct must be positive"),
    ],
)
def test_a_broken_constant_is_refused_before_any_sizing(
    key: str, value: float, fragment: str
) -> None:
    with pytest.raises(CalcError, match=fragment):
        power.sample_size_v1(frame(TESTS), constants=CONSTANTS.merged({key: value}))


def test_a_stricter_alpha_needs_more_traffic() -> None:
    strict = power.sample_size_v1(
        frame(TESTS[:1]), constants=CONSTANTS.merged({"test.alpha": 0.01})
    ).result
    assert sized(strict, "T1")["required_visitors_per_arm"] > 8_158


def test_the_longest_run_is_reported_so_a_node_can_act_on_it() -> None:
    assert size()["longest_days_to_significance"] == 423  # T2


def test_no_duration_anywhere_reports_none_rather_than_zero() -> None:
    rows = [{"id": "T", "baseline_cvr_pct": 5.0}]
    assert size(rows)["longest_days_to_significance"] is None


def test_the_summary_names_the_first_test_and_the_longest_run() -> None:
    calc = power.sample_size_v1(frame(TESTS), constants=CONSTANTS)
    assert "408 conversions per arm for T1" in calc.summary
    assert "longest run 423 day(s)" in calc.summary
