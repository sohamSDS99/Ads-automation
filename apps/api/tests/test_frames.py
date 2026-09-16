"""The CRM and campaign rollups (PRD §18 law 3).

Share of revenue, ACV, LTV, payback and the seasonal month arrays are the
numbers stage 1.1 reports, so they are computed here rather than asked of a
model. The fixtures are four deals and four campaign-months — small enough that
every expected value below was worked out by hand.
"""

from __future__ import annotations

import uuid
from typing import Any

from agent.nodes import frames

WON: list[dict[str, Any]] = [
    {
        "account_name": "Acme",
        "industry": "Chemicals",
        "country": "US",
        "employee_count": 120,
        "deal_value": 24000,
        "created_at": "2025-03-04",
    },
    {
        "account_name": "Borax",
        "industry": "Chemicals",
        "country": "US",
        "employee_count": 180,
        "deal_value": 16000,
        "created_at": "2025-03-19",
    },
    {
        "account_name": "Rheinwerk",
        "industry": "Manufacturing",
        "country": "DE",
        "employee_count": 640,
        "deal_value": 40000,
        "created_at": "2025-09-02",
    },
    {
        "account_name": "Nordmetall",
        "industry": "Manufacturing",
        "country": "DE",
        "employee_count": 900,
        "deal_value": 20000,
        "created_at": "2025-10-11",
    },
]


def won_frame() -> Any:
    return frames.crm_frame(WON, [uuid.uuid4() for _ in WON])


def test_segments_are_ranked_by_revenue_and_share_is_of_revenue() -> None:
    segments, dropped = frames.crm_segments(won_frame())

    assert dropped == 0
    assert [item.key for item in segments] == [
        "Manufacturing|DE|250-999",
        "Chemicals|US|50-249",
    ]
    assert [item.revenue for item in segments] == [60000.0, 40000.0]
    # 60,000 and 40,000 of 100,000 — of money, not of logos.
    assert [item.share_of_revenue_pct for item in segments] == [60.0, 40.0]
    assert [item.avg_deal_value for item in segments] == [30000.0, 20000.0]
    assert all(item.evidence_ids for item in segments)


def test_headcount_bands_are_coarse_and_a_missing_one_is_unknown() -> None:
    assert frames.size_band(1) == "1-49"
    assert frames.size_band(49) == "1-49"
    assert frames.size_band(50) == "50-249"
    assert frames.size_band(1000) == "1000+"
    assert frames.size_band(None) == "unknown"


def test_a_crm_export_with_no_headcount_column_still_segments() -> None:
    rows = [{"account_name": "A", "industry": "X", "country": "US", "deal_value": 10}]
    segments, _ = frames.crm_segments(frames.crm_frame(rows, [uuid.uuid4()]))
    assert segments[0].size_band == "unknown"
    assert segments[0].share_of_revenue_pct == 100.0


def test_ltv_and_payback_are_derived_from_the_assumptions_not_stated() -> None:
    economics = frames.crm_economics(
        won_frame(), gross_margin_pct=80, lifetime_months=36, ltv_cac_ratio=3
    )

    assert economics.deals == 4
    assert economics.revenue == 100000.0
    assert economics.acv == 25000.0
    assert economics.median_deal_value == 22000.0
    # 25,000 x 3 years x 80% margin.
    assert economics.ltv_estimate == 60000.0
    assert economics.target_cac == 20000.0
    # 20,000 target CAC over (25,000/12 x 0.8) monthly gross profit.
    assert economics.payback_months == 12.0


def test_absurd_assumptions_are_clamped_rather_than_producing_absurd_money() -> None:
    economics = frames.crm_economics(
        won_frame(), gross_margin_pct=500, lifetime_months=0, ltv_cac_ratio=0
    )
    assert economics.gross_margin_pct == 100.0
    assert economics.lifetime_months == 1.0
    assert economics.target_cac == economics.ltv_estimate, "a ratio below 1 is clamped to 1"


def test_economics_of_an_empty_crm_are_zero_not_an_error() -> None:
    economics = frames.crm_economics(
        frames.crm_frame([], []), gross_margin_pct=50, lifetime_months=12, ltv_cac_ratio=3
    )
    assert economics.acv == 0.0
    assert economics.ltv_estimate == 0.0
    assert economics.payback_months is None


def test_demand_months_are_relative_to_the_country_not_across_countries() -> None:
    demand = {item.country: item for item in frames.crm_demand_by_country(won_frame())}

    assert set(demand) == {"US", "DE"}
    assert demand["US"].monthly_deals[2] == 2, "both US deals closed in March"
    assert demand["US"].demand_months == (3,)
    assert 3 not in demand["US"].dead_months
    assert demand["DE"].demand_months == (9, 10)
    assert demand["DE"].monthly_deals[8] == 1


def test_a_crm_with_no_dates_has_no_demand_profile() -> None:
    rows = [{"account_name": "A", "country": "US", "deal_value": 1}]
    assert frames.crm_demand_by_country(frames.crm_frame(rows, [uuid.uuid4()])) == []


def test_lost_reasons_are_ranked_and_cite_their_rows() -> None:
    rows = [
        {"account_name": "A", "close_reason": "price", "deal_value": 10, "industry": "X"},
        {"account_name": "B", "close_reason": "price", "deal_value": 20, "industry": "Y"},
        {"account_name": "C", "close_reason": "timing", "deal_value": 5, "industry": "X"},
        {"account_name": "D", "close_reason": "", "deal_value": 5, "industry": "X"},
    ]
    reasons = frames.lost_reasons(frames.crm_frame(rows, [uuid.uuid4() for _ in rows]))

    assert [item["reason"] for item in reasons] == ["price", "timing"]
    assert reasons[0]["deals"] == 2
    assert reasons[0]["lost_value"] == 30.0
    assert reasons[0]["evidence_ids"]


def test_the_campaign_delta_compares_the_windows_own_halves() -> None:
    rows = [
        {
            "campaign": "Brand",
            "month": "2025-01",
            "cost": 100.0,
            "conversions": 4.0,
            "conversion_value": 2000.0,
        },
        {
            "campaign": "Brand",
            "month": "2025-06",
            "cost": 120.0,
            "conversions": 6.0,
            "conversion_value": 3000.0,
        },
        {
            "campaign": "Generic",
            "month": "2025-01",
            "cost": 400.0,
            "conversions": 1.0,
            "conversion_value": 500.0,
        },
        {
            "campaign": "Generic",
            "month": "2025-06",
            "cost": 500.0,
            "conversions": 0.0,
            "conversion_value": 0.0,
        },
    ]
    rollup = {
        item.campaign: item for item in frames.campaign_rollup(rows, [uuid.uuid4() for _ in rows])
    }

    brand = rollup["Brand"]
    assert brand.cost == 220.0
    assert brand.cpa == 22.0
    assert brand.roas == 22.727
    # 100/4 = 25.00 then 120/6 = 20.00: a 20% improvement.
    assert brand.first_half_cpa == 25.0
    assert brand.second_half_cpa == 20.0
    assert brand.cpa_delta_pct == -20.0
    assert brand.period == "2025-01..2025-06"

    generic = rollup["Generic"]
    # Nothing converted in the second half, so there is no CPA to compare to.
    assert generic.second_half_cpa is None
    assert generic.cpa_delta_pct is None


def test_a_single_month_window_has_no_halves_to_compare() -> None:
    rows = [{"campaign": "Solo", "month": "2025-01", "cost": 10.0, "conversions": 1.0}]
    [row] = frames.campaign_rollup(rows, [uuid.uuid4()])
    assert row.cpa == 10.0
    assert row.cpa_delta_pct is None


def test_campaign_rows_without_a_campaign_name_are_dropped() -> None:
    rows = [
        {"campaign": "", "cost": 10.0, "conversions": 0.0},
        {"campaign": "Real", "cost": 10.0, "conversions": 0.0},
    ]
    assert [
        item.campaign for item in frames.campaign_rollup(rows, [uuid.uuid4(), uuid.uuid4()])
    ] == ["Real"]


def test_mismatched_payloads_and_ids_fail_loudly() -> None:
    """A silently misattributed citation is worse than a crash at the call site."""
    import pytest

    with pytest.raises(ValueError, match="length"):
        frames.with_ids([{"a": 1}, {"a": 2}], [uuid.uuid4()])
