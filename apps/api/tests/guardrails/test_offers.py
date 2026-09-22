"""Offer integrity (S3-P1 deliverable 5, PRD §9.4).

`test_the_locale_trap` is the one that would have shipped a confidently wrong
blocking verdict: `1.299,00` and `1,299.00` are the same amount written by a
German and an American, and reading the first as `1.299` blocks a correct ad
with arithmetic that looks right in the finding message.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.guardrails.helpers import GOOGLE, NOW, context, target

from agent.guardrails.matchers.offers import (
    amount_off,
    countdown,
    evaluate_offer,
    free_trial,
    from_price,
    parse_amount,
    percent_off,
    prepare_offer,
)
from agent.schemas.guardrails import OfferRecord, Rule


def offer(**overrides: object) -> OfferRecord:
    payload: dict[str, object] = {
        "sku": "sds-pro",
        "product_set": "software",
        "list_price": 99.0,
        "current_price": 49.0,
        "currency": "EUR",
        "market": "DE",
    }
    payload.update(overrides)
    return OfferRecord.model_validate(payload)


def lint(rule: Rule, text: str, offers: tuple[OfferRecord, ...], *, now: datetime = NOW) -> list:
    item = target(text)
    ctx = context([item], offers=offers, now=now)
    return evaluate_offer(rule, prepare_offer(rule.matcher), item, ctx)


# --- number parsing ---------------------------------------------------------


@pytest.mark.parametrize(
    ("written", "expected"),
    [
        ("49", 49.0),
        ("49.99", 49.99),
        ("49,99", 49.99),
        ("1,299.00", 1299.0),
        ("1.299,00", 1299.0),
        ("1 299,00", 1299.0),
        ("1,299", 1299.0),
        ("1.299", 1299.0),
        ("10,000", 10000.0),
    ],
)
def test_the_locale_trap(written: str, expected: float) -> None:
    """A lone separator with three digits after it is grouping, not a decimal
    point. Reading `1,299` as `1.299` blocks a correct ad."""
    assert parse_amount(written) == pytest.approx(expected)


# --- from-pricing -----------------------------------------------------------


def test_a_from_price_matching_the_live_floor_is_silent() -> None:
    assert lint(from_price(authority=GOOGLE), "SDS software from €49", (offer(),)) == []


def test_a_stale_from_price_is_blocking_and_names_both_numbers() -> None:
    findings = lint(from_price(authority=GOOGLE), "from €39", (offer(current_price=49.0),))
    assert len(findings) == 1
    assert findings[0].severity == "blocking"
    assert "39" in findings[0].message and "49" in findings[0].message


def test_the_floor_is_the_minimum_across_the_product_set() -> None:
    offers = (offer(sku="a", current_price=79.0), offer(sku="b", current_price=29.0))
    assert lint(from_price(authority=GOOGLE), "from €29", offers) == []
    assert lint(from_price(authority=GOOGLE), "from €79", offers) != []


def test_tolerance_is_respected() -> None:
    rule = from_price(authority=GOOGLE, tolerance=1.0)
    assert lint(rule, "from €49.50", (offer(current_price=49.0),)) == []


def test_copy_making_no_offer_claim_is_not_checked() -> None:
    assert lint(from_price(authority=GOOGLE), "Manage your safety data sheets", (offer(),)) == []


# --- law 31 -----------------------------------------------------------------


def test_no_offer_data_is_indeterminate_never_pass() -> None:
    """The copy makes a priced promise and we hold nothing to check it against."""
    findings = lint(from_price(authority=GOOGLE), "from €49", ())
    assert len(findings) == 1
    assert findings[0].indeterminate is True
    assert findings[0].severity == "blocking"


def test_offers_for_another_market_do_not_count_as_data() -> None:
    findings = lint(from_price(authority=GOOGLE), "from €49", (offer(market="FR"),))
    assert findings[0].indeterminate is True


def test_an_offer_outside_its_effective_window_is_not_live() -> None:
    expired = offer(effective_to=datetime(2026, 1, 1, tzinfo=UTC))
    future = offer(effective_from=datetime(2027, 1, 1, tzinfo=UTC))
    assert lint(from_price(authority=GOOGLE), "from €49", (expired,))[0].indeterminate
    assert lint(from_price(authority=GOOGLE), "from €49", (future,))[0].indeterminate


def test_a_product_set_scopes_which_offers_apply() -> None:
    rule = from_price(authority=GOOGLE, product_set="hardware")
    assert lint(rule, "from €49", (offer(product_set="software"),))[0].indeterminate


# --- discounts --------------------------------------------------------------


def test_a_percentage_discount_needs_a_documented_reference_price() -> None:
    findings = lint(percent_off(authority=GOOGLE), "50% off today", (offer(),))
    assert findings and "reference_price" in findings[0].message


def test_a_percentage_discount_within_the_documented_one_is_silent() -> None:
    documented = offer(reference_price=100.0, current_price=50.0)
    assert lint(percent_off(authority=GOOGLE), "50% off today", (documented,)) == []
    assert lint(percent_off(authority=GOOGLE), "40% off today", (documented,)) == []


def test_an_overclaimed_percentage_is_blocking() -> None:
    documented = offer(reference_price=100.0, current_price=80.0)
    findings = lint(percent_off(authority=GOOGLE), "60% off", (documented,))
    assert findings and "20.0%" in findings[0].message


def test_an_overclaimed_amount_is_blocking() -> None:
    findings = lint(amount_off(authority=GOOGLE), "Save €80", (offer(reference_price=99.0),))
    assert findings and "50" in findings[0].message


# --- countdowns -------------------------------------------------------------


def test_a_countdown_with_no_end_date_is_blocking() -> None:
    findings = lint(countdown(authority=GOOGLE), "Offer ends soon", (offer(),))
    assert findings and "no live offer carries an end date" in findings[0].message.lower()


def test_a_countdown_whose_deadline_has_no_timezone_is_blocking() -> None:
    naive = offer(ends_at=datetime(2026, 12, 1))
    findings = lint(countdown(authority=GOOGLE), "Offer ends soon", (naive,))
    assert findings and "timezone" in findings[0].message


def test_a_real_dated_countdown_is_silent() -> None:
    live = offer(ends_at=datetime(2026, 12, 1, tzinfo=UTC))
    assert lint(countdown(authority=GOOGLE), "Offer ends soon", (live,)) == []


def test_a_countdown_that_already_passed_is_blocking() -> None:
    gone = offer(ends_at=datetime(2026, 1, 1, tzinfo=UTC))
    findings = lint(countdown(authority=GOOGLE), "Offer ends soon", (gone,))
    assert findings and "already ended" in findings[0].message


def test_an_extended_deadline_is_blocking_because_it_was_never_a_deadline() -> None:
    moved = offer(ends_at=datetime(2026, 12, 1, tzinfo=UTC), extensions=1)
    findings = lint(countdown(authority=GOOGLE), "Last chance", (moved,))
    assert findings and "extended" in findings[0].message


# --- the thin two -----------------------------------------------------------


def test_a_trial_promise_with_offer_data_defers_to_the_claims_register() -> None:
    """There is no trial field on OfferRecord to check against, and inventing
    one here would be this module asserting something nobody recorded."""
    assert lint(free_trial(authority=GOOGLE), "Start your free trial", (offer(),)) == []


def test_a_trial_promise_with_no_offer_data_at_all_is_indeterminate() -> None:
    assert lint(free_trial(authority=GOOGLE), "Start your free trial", ())[0].indeterminate


# --- staleness (Q4) ---------------------------------------------------------


def test_a_finding_computed_from_stale_offer_data_says_so() -> None:
    old = offer(current_price=49.0, observed_at=datetime(2026, 1, 1, tzinfo=UTC))
    findings = lint(from_price(authority=GOOGLE), "from €39", (old,))
    assert len(findings) == 2
    assert any("older than 30 days" in item.message for item in findings)


def test_fresh_offer_data_adds_no_staleness_warning() -> None:
    fresh = offer(current_price=49.0, observed_at=datetime(2026, 9, 20, tzinfo=UTC))
    assert len(lint(from_price(authority=GOOGLE), "from €39", (fresh,))) == 1
