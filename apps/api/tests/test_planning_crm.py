"""`planning/crm.py` — the frame a ceiling is computed from.

Every expected value below is hand-checked, for the same reason the `calc/`
suites are: a recorded fixture would agree with whatever the code does today,
including the day it starts dividing by the wrong denominator.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.export.contract import BusinessContext, Product
from agent.planning import crm


def won(**overrides: Any) -> dict[str, Any]:
    return {
        "account_name": "Acme",
        "industry": "Chemicals",
        "country": "US",
        "deal_value": 20_000,
        "created_at": "2026-02-01",
        **overrides,
    }


def lost(**overrides: Any) -> dict[str, Any]:
    return won(**{"close_reason": "price", **overrides})


def context(*margins: float | None) -> BusinessContext:
    return BusinessContext(
        products=[
            Product(name=f"p{index}", gross_margin_pct=margin, evidence_ids=[])
            for index, margin in enumerate(margins)
        ]
    )


# ---------------------------------------------------------------------------
# the happy path, checked by hand
# ---------------------------------------------------------------------------


def test_one_segment_is_measured_from_the_rows() -> None:
    """Three wins at 30k/20k/10k and one loss: ACV 20,000 and a 75% close rate."""
    basis = crm.segments_frame(
        won=[won(deal_value=30_000), won(deal_value=20_000), won(deal_value=10_000)],
        lost=[lost()],
        business_context=context(80),
        product_context={crm.CONTRACT_TERM_KEY: 12},
    )
    assert basis.usable
    assert basis.gaps == []
    row = basis.frame.to_dict(orient="records")[0]
    assert row["segment"] == "Chemicals"
    assert row["acv_usd"] == 20_000.0
    assert row["deals"] == 3
    assert row["lost_deals"] == 1
    # 3 won of 4 opportunities.
    assert row["lead_to_won_pct"] == 75.0
    assert row["close_rate_basis"] == "segment"
    assert row["gross_margin_pct"] == 80.0
    assert row["contract_term_months"] == 12.0
    assert basis.close_rate_pct == 75.0
    assert basis.has_contract_term is True


def test_segments_split_on_industry_and_sort_by_deal_count() -> None:
    basis = crm.segments_frame(
        won=[
            won(industry="Manufacturing", deal_value=40_000),
            won(industry="Chemicals", deal_value=10_000),
            won(industry="Chemicals", deal_value=20_000),
        ],
        lost=[lost(industry="Chemicals"), lost(industry="Manufacturing")],
        business_context=context(50),
        product_context={},
    )
    rows = basis.frame.to_dict(orient="records")
    assert [row["segment"] for row in rows] == ["Chemicals", "Manufacturing"]
    assert rows[0]["acv_usd"] == 15_000.0
    # Chemicals: 2 won, 1 lost.
    assert rows[0]["lead_to_won_pct"] == pytest.approx(66.67)
    # Manufacturing: 1 won, 1 lost.
    assert rows[1]["lead_to_won_pct"] == 50.0


def test_a_segment_with_no_recorded_losses_borrows_the_account_rate() -> None:
    """The defect this guards: no losses is missing data, not a 100% close rate.

    Manufacturing won once and lost nothing. Its own rate would read 100%,
    which would roughly double the CPL ceiling it produces — on the strength
    of a CRM export that simply did not include its losses.
    """
    basis = crm.segments_frame(
        won=[won(industry="Chemicals"), won(industry="Manufacturing")],
        lost=[lost(industry="Chemicals"), lost(industry="Chemicals")],
        business_context=context(60),
        product_context={},
    )
    rows = {row["segment"]: row for row in basis.frame.to_dict(orient="records")}
    assert rows["Chemicals"]["close_rate_basis"] == "segment"
    assert rows["Chemicals"]["lead_to_won_pct"] == pytest.approx(33.33)
    assert rows["Manufacturing"]["close_rate_basis"] == "account"
    # Account-wide: 2 won of 4.
    assert rows["Manufacturing"]["lead_to_won_pct"] == 50.0
    assert any("account-wide lead-to-won rate" in note for note in basis.notes)


def test_rows_with_no_industry_become_one_named_segment() -> None:
    basis = crm.segments_frame(
        won=[won(industry=""), won(industry=None)],
        lost=[lost(industry="")],
        business_context=context(70),
        product_context={},
    )
    assert basis.frame.to_dict(orient="records")[0]["segment"] == crm.UNSEGMENTED


def test_zero_value_rows_are_skipped_and_said_so() -> None:
    basis = crm.segments_frame(
        won=[won(deal_value=0), won(deal_value=10_000)],
        lost=[lost()],
        business_context=context(50),
        product_context={},
    )
    row = basis.frame.to_dict(orient="records")[0]
    assert row["acv_usd"] == 10_000.0
    assert row["deals"] == 1, "the zero-value row must not dilute the ACV"


# ---------------------------------------------------------------------------
# what it refuses to invent
# ---------------------------------------------------------------------------


def test_no_margin_names_the_field_and_produces_nothing() -> None:
    basis = crm.segments_frame(
        won=[won()],
        lost=[lost()],
        business_context=context(None),
        product_context={crm.CONTRACT_TERM_KEY: 12},
    )
    assert not basis.usable
    assert any(gap.startswith("gross_margin_pct") for gap in basis.gaps)


def test_no_lost_rows_means_no_close_rate_rather_than_a_perfect_one() -> None:
    basis = crm.segments_frame(
        won=[won(), won()],
        lost=[],
        business_context=context(80),
        product_context={},
    )
    assert not basis.usable
    assert basis.close_rate_pct is None
    assert any(gap.startswith("crm_lost") for gap in basis.gaps)


def test_no_won_rows_names_that_too() -> None:
    basis = crm.segments_frame(
        won=[], lost=[lost()], business_context=context(80), product_context={}
    )
    assert not basis.usable
    assert any(gap.startswith("crm_won") for gap in basis.gaps)


def test_a_missing_contract_term_is_a_gap_not_a_default() -> None:
    basis = crm.segments_frame(
        won=[won()], lost=[lost()], business_context=context(80), product_context={}
    )
    assert basis.usable, "the ceiling does not need a contract term"
    assert basis.has_contract_term is False
    assert any(gap.startswith(crm.CONTRACT_TERM_KEY) for gap in basis.gaps)


@pytest.mark.parametrize("value", ["", None, 0, -3, True, "twelve"])
def test_an_unusable_contract_term_is_treated_as_absent(value: Any) -> None:
    basis = crm.segments_frame(
        won=[won()],
        lost=[lost()],
        business_context=context(80),
        product_context={crm.CONTRACT_TERM_KEY: value},
    )
    assert basis.has_contract_term is False


def test_a_zero_margin_is_ignored_rather_than_used() -> None:
    """Zero is the only unusable margin that can reach here.

    `Product.gross_margin_pct` is `ge=0, le=100`, so a negative or a 140 is
    rejected by the report contract long before this module sees it — but 0
    passes that validator and would make every ceiling zero, so it is filtered
    here and named as a gap instead.
    """
    basis = crm.segments_frame(
        won=[won()], lost=[lost()], business_context=context(0), product_context={}
    )
    assert not basis.usable
    assert any(gap.startswith("gross_margin_pct") for gap in basis.gaps)


def test_several_product_margins_are_averaged_and_the_averaging_is_stated() -> None:
    basis = crm.segments_frame(
        won=[won()],
        lost=[lost()],
        business_context=context(60, 80),
        product_context={},
    )
    assert basis.frame.to_dict(orient="records")[0]["gross_margin_pct"] == 70.0
    assert any("unweighted mean" in note for note in basis.notes)


# ---------------------------------------------------------------------------
# the payback join and the reasons table
# ---------------------------------------------------------------------------


def test_payback_frame_carries_the_ceiling_through_as_the_cac() -> None:
    basis = crm.segments_frame(
        won=[won()],
        lost=[lost()],
        business_context=context(80),
        product_context={crm.CONTRACT_TERM_KEY: 24},
    )
    joined = crm.payback_frame(
        basis, {"by_segment": [{"segment": "Chemicals", "max_cpa_won_usd": 1234.5}]}
    )
    row = joined.to_dict(orient="records")[0]
    assert row["max_cpa_won_usd"] == 1234.5
    assert row["contract_term_months"] == 24.0


def test_payback_frame_is_empty_without_a_contract_term() -> None:
    basis = crm.segments_frame(
        won=[won()], lost=[lost()], business_context=context(80), product_context={}
    )
    assert crm.payback_frame(basis, {"by_segment": []}).empty


def test_rejection_reasons_are_counted_for_the_prompt_only() -> None:
    reasons = crm.rejection_reasons(
        [lost(close_reason="price"), lost(close_reason="price"), lost(close_reason="timing")]
    )
    assert reasons == [{"reason": "price", "deals": 2}, {"reason": "timing", "deals": 1}]


def test_rejection_reasons_of_nothing_is_nothing() -> None:
    assert crm.rejection_reasons([]) == []
    assert crm.rejection_reasons([{"account_name": "x"}]) == []
