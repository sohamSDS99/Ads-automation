"""Reading OpenRouter's account and catalogue endpoints.

Every case here is a real shape the API returns, including the ones that make a
key look fine when it is not — `/models` answers an unauthenticated caller, so
validating a key against it would call any string valid.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx
import pytest

from agent.llm.openrouter import OpenRouterError, list_models, probe_key

BASE = "https://openrouter.ai/api/v1"


def transport(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def test_probe_reads_the_balance() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/auth/key")
        assert request.headers["authorization"] == "Bearer sk-or-test"
        return httpx.Response(
            200,
            json={
                "data": {
                    "label": "ads-research",
                    "usage": 4.25,
                    "limit": 50,
                    "limit_remaining": 45.75,
                    "is_free_tier": False,
                }
            },
        )

    async with transport(handler) as client:
        status = await probe_key(client, base_url=BASE, api_key="sk-or-test")

    assert status.label == "ads-research"
    assert status.remaining_usd == Decimal("45.75")
    assert "45.75" in status.detail


async def test_a_key_with_no_limit_reports_usage_instead_of_a_balance() -> None:
    """Pay-as-you-go keys return a null limit; the screen must not print "$None"."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"label": "pay-go", "usage": 12.5, "limit": None}})

    async with transport(handler) as client:
        status = await probe_key(client, base_url=BASE, api_key="sk-or-test")

    assert status.remaining_usd is None
    assert "12.50 used" in status.detail


async def test_a_rejected_key_says_so() -> None:
    async with transport(lambda _: httpx.Response(401, json={"error": "no"})) as client:
        with pytest.raises(OpenRouterError, match="rejected this key"):
            await probe_key(client, base_url=BASE, api_key="wrong")


async def test_an_unreachable_upstream_is_not_a_rejected_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    async with transport(handler) as client:
        with pytest.raises(OpenRouterError, match="unreachable"):
            await probe_key(client, base_url=BASE, api_key="sk-or-test")


async def test_models_are_priced_per_million_tokens() -> None:
    """OpenRouter quotes per token; every model card in the world quotes per million."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "anthropic/claude-haiku-4.5",
                        "name": "Claude Haiku 4.5",
                        "context_length": 200000,
                        "pricing": {"prompt": "0.000001", "completion": "0.000005"},
                        "supported_parameters": ["structured_outputs", "tools"],
                    },
                    {
                        "id": "someone/legacy-model",
                        "name": "Legacy",
                        "context_length": 8192,
                        "pricing": {"prompt": "0", "completion": "0"},
                        "supported_parameters": ["tools"],
                    },
                    {"name": "no id at all"},
                ]
            },
        )

    async with transport(handler) as client:
        models = await list_models(client, base_url=BASE, api_key="sk-or-test")

    # The entry with no id is dropped rather than rendered as an empty row.
    assert [model.id for model in models] == [
        "anthropic/claude-haiku-4.5",
        "someone/legacy-model",
    ]
    haiku = models[0]
    assert haiku.prompt_per_million == Decimal("1")
    assert haiku.completion_per_million == Decimal("5")
    assert haiku.supports_structured_output is True
    assert models[1].supports_structured_output is False
