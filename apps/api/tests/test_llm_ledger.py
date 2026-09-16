"""Pricing, cost arithmetic and the budget cap."""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import httpx
import pytest

from agent.llm.ledger import (
    BudgetExceeded,
    ModelCatalogue,
    ModelPrice,
    PricingUnavailable,
    RunLedger,
    Usage,
    quantize_money,
)
from tests.openrouter_fake import DEFAULT_PRICES, FakeOpenRouter

BASE_URL = "https://openrouter.test/api/v1"


async def test_prices_come_from_the_catalogue_not_from_a_constant() -> None:
    fake = FakeOpenRouter()
    async with fake.client() as client:
        catalogue = ModelCatalogue(client, base_url=BASE_URL)
        price = await catalogue.price_of("anthropic/claude-opus-4.6")

    assert price.prompt == Decimal("0.00001")
    assert price.completion == Decimal("0.00003")


async def test_the_catalogue_is_fetched_once_per_ttl() -> None:
    fake = FakeOpenRouter()
    async with fake.client() as client:
        catalogue = ModelCatalogue(client, base_url=BASE_URL)
        await catalogue.price_of("openai/gpt-5.2")
        await catalogue.price_of("google/gemini-2.5-flash")

    assert fake.catalogue_calls == 1


async def test_an_expired_catalogue_is_refetched() -> None:
    fake = FakeOpenRouter()
    async with fake.client() as client:
        catalogue = ModelCatalogue(client, base_url=BASE_URL, ttl=timedelta(seconds=-1))
        await catalogue.price_of("openai/gpt-5.2")
        await catalogue.price_of("openai/gpt-5.2")

    assert fake.catalogue_calls == 2


async def test_a_stale_catalogue_beats_an_aborted_run() -> None:
    fake = FakeOpenRouter()
    async with fake.client() as client:
        catalogue = ModelCatalogue(client, base_url=BASE_URL, ttl=timedelta(seconds=-1))
        await catalogue.price_of("openai/gpt-5.2")
        fake.prices = {}

        def broken(_request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("catalogue down")

        client._transport = httpx.MockTransport(broken)  # noqa: SLF001
        price = await catalogue.price_of("openai/gpt-5.2")

    assert price.prompt == Decimal(DEFAULT_PRICES["openai/gpt-5.2"]["prompt"])


async def test_no_catalogue_at_all_is_an_error_not_a_zero() -> None:
    def broken(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("catalogue down")

    async with httpx.AsyncClient(transport=httpx.MockTransport(broken)) as client:
        catalogue = ModelCatalogue(client, base_url=BASE_URL)
        with pytest.raises(PricingUnavailable):
            await catalogue.price_of("openai/gpt-5.2")


async def test_an_unlisted_model_prices_at_zero_rather_than_failing() -> None:
    fake = FakeOpenRouter()
    async with fake.client() as client:
        catalogue = ModelCatalogue(client, base_url=BASE_URL)
        price = await catalogue.price_of("vendor/never-heard-of-it")

    assert price.cost(Usage(1000, 1000)) == Decimal(0)


def test_cost_is_tokens_times_price_in_decimal() -> None:
    price = ModelPrice(model="m", prompt=Decimal("0.000003"), completion=Decimal("0.000015"))
    cost = price.cost(Usage(prompt_tokens=1_000_000, completion_tokens=200_000))
    assert cost == Decimal("6.0")


def test_money_quantizes_to_the_four_places_the_column_stores() -> None:
    assert quantize_money(Decimal("0.00005")) == Decimal("0.0001")
    assert quantize_money(Decimal("1.23456")) == Decimal("1.2346")


def test_the_ledger_sums_before_it_rounds() -> None:
    """Ten sub-cent calls are a cent, not nothing."""
    ledger = RunLedger(cap_usd=Decimal("15"))
    for _ in range(10):
        ledger.record(usage=Usage(1, 1), cost=Decimal("0.00009"))

    assert ledger.spent_usd == Decimal("0.00090")
    assert quantize_money(ledger.spent_usd) == Decimal("0.0009")
    assert ledger.token_in == 10


def test_the_budget_cap_is_enforced_between_nodes() -> None:
    ledger = RunLedger(cap_usd=Decimal("0.50"))
    ledger.record(usage=Usage(10, 10), cost=Decimal("0.49"))
    ledger.enforce()  # still inside the cap

    ledger.record(usage=Usage(10, 10), cost=Decimal("0.02"))
    with pytest.raises(BudgetExceeded) as raised:
        ledger.enforce()

    assert raised.value.spent == Decimal("0.51")
    assert raised.value.cap == Decimal("0.50")


def test_spending_exactly_the_cap_is_allowed() -> None:
    ledger = RunLedger(cap_usd=Decimal("1.00"))
    ledger.record(usage=Usage(1, 1), cost=Decimal("1.00"))
    ledger.enforce()
