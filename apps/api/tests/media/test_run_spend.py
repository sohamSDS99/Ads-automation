"""The Creative Console's two spend meters (Stage 04 PRD §15.4 C, law 43; S4-P18).

Each meter is the three numbers the reserve script compares before it grants
a media job — spent, reserved, cap — so the header and the guard cannot
disagree about how close a run is to either cap.
"""

from __future__ import annotations

from decimal import Decimal

from agent.media.budget import BudgetCaps, BudgetState, SpendLine, run_spend, text_spend

CAPS = BudgetCaps(max_creative_cost_usd=Decimal("50"), max_media_cost_usd=Decimal("40"))


def test_text_is_the_ledger_less_committed_media_and_never_negative() -> None:
    assert text_spend(Decimal("7.50"), Decimal("2.50")) == Decimal("5.00")
    assert text_spend(None, Decimal("0")) == Decimal("0")
    assert text_spend(Decimal("1.00"), Decimal("2.50")) == Decimal("0")


def test_each_meter_is_spent_and_reserved_against_its_own_cap() -> None:
    spend = run_spend(
        caps=CAPS,
        run_cost_usd=Decimal("7.50"),
        media_committed_usd=Decimal("2.50"),
        state=BudgetState(spent_usd=Decimal("2.50"), reserved_usd=Decimal("1.20")),
    )
    assert spend.media == SpendLine(Decimal("2.50"), Decimal("1.20"), Decimal("40"))
    assert spend.total == SpendLine(Decimal("7.50"), Decimal("1.20"), Decimal("50"))


def test_a_flushed_redis_never_draws_media_below_what_is_committed() -> None:
    spend = run_spend(
        caps=CAPS,
        run_cost_usd=Decimal("7.50"),
        media_committed_usd=Decimal("2.50"),
        state=BudgetState(spent_usd=Decimal("0"), reserved_usd=Decimal("0")),
    )
    assert spend.media.spent_usd == Decimal("2.50")
    assert spend.total.spent_usd == Decimal("7.50")


def test_a_reconciled_job_redis_knows_before_the_row_is_counted() -> None:
    spend = run_spend(
        caps=CAPS,
        run_cost_usd=Decimal("7.50"),
        media_committed_usd=Decimal("2.50"),
        state=BudgetState(spent_usd=Decimal("3.10"), reserved_usd=Decimal("0")),
    )
    assert spend.media.spent_usd == Decimal("3.10")
    assert spend.total.spent_usd == Decimal("8.10")
