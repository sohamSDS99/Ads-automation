"""`GET /models` — the catalogue the picker is hydrated from."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from agent.api import routes_models
from agent.llm.openrouter import ModelInfo, OpenRouterError
from tests.integration.conftest import ApiClient

CATALOGUE = [
    ModelInfo(
        id="anthropic/claude-haiku-4.5",
        name="Claude Haiku 4.5",
        context_length=200_000,
        prompt_per_million=Decimal("1"),
        completion_per_million=Decimal("5"),
        supports_structured_output=True,
    ),
    ModelInfo(
        id="google/gemini-2.5-flash",
        name="Gemini 2.5 Flash",
        context_length=1_000_000,
        prompt_per_million=Decimal("0.3"),
        completion_per_million=Decimal("2.5"),
        supports_structured_output=True,
    ),
]


@pytest.fixture(autouse=True)
def _empty_cache() -> None:
    """The catalogue is cached per process; a test must not inherit one."""
    routes_models.reset_cache()


def _serve(monkeypatch: Any, models: list[ModelInfo]) -> None:
    async def fake(*_args: Any, **_kwargs: Any) -> list[ModelInfo]:
        return models

    monkeypatch.setattr("agent.api.routes_models.list_models", fake)


async def test_without_a_key_the_answer_is_a_409_that_says_what_to_do(
    admin: ApiClient,
) -> None:
    response = await admin.get("/models")
    assert response.status_code == 409
    assert response.json()["missing_credential"] == "openrouter"
    assert "Settings" in response.json()["detail"]


async def test_the_catalogue_comes_back_priced_per_million(
    admin: ApiClient, project: Any, monkeypatch: Any
) -> None:
    """`project` seeds the workspace OpenRouter credential the route resolves."""
    _serve(monkeypatch, CATALOGUE)

    body = (await admin.get("/models")).json()

    assert [model["id"] for model in body["models"]] == [
        "anthropic/claude-haiku-4.5",
        "google/gemini-2.5-flash",
    ]
    assert body["models"][0]["prompt_per_million"] == "1"


async def test_every_task_class_arrives_with_its_seed_and_its_baseline(
    admin: ApiClient, project: Any, monkeypatch: Any
) -> None:
    _serve(monkeypatch, CATALOGUE)

    body = (await admin.get("/models")).json()
    classes = {row["task_class"]: row for row in body["task_classes"]}

    assert set(classes) == {"extract", "classify", "synthesize", "critique"}
    assert classes["extract"]["default_model"] == "google/gemini-2.5-flash"
    assert classes["extract"]["fallbacks"]
    assert classes["extract"]["token_in_per_run"] > 0
    # No runs have finished, so the baseline is this build's declared
    # assumption — and it says so rather than presenting itself as measured.
    assert body["usage_source"] == "assumed"
    assert classes["synthesize"]["usage_source"] == "assumed"


async def test_an_operator_cannot_read_the_catalogue(
    signed_in_as: Any, project: Any, monkeypatch: Any
) -> None:
    """The only screen that consumes it is the admin-only routing step (§13.4)."""
    _serve(monkeypatch, CATALOGUE)
    operator = await signed_in_as("operator")

    response = await operator.get("/models")
    assert response.status_code == 403
    assert response.json()["missing_permission"] == "settings_write"


async def test_an_unreachable_upstream_is_a_502_not_a_500(
    admin: ApiClient, project: Any, monkeypatch: Any
) -> None:
    async def explode(*_args: Any, **_kwargs: Any) -> list[ModelInfo]:
        raise OpenRouterError("OpenRouter is unreachable: timed out")

    monkeypatch.setattr("agent.api.routes_models.list_models", explode)

    response = await admin.get("/models")
    assert response.status_code == 502
    assert "unreachable" in response.json()["detail"]


async def test_the_catalogue_is_fetched_once_per_ttl(
    admin: ApiClient, project: Any, monkeypatch: Any
) -> None:
    """Four browsers on the settings screen must not be four upstream fetches."""
    calls = 0

    async def counting(*_args: Any, **_kwargs: Any) -> list[ModelInfo]:
        nonlocal calls
        calls += 1
        return CATALOGUE

    monkeypatch.setattr("agent.api.routes_models.list_models", counting)

    await admin.get("/models")
    await admin.get("/models")
    assert calls == 1

    await admin.get("/models?refresh=true")
    assert calls == 2
