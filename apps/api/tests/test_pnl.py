"""The search-term P&L arithmetic (PRD §18 law 3).

Every number node 1.2.2 emits is produced by `pnl.compute`, so these are the
assertions that stand between a report and a wrong CPA. The fixtures are small
enough to check by hand, which is the point — a test that needs a spreadsheet to
verify is not a check on the arithmetic, it is a second implementation of it.
"""

from __future__ import annotations

import uuid
from typing import Any

from agent.nodes import pnl


def rows() -> tuple[list[dict[str, Any]], list[uuid.UUID]]:
    """Three terms across four monthly rows. Totals: £250 spent, £100 of it wasted."""
    data: list[dict[str, Any]] = [
        {
            "search_term": "sds software",
            "campaign": "Brand",
            "month": "2025-01",
            "cost": 100.0,
            "conversions": 2,
            "conversion_value": 600.0,
            "clicks": 40,
            "impressions": 800,
        },
        {
            # Same term, different month and different capitalisation. Google
            # reports both; they are one term.
            "search_term": "SDS Software",
            "campaign": "Brand",
            "month": "2025-02",
            "cost": 50.0,
            "conversions": 1,
            "conversion_value": 300.0,
            "clicks": 20,
            "impressions": 400,
        },
        {
            "search_term": "free sds template",
            "campaign": "Generic",
            "month": "2025-01",
            "cost": 80.0,
            "conversions": 0,
            "conversion_value": 0.0,
            "clicks": 60,
            "impressions": 2000,
        },
        {
            "search_term": "sds jobs",
            "campaign": "Generic",
            "month": "2025-01",
            "cost": 20.0,
            "conversions": 0,
            "conversion_value": 0.0,
            "clicks": 10,
            "impressions": 500,
        },
    ]
    return data, [uuid.uuid4() for _ in data]


def test_a_term_is_aggregated_across_months_and_case() -> None:
    data, ids = rows()
    table = pnl.compute(data, ids)

    assert [item.term for item in table.profitable] == ["sds software"]
    winner = table.profitable[0]
    assert winner.cost == 150.0
    assert winner.conversions == 3.0
    assert winner.conversion_value == 900.0
    assert winner.clicks == 60
    assert winner.impressions == 1200
    assert winner.cpa == 50.0
    assert winner.roas == 6.0
    assert winner.campaigns == ("Brand",)
    # Both source rows are cited, because both are behind the aggregate.
    assert set(winner.evidence_ids) == {ids[0], ids[1]}


def test_wasteful_means_spent_money_and_converted_nobody() -> None:
    data, ids = rows()
    table = pnl.compute(data, ids)

    assert [item.term for item in table.wasteful] == ["free sds template", "sds jobs"]
    assert [item.cost for item in table.wasteful] == [80.0, 20.0]
    assert all(item.conversions == 0 for item in table.wasteful)
    assert table.wasteful[0].cpa is None, "no conversions means no CPA, not a zero"


def test_the_totals_are_of_spend_not_of_terms() -> None:
    data, ids = rows()
    totals = pnl.compute(data, ids).totals

    assert totals.terms == 3
    assert totals.cost == 250.0
    assert totals.conversions == 3.0
    assert totals.conversion_value == 900.0
    assert totals.wasted_spend == 100.0
    # 100 of 250 spent, not 2 of 3 terms — the sentence a marketer acts on.
    assert totals.waste_pct == 40.0
    assert totals.profitable_terms == 1
    assert totals.wasteful_terms == 2
    assert totals.blended_cpa == 83.33
    assert totals.blended_roas == 3.6


def test_a_zero_cost_term_is_neither_profitable_nor_wasteful() -> None:
    """An impression-only term bought nothing because it spent nothing."""
    data = [{"search_term": "ghost", "cost": 0.0, "conversions": 0, "impressions": 30, "clicks": 0}]
    table = pnl.compute(data, [uuid.uuid4()])
    assert table.profitable == []
    assert table.wasteful == []
    assert table.totals.terms == 1
    assert table.totals.waste_pct == 0.0


def test_division_by_zero_produces_null_not_infinity() -> None:
    """`inf` serialises to invalid JSON and would fail at the API boundary."""
    data = [{"search_term": "a", "cost": 0.0, "conversions": 0.0, "conversion_value": 0.0}]
    table = pnl.compute(data, [uuid.uuid4()])
    assert table.totals.blended_cpa is None
    assert table.totals.blended_roas is None


def test_missing_columns_are_treated_as_zero_rather_than_dropping_the_row() -> None:
    """A partial Google Ads pull still produces a P&L (PRD §16, degraded sources)."""
    data = [{"search_term": "bare", "cost": 12.0}]
    table = pnl.compute(data, [uuid.uuid4()])
    assert [item.term for item in table.wasteful] == ["bare"]
    assert table.wasteful[0].clicks == 0


def test_rows_without_a_search_term_are_not_a_term() -> None:
    data = [
        {"search_term": "", "cost": 5.0},
        {"search_term": "   ", "cost": 5.0},
        {"search_term": "real", "cost": 5.0},
    ]
    table = pnl.compute(data, [uuid.uuid4() for _ in data])
    assert [item.term for item in table.wasteful] == ["real"]


def test_no_evidence_produces_an_empty_table_not_an_error() -> None:
    table = pnl.compute([], [])
    assert table.is_empty
    assert table.totals.terms == 0


def test_a_payload_without_the_search_term_column_at_all_is_empty() -> None:
    table = pnl.compute([{"campaign": "Brand", "cost": 10.0}], [uuid.uuid4()])
    assert table.is_empty


def test_truncation_is_reported_rather_than_silent() -> None:
    """A capped list that says nothing reads as complete coverage."""
    data = [
        {"search_term": f"waste {index}", "cost": float(index + 1), "conversions": 0}
        for index in range(pnl.TOP_N + 5)
    ]
    table = pnl.compute(data, [uuid.uuid4() for _ in data])

    assert len(table.wasteful) == pnl.TOP_N
    assert table.truncated == {"wasteful_terms": 5}
    # The totals still count every term the cut dropped.
    assert table.totals.wasteful_terms == pnl.TOP_N + 5


def test_citations_per_term_are_bounded() -> None:
    """A term seen in 24 monthly rows does not need 24 citations to be checkable."""
    data = [
        {"search_term": "same", "month": f"2025-{index:02d}", "cost": 1.0, "conversions": 0}
        for index in range(1, 13)
    ] * 2
    ids = [uuid.uuid4() for _ in data]
    table = pnl.compute(data, ids)
    assert len(table.wasteful[0].evidence_ids) == pnl.MAX_IDS_PER_TERM


def test_unparseable_numbers_do_not_take_the_row_with_them() -> None:
    data = [{"search_term": "odd", "cost": "not a number", "conversions": 0}]
    table = pnl.compute(data, [uuid.uuid4()])
    assert table.totals.cost == 0.0
    assert table.totals.terms == 1
