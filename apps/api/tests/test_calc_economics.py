"""`economics.max_cpa_v1` and `economics.payback_v1` (PRD §9.2).

    maxCPL = (ACV x grossMargin / targetCacRatio) x closeRate(lead->won)

Every expected value here was worked out by hand from the two segments in
`calc_support.SEGMENTS`, against the constants the product ships
(`target_cac_ratio` 3.0, `safety_margin_pct` 15). The arithmetic is written into
the test names and the comments so that a failure says which step moved.
"""

from __future__ import annotations

import pytest

from agent.calc import economics
from agent.calc.registry import CalcError
from tests.calc_support import CONSTANTS, SEGMENTS, frame


def result() -> dict:
    return economics.max_cpa_v1(frame(SEGMENTS), constants=CONSTANTS).result


def test_the_enterprise_ceiling_is_hand_checkable() -> None:
    row = next(r for r in result()["by_segment"] if r["segment"] == "enterprise")
    # 60,000 x 80% = 48,000 gross profit
    assert row["gross_profit_usd"] == 48_000
    # 48,000 / 3.0 = 16,000 per closed-won customer
    assert row["max_cpa_won_usd"] == 16_000
    # 16,000 x 12% = 1,920 per lead
    assert row["max_cpl_usd"] == 1_920
    # 15% under the ceiling
    assert row["target_cpa_won_usd"] == 13_600
    assert row["target_cpl_usd"] == 1_632
    # ROAS is revenue over cost, not profit over cost: 60,000 / 13,600
    assert row["target_roas"] == 4.4118


def test_the_smb_ceiling_is_hand_checkable() -> None:
    row = next(r for r in result()["by_segment"] if r["segment"] == "smb")
    assert row["gross_profit_usd"] == 9_000  # 12,000 x 75%
    assert row["max_cpa_won_usd"] == 3_000  # 9,000 / 3
    assert row["max_cpl_usd"] == 600  # 3,000 x 20%
    assert row["target_cpl_usd"] == 510  # x 0.85
    assert row["target_roas"] == 4.7059  # 12,000 / 2,550


def test_the_blend_is_deal_weighted_and_derived_from_blended_inputs() -> None:
    blended = result()["blended"]
    assert blended["weight_basis"] == "deals"
    # (60,000 x 10 + 12,000 x 40) / 50
    assert blended["acv_usd"] == 21_600
    # (48,000 x 10 + 9,000 x 40) / 50
    assert blended["gross_profit_usd"] == 16_800
    # recovered from the blended figures, not averaged: 16,800 / 21,600
    assert blended["gross_margin_pct"] == 77.78
    # (12 x 10 + 20 x 40) / 50
    assert blended["lead_to_won_pct"] == 18.4
    assert blended["max_cpa_won_usd"] == 5_600  # 16,800 / 3
    assert blended["max_cpl_usd"] == 1_030.4  # 5,600 x 18.4%
    assert blended["target_cpl_usd"] == 875.84


def test_the_blend_is_not_the_mean_of_the_two_ceilings() -> None:
    """The bug this guards: averaging outputs answers a question nobody asked."""
    blended = result()["blended"]
    naive = (16_000 + 3_000) / 2
    assert blended["max_cpa_won_usd"] != naive


def test_a_flat_blend_is_used_and_reported_when_no_deal_counts_arrive() -> None:
    rows = [{k: v for k, v in row.items() if k != "deals"} for row in SEGMENTS]
    blended = economics.max_cpa_v1(frame(rows), constants=CONSTANTS).result["blended"]
    assert blended["weight_basis"] == "equal"
    assert blended["acv_usd"] == 36_000  # (60,000 + 12,000) / 2
    assert blended["max_cpa_won_usd"] == 9_500  # (48,000 + 9,000) / 2 / 3


def test_zero_deal_counts_fall_back_to_a_flat_blend_rather_than_dividing_by_nothing() -> None:
    rows = [{**row, "deals": 0} for row in SEGMENTS]
    blended = economics.max_cpa_v1(frame(rows), constants=CONSTANTS).result["blended"]
    assert blended["weight_basis"] == "equal"
    assert blended["acv_usd"] == 36_000


@pytest.mark.parametrize(
    ("field", "value", "fragment"),
    [
        ("acv_usd", 0, "no revenue to pay out of"),
        ("acv_usd", -100, "no revenue to pay out of"),
        ("gross_margin_pct", 0, "outside (0, 100]"),
        ("gross_margin_pct", 120, "outside (0, 100]"),
        ("lead_to_won_pct", 0, "outside (0, 100]"),
        ("lead_to_won_pct", 101, "outside (0, 100]"),
    ],
)
def test_one_unusable_segment_is_excluded_with_its_reason_not_silently_averaged(
    field: str, value: float, fragment: str
) -> None:
    rows = [{**SEGMENTS[0], field: value}, SEGMENTS[1]]
    calc = economics.max_cpa_v1(frame(rows), constants=CONSTANTS)
    assert [row["segment"] for row in calc.result["by_segment"]] == ["smb"]
    assert calc.excluded[0]["segment"] == "enterprise"
    assert fragment in calc.excluded[0]["reason"]
    # The exclusion must not quietly become the blend: one segment in, one out.
    assert calc.result["segment_count"] == 1
    assert calc.result["blended"]["max_cpa_won_usd"] == 3_000


def test_every_segment_being_unusable_raises_and_lists_why() -> None:
    rows = [{**row, "acv_usd": 0} for row in SEGMENTS]
    with pytest.raises(CalcError, match="no segment produced a usable ceiling"):
        economics.max_cpa_v1(frame(rows), constants=CONSTANTS)


def test_a_missing_column_names_itself() -> None:
    rows = [{k: v for k, v in row.items() if k != "lead_to_won_pct"} for row in SEGMENTS]
    with pytest.raises(CalcError, match="missing required column\\(s\\): lead_to_won_pct"):
        economics.max_cpa_v1(frame(rows), constants=CONSTANTS)


def test_an_empty_frame_raises_rather_than_returning_a_zero() -> None:
    import pandas as pd

    empty = pd.DataFrame(columns=["segment", "acv_usd", "gross_margin_pct", "lead_to_won_pct"])
    with pytest.raises(CalcError, match="is empty"):
        economics.max_cpa_v1(empty, constants=CONSTANTS)


def test_a_project_override_of_the_cac_ratio_moves_the_ceiling_and_the_version() -> None:
    overridden = CONSTANTS.merged({"economics.target_cac_ratio": 4.0})
    calc = economics.max_cpa_v1(frame(SEGMENTS), constants=overridden)
    row = next(r for r in calc.result["by_segment"] if r["segment"] == "enterprise")
    assert row["max_cpa_won_usd"] == 12_000  # 48,000 / 4
    assert "+ovr." in calc.calc_version


def test_a_broken_constant_is_refused_before_any_arithmetic() -> None:
    with pytest.raises(CalcError, match="safety_margin_pct must be in"):
        economics.max_cpa_v1(
            frame(SEGMENTS), constants=CONSTANTS.merged({"economics.safety_margin_pct": 100})
        )
    with pytest.raises(CalcError, match="target_cac_ratio must be positive"):
        economics.max_cpa_v1(
            frame(SEGMENTS), constants=CONSTANTS.merged({"economics.target_cac_ratio": 0})
        )


# --- payback ----------------------------------------------------------------


def payback_rows() -> list[dict]:
    ceilings = result()["by_segment"]
    return [
        {**row, "max_cpa_won_usd": ceiling["max_cpa_won_usd"]}
        for row, ceiling in zip(SEGMENTS, ceilings, strict=True)
    ]


def test_payback_at_the_ceiling_is_exactly_the_cac_ratio_in_months() -> None:
    """The relationship worth knowing: pay the ceiling and you get the ratio back.

    16,000 CAC against 4,000/month of gross profit is 4 months, and LTV:CAC is
    3.0 — which is `target_cac_ratio`, by construction. If that identity ever
    breaks, one of the two formulas has drifted from the other.
    """
    calc = economics.payback_v1(frame(payback_rows()), constants=CONSTANTS)
    enterprise = next(r for r in calc.result["by_segment"] if r["segment"] == "enterprise")
    assert enterprise["monthly_gross_profit_usd"] == 4_000  # 48,000 / 12
    assert enterprise["payback_months"] == 4  # 16,000 / 4,000
    assert enterprise["ltv_usd"] == 48_000  # one 12-month term
    assert enterprise["ltv_to_cac"] == CONSTANTS.value("economics.target_cac_ratio")


def test_lifetime_defaults_to_one_contract_term_and_can_be_extended() -> None:
    rows = [{**row, "lifetime_months": 36} for row in payback_rows()]
    calc = economics.payback_v1(frame(rows), constants=CONSTANTS)
    enterprise = next(r for r in calc.result["by_segment"] if r["segment"] == "enterprise")
    assert enterprise["lifetime_months"] == 36
    assert enterprise["ltv_usd"] == 144_000  # 4,000 x 36
    assert enterprise["ltv_to_cac"] == 9


def test_payback_can_be_asked_about_a_real_cpa_instead_of_the_ceiling() -> None:
    rows = [{**row, "planned_cpa_usd": 8_000} for row in payback_rows()]
    calc = economics.payback_v1(frame(rows), constants=CONSTANTS, cac_column="planned_cpa_usd")
    enterprise = next(r for r in calc.result["by_segment"] if r["segment"] == "enterprise")
    assert enterprise["cac_usd"] == 8_000
    assert enterprise["payback_months"] == 2  # 8,000 / 4,000
    assert calc.inputs["cac_column"] == "planned_cpa_usd"


def test_a_zero_contract_term_is_excluded_not_divided_by() -> None:
    rows = [{**payback_rows()[0], "contract_term_months": 0}, payback_rows()[1]]
    calc = economics.payback_v1(frame(rows), constants=CONSTANTS)
    assert [row["segment"] for row in calc.result["by_segment"]] == ["smb"]
    assert "term=0.0" in calc.excluded[0]["reason"]


def test_every_segment_unusable_for_payback_raises() -> None:
    rows = [{**row, "contract_term_months": 0} for row in payback_rows()]
    with pytest.raises(CalcError, match="no segment produced a usable payback"):
        economics.payback_v1(frame(rows), constants=CONSTANTS)


def test_the_summary_is_a_line_a_person_can_read() -> None:
    calc = economics.max_cpa_v1(frame(SEGMENTS), constants=CONSTANTS)
    assert "Max CPL $1,030.40" in calc.summary
    assert "3x CAC ratio" in calc.summary
    assert "15% safety margin" in calc.summary
