"""`creative/offers.py` — OfferBinding resolution against the snapshot (PRD §12.2, law 35).

* an offer record's identity survives a price change, so a binding can be
  re-resolved against the live row at release;
* only fresh (≤ `offer_max_age_days`) and live offers are usable — a stale one
  is skipped, never guessed, and an offer with no observation date is stale;
* every figure is rendered in code from the record, exactly as Stage 03's
  offer matchers derive it, and never overstated;
* a binding the promotion and price contracts accept is what `bind` produces.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from agent.creative import offers
from agent.creative.offers import OfferBindingError
from agent.schemas.extras import PriceItem, Promotion
from agent.schemas.guardrails import OfferRecord

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
LINT = {"verdict": "pass", "ruleset_version": "1.0+aa", "rule_ids": []}


def _record(**overrides: Any) -> OfferRecord:
    fields: dict[str, Any] = {
        "sku": "SDS-PRO",
        "product_set": "plans",
        "list_price": 129.0,
        "current_price": 99.0,
        "reference_price": 129.0,
        "currency": "USD",
        "market": "US",
        "effective_from": NOW - timedelta(days=1),
        "ends_at": NOW + timedelta(days=18),
        "observed_at": NOW - timedelta(hours=2),
    }
    fields.update(overrides)
    return OfferRecord.model_validate(fields)


# ---------------------------------------------------------------------------
# identity
# ---------------------------------------------------------------------------


def test_a_record_keeps_its_id_when_its_price_changes() -> None:
    before = _record()
    after = _record(current_price=89.0, observed_at=NOW)
    assert offers.record_id(before) == offers.record_id(after)


@pytest.mark.parametrize("change", [{"sku": "SDS-TEAM"}, {"market": "GB"}, {"product_set": "x"}])
def test_a_different_offer_has_a_different_id(change: dict[str, Any]) -> None:
    assert offers.record_id(_record()) != offers.record_id(_record(**change))


def test_market_case_is_not_identity() -> None:
    assert offers.record_id(_record(market="us")) == offers.record_id(_record(market="US"))


# ---------------------------------------------------------------------------
# fresh and live
# ---------------------------------------------------------------------------


def test_a_fresh_live_offer_is_usable() -> None:
    found = offers.usable([_record()], now=NOW, max_age_days=7)
    assert found.usable == [_record()]
    assert found.stale == []


def test_an_offer_older_than_the_window_is_stale() -> None:
    old = _record(observed_at=NOW - timedelta(days=7, seconds=1))
    found = offers.usable([old], now=NOW, max_age_days=7)
    assert found.usable == []
    assert found.stale == [old]


def test_an_offer_on_the_edge_of_the_window_is_fresh() -> None:
    edge = _record(observed_at=NOW - timedelta(days=7))
    assert offers.usable([edge], now=NOW, max_age_days=7).usable == [edge]


def test_an_offer_nobody_dated_is_stale_never_assumed_fresh() -> None:
    undated = _record(observed_at=None)
    found = offers.usable([undated], now=NOW, max_age_days=7)
    assert found.usable == []
    assert found.stale == [undated]


@pytest.mark.parametrize(
    "change",
    [
        {"effective_from": NOW + timedelta(days=1)},
        {"effective_to": NOW},
        {"ends_at": NOW - timedelta(minutes=1)},
    ],
)
def test_an_offer_not_live_at_the_runs_start_is_not_usable(change: dict[str, Any]) -> None:
    found = offers.usable([_record(**change)], now=NOW, max_age_days=7)
    assert found.usable == []
    assert len(found.not_live) == 1


def test_the_latest_observation_of_one_offer_wins() -> None:
    older = _record(current_price=109.0, observed_at=NOW - timedelta(days=2))
    newer = _record(current_price=99.0, observed_at=NOW - timedelta(hours=1))
    assert offers.usable([newer, older], now=NOW, max_age_days=7).usable == [newer]


def test_markets_match_case_insensitively_and_an_unknown_market_takes_all() -> None:
    us, gb = _record(), _record(sku="SDS-GB", market="GB", currency="GBP")
    assert offers.in_market([us, gb], "us") == [us]
    assert offers.in_market([us, gb], "*") == [us, gb]


# ---------------------------------------------------------------------------
# what a promotion and a price bind
# ---------------------------------------------------------------------------


def test_a_documented_reference_price_is_a_percent_off() -> None:
    assert offers.promotion_fields(_record()) == {
        "percent_off": "percent_off",
        "currency": "currency",
        "start": "effective_from",
        "end": "ends_at",
    }


def test_a_saving_without_a_reference_price_is_money_off() -> None:
    # Stage 03's percent_off construction needs a documented reference price.
    fields = offers.promotion_fields(
        _record(reference_price=None, list_price=59.0, current_price=49.0)
    )
    assert fields is not None and fields["money_off"] == "money_off"
    assert "percent_off" not in fields


def test_no_saving_is_no_promotion() -> None:
    assert offers.promotion_fields(_record(reference_price=None, list_price=99.0)) is None


def test_the_end_falls_back_to_the_effective_window() -> None:
    fields = offers.promotion_fields(_record(ends_at=None, effective_to=NOW + timedelta(days=3)))
    assert fields is not None and fields["end"] == "effective_to"


def test_an_undated_offer_binds_no_dates() -> None:
    fields = offers.promotion_fields(_record(effective_from=None, ends_at=None))
    assert fields is not None and {"start", "end"}.isdisjoint(fields)


# ---------------------------------------------------------------------------
# rendering — in code, from the record
# ---------------------------------------------------------------------------


def test_every_figure_is_rendered_from_the_record() -> None:
    record = _record()
    resolved = offers.resolve(
        record,
        {
            "percent_off": "percent_off",
            "currency": "currency",
            "start": "effective_from",
            "end": "ends_at",
            "price": "current_price",
        },
    )
    assert resolved == {
        # 100 × 30/129 = 23.26: floored, so the ad never claims more than the offer gives.
        "percent_off": "23",
        "currency": "USD",
        "start": (NOW - timedelta(days=1)).isoformat(),
        "end": (NOW + timedelta(days=18)).isoformat(),
        "price": "99.00",
    }


def test_money_is_exact_decimal() -> None:
    record = _record(reference_price=None, list_price=59.99, current_price=49.98)
    assert offers.resolve(record, {"money_off": "money_off"}) == {"money_off": "10.01"}


@pytest.mark.parametrize(
    ("record", "fields"),
    [
        (_record(), {"price": "the_price_the_model_liked"}),
        (_record(reference_price=None), {"percent_off": "percent_off"}),
        (_record(ends_at=None), {"end": "ends_at"}),
        (_record(ends_at=datetime(2026, 10, 1)), {"end": "ends_at"}),  # no timezone
        (_record(reference_price=None, list_price=99.0), {"money_off": "money_off"}),
    ],
)
def test_a_field_the_record_cannot_honestly_render_refuses(
    record: OfferRecord, fields: dict[str, str]
) -> None:
    with pytest.raises(OfferBindingError):
        offers.resolve(record, fields)


def test_a_bound_promotion_passes_the_promotion_contract() -> None:
    record = _record()
    fields = offers.promotion_fields(record)
    assert fields is not None
    binding = offers.bind(record, fields)
    assert binding.offer_record_id == offers.record_id(record)
    assert binding.sku_or_set == "SDS-PRO"
    Promotion.model_validate(
        {
            "asset_id": str(uuid.uuid4()),
            "campaign_ref": "c-sds-us",
            "discount_kind": "percent_off",
            "bound": {"percent_off": binding.resolved["percent_off"], "currency": "USD"},
            "start": binding.resolved["start"],
            "end": binding.resolved["end"],
            "final_url": "https://sdsmanager.com/sds",
            "text": "SDS management software",
            "offer_binding": binding.model_dump(mode="json"),
            "lint": LINT,
        }
    )


def test_a_bound_price_passes_the_price_contract() -> None:
    record = _record()
    binding = offers.bind(record, offers.price_fields(record))
    PriceItem.model_validate(
        {
            "asset_id": str(uuid.uuid4()),
            "header": "Professional plan",
            "description": "For growing EHS teams",
            "bound": {"price": binding.resolved["price"], "currency": binding.resolved["currency"]},
            "final_url": "https://sdsmanager.com/sds",
            "offer_binding": binding.model_dump(mode="json"),
            "lint": LINT,
        }
    )


def test_an_undated_and_a_dated_observation_of_one_offer_compare() -> None:
    undated = _record(current_price=109.0, observed_at=None)
    dated = _record(current_price=99.0)
    assert offers.usable([dated, undated], now=NOW, max_age_days=7).usable == [dated]
    assert offers.usable([undated, dated], now=NOW, max_age_days=7).usable == [dated]


def test_an_observation_without_a_timezone_is_aged_as_utc() -> None:
    # `csv_ingest` dates carry no zone; a seven-day window is not moved by one.
    naive = _record(observed_at=datetime(2026, 9, 24, 12))
    assert offers.usable([naive], now=NOW, max_age_days=7).usable == [naive]
    old = _record(observed_at=datetime(2026, 9, 1))
    assert offers.usable([old], now=NOW, max_age_days=7).stale == [old]


def test_a_window_without_a_timezone_is_read_as_utc() -> None:
    ended = _record(effective_from=datetime(2026, 9, 1), effective_to=datetime(2026, 9, 20))
    assert len(offers.usable([ended], now=NOW, max_age_days=7).not_live) == 1
