"""4.3.2 `offer_assets` — the parts that decide (PRD §11 4.3.2, law 35).

The node's database and model work is proved in
`tests/integration/test_s4p8_extras.py`. Here:

* **the model writes header text only** — its draft schema has no number, no
  date and no free field, and a digit in any string it writes fails it;
* which offers become promotions and which price items is decided in code,
  from the records, within the spec sheet's counts;
* `not_required` exactly when no offer in the snapshot is fresh and live.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from agent.creative import offers
from agent.nodes.creative.n4_3_2_offer_assets import (
    PRICE_NEEDS,
    PROMOTION_NEEDS,
    draft_model,
    not_required_reason,
    plan_campaign,
)
from agent.schemas.guardrails import SURFACE_ASSET_TYPES, AssetSpec, OfferRecord

NOW = datetime(2026, 9, 25, 12, tzinfo=UTC)
TYPES = ("SERVICES", "SERVICE_TIERS")


def _record(sku: str, **overrides: Any) -> OfferRecord:
    fields: dict[str, Any] = {
        "sku": sku,
        "product_set": "plans",
        "list_price": 99.0,
        "current_price": 99.0,
        "currency": "USD",
        "market": "US",
        "observed_at": NOW,
    }
    fields.update(overrides)
    return OfferRecord.model_validate(fields)


def _spec(**fields: Any) -> AssetSpec:
    return AssetSpec.model_validate({"source": "unverified", "reviewed_at": "2026-09-25", **fields})


PROMO = _spec(max_chars=20, max_count=2)
PRICE = _spec(max_chars=25, min_count=3, max_count=4)


def test_promotion_and_price_are_linted_as_their_own_asset_types() -> None:
    assert SURFACE_ASSET_TYPES["promotion"] == "promotion"
    assert SURFACE_ASSET_TYPES["price"] == "price"
    assert set(PROMOTION_NEEDS) == {"max_chars", "max_count"}
    assert set(PRICE_NEEDS) == {"max_chars", "min_count", "max_count"}


# ---------------------------------------------------------------------------
# the draft: header text only
# ---------------------------------------------------------------------------


def _strings(schema: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("type") in ("number", "integer"):
                raise AssertionError(f"a number the model could write: {node}")
            if node.get("type") == "string" and "enum" not in node and "const" not in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return found


def _model() -> Any:
    return draft_model(["offer_1", "offer_2"], ["offer_1", "offer_2", "offer_3"], price_types=TYPES)


def test_the_draft_has_no_number_and_every_string_it_writes_refuses_a_digit() -> None:
    strings = _strings(_model().model_json_schema())
    # Every promotion shares one text definition and every item one header and
    # description: three free strings in all, each refusing a digit.
    assert len(strings) == 3
    assert all(node.get("pattern") == r"^\D*$" for node in strings)


def _answer(**overrides: Any) -> dict[str, Any]:
    return {
        "promotions": {"offer_1": {"text": "SDS software"}, "offer_2": {"text": "Team plan"}},
        "price": {
            "type": "SERVICE_TIERS",
            "items": {
                key: {"header": "Plan", "description": "For EHS teams"}
                for key in ("offer_1", "offer_2", "offer_3")
            },
        },
        **overrides,
    }


def test_header_text_is_accepted() -> None:
    _model().model_validate(_answer())


@pytest.mark.parametrize(
    "overrides",
    [
        {"promotions": {"offer_1": {"text": "20% off SDS"}, "offer_2": {"text": "Team plan"}}},
        {
            "price": {
                "type": "SERVICES",
                "items": {
                    "offer_1": {"header": "Plan", "description": "From 99 dollars"},
                    "offer_2": {"header": "Plan", "description": "EHS"},
                    "offer_3": {"header": "Plan", "description": "EHS"},
                },
            }
        },
    ],
)
def test_a_model_written_digit_fails_the_draft(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _model().model_validate(_answer(**overrides))


def test_the_model_cannot_add_a_figure_of_its_own() -> None:
    answer = _answer()
    answer["promotions"]["offer_1"]["percent_off"] = "20"
    with pytest.raises(ValidationError):
        _model().model_validate(answer)


def test_a_price_type_google_does_not_list_is_refused() -> None:
    answer = _answer()
    answer["price"]["type"] = "BEST_SELLERS"
    with pytest.raises(ValidationError):
        _model().model_validate(answer)


# ---------------------------------------------------------------------------
# which offers, decided in code
# ---------------------------------------------------------------------------


def test_discounted_offers_become_promotions_within_the_spec_count() -> None:
    records = [
        _record("C", reference_price=129.0, current_price=99.0),
        _record("A", list_price=59.0, current_price=49.0),
        _record("B"),  # no saving
        _record("D", reference_price=20.0, current_price=10.0),
    ]
    plan = plan_campaign(records, promotion=PROMO, price=PRICE)
    assert [record.sku for record in plan.promotions] == ["A", "C"]  # sorted, capped at 2


def test_price_items_are_one_currency_within_the_spec_count() -> None:
    records = [
        _record("A"),
        _record("B"),
        _record("C"),
        _record("D"),
        _record("E"),
        _record("G1", currency="GBP"),
    ]
    plan = plan_campaign(records, promotion=None, price=PRICE)
    assert [record.sku for record in plan.price_items] == ["A", "B", "C", "D"]
    assert plan.promotions == []


def test_too_few_offers_for_a_price_asset_is_a_gap_not_a_guess() -> None:
    plan = plan_campaign([_record("A"), _record("B")], promotion=None, price=PRICE)
    assert plan.price_items == []
    assert [gap for gap, _ in plan.gaps] == ["too_few_offers"]


def test_no_saving_anywhere_is_a_gap() -> None:
    plan = plan_campaign([_record("A")], promotion=PROMO, price=None)
    assert [gap for gap, _ in plan.gaps] == ["no_discount"]


def test_an_offer_whose_deadline_has_no_timezone_is_never_bound() -> None:
    naive_deadline = _record(
        "A", reference_price=129.0, current_price=99.0, ends_at=datetime(2026, 10, 1)
    )
    plan = plan_campaign([naive_deadline], promotion=PROMO, price=None)
    assert plan.promotions == []
    assert [gap for gap, _ in plan.gaps] == ["unbindable_offer"]


# ---------------------------------------------------------------------------
# not_required
# ---------------------------------------------------------------------------


def test_stale_offers_are_not_required() -> None:
    stale = offers.usable(
        [_record("A", observed_at=NOW - timedelta(days=30))], now=NOW, max_age_days=7
    )
    reason = not_required_reason(stale, max_age_days=7)
    assert reason is not None and "never guessed" in reason and "1" in reason


def test_no_offers_at_all_are_not_required() -> None:
    assert not_required_reason(offers.Usable(), max_age_days=7) is not None


def test_a_usable_offer_is_required() -> None:
    usable = offers.usable([_record("A")], now=NOW, max_age_days=7)
    assert not_required_reason(usable, max_age_days=7) is None
