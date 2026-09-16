"""The gateway's ladder: strict schema → forced tool call → prompt, then models."""

from __future__ import annotations

from decimal import Decimal

import httpx
import pytest
from pydantic import BaseModel, Field

from agent.llm.gateway import (
    LLMAuthError,
    LLMGateway,
    RateLimiter,
    Strategy,
    StructuredOutputError,
    _backoff,
    _strict_schema,
)
from agent.llm.ledger import ModelCatalogue
from agent.llm.router import ModelRouter, TaskClass
from tests.openrouter_fake import FakeOpenRouter, completion, error, tool_completion

BASE_URL = "https://openrouter.test/api/v1"
API_KEY = "sk-or-secret-value-do-not-log"  # noqa: S105 — a fixture, not a real key


@pytest.fixture(autouse=True)
def _instant_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the retry tests honest about ordering without paying the real waits."""
    monkeypatch.setattr("agent.llm.gateway.BACKOFF_BASE_SECONDS", 0.0)


class Brief(BaseModel):
    summary: str
    keywords: list[str] = Field(default_factory=list)


def build(fake: FakeOpenRouter, **kwargs: object) -> tuple[LLMGateway, httpx.AsyncClient]:
    client = fake.client()
    catalogue = ModelCatalogue(client, base_url=BASE_URL, api_key=API_KEY)
    gateway = LLMGateway(
        client=client,
        api_key=API_KEY,
        catalogue=catalogue,
        base_url=BASE_URL,
        limiter=RateLimiter(rate=1000, concurrency=8),
        **kwargs,  # type: ignore[arg-type]
    )
    return gateway, client


async def complete(gateway: LLMGateway, task_class: TaskClass = TaskClass.EXTRACT) -> object:
    return await gateway.complete_structured(
        output_model=Brief,
        system="be useful",
        user="describe the company",
        choice=ModelRouter().choose(task_class),
    )


async def test_strict_schema_is_tried_first_and_prices_the_call() -> None:
    fake = FakeOpenRouter()
    fake.queue(completion({"summary": "sells safety data sheets", "keywords": ["sds"]}))
    gateway, client = build(fake)

    async with client:
        result = await complete(gateway)

    assert result.value.summary == "sells safety data sheets"  # type: ignore[attr-defined]
    assert result.strategy is Strategy.STRICT_SCHEMA  # type: ignore[attr-defined]
    assert result.model == "google/gemini-2.5-flash"  # type: ignore[attr-defined]
    assert fake.body(0)["response_format"]["json_schema"]["strict"] is True
    # 100 prompt tokens at $0.000001 + 50 completion tokens at $0.000002.
    assert result.cost_usd == Decimal("0.0002")  # type: ignore[attr-defined]
    assert result.usage.prompt_tokens == 100  # type: ignore[attr-defined]


async def test_determinism_settings_reach_the_provider() -> None:
    fake = FakeOpenRouter()
    fake.queue(completion({"summary": "s", "keywords": []}))
    gateway, client = build(fake)

    async with client:
        await complete(gateway, TaskClass.EXTRACT)

    assert fake.body(0)["temperature"] == 0.0
    assert fake.body(0)["top_p"] == 1.0


async def test_unsupported_schema_drops_to_a_forced_tool_call() -> None:
    fake = FakeOpenRouter()
    fake.queue(
        error(400, "response_format json_schema is not supported by this model"),
        tool_completion({"summary": "from a tool call", "keywords": []}),
    )
    gateway, client = build(fake)

    async with client:
        result = await complete(gateway)

    assert result.strategy is Strategy.TOOL_CALL  # type: ignore[attr-defined]
    assert result.value.summary == "from a tool call"  # type: ignore[attr-defined]
    assert (
        fake.body(1)["tool_choice"]["function"]["name"]
        == fake.body(1)["tools"][0]["function"]["name"]
    )


async def test_last_rung_salvages_a_fenced_json_object() -> None:
    fake = FakeOpenRouter()
    fake.queue(
        error(400, "response_format is not supported"),
        error(400, "tool_choice is not supported"),
        completion('Here you go:\n```json\n{"summary": "fenced", "keywords": []}\n```'),
    )
    gateway, client = build(fake)

    async with client:
        result = await complete(gateway)

    assert result.strategy is Strategy.PROMPT_JSON  # type: ignore[attr-defined]
    assert result.value.summary == "fenced"  # type: ignore[attr-defined]
    # The schema has to be in the prompt on this rung — nothing else enforces it.
    assert "summary" in fake.body(2)["messages"][0]["content"]


async def test_invalid_output_triggers_exactly_one_repair_pass() -> None:
    fake = FakeOpenRouter()
    fake.queue(
        completion({"keywords": ["missing the summary"]}),
        completion({"summary": "repaired", "keywords": []}),
    )
    gateway, client = build(fake)

    async with client:
        result = await complete(gateway)

    assert result.repairs == 1  # type: ignore[attr-defined]
    assert result.value.summary == "repaired"  # type: ignore[attr-defined]
    repair_messages = fake.body(1)["messages"]
    assert repair_messages[-1]["role"] == "user"
    assert "did not validate" in repair_messages[-1]["content"]
    # Usage accumulates across the repair: two calls, not one.
    assert result.usage.prompt_tokens == 200  # type: ignore[attr-defined]


async def test_repair_is_not_attempted_twice_on_one_rung() -> None:
    fake = FakeOpenRouter()
    fake.always(completion({"nope": True}))
    gateway, client = build(fake)

    async with client:
        with pytest.raises(StructuredOutputError):
            await complete(gateway)

    # 3 rungs x (first call + one repair) = 6, for each of the two models.
    assert len(fake.requests) == 12


async def test_429_is_retried_after_the_provider_says_when() -> None:
    fake = FakeOpenRouter()
    fake.queue(
        error(429, "rate limited", **{"retry-after": "0"}),
        completion({"summary": "second time", "keywords": []}),
    )
    gateway, client = build(fake)

    async with client:
        result = await complete(gateway)

    assert result.value.summary == "second time"  # type: ignore[attr-defined]
    assert len(fake.requests) == 2


async def test_persistent_5xx_falls_back_to_the_next_model() -> None:
    fake = FakeOpenRouter()
    fake.queue(
        error(503, "upstream unavailable"),
        error(503, "upstream unavailable"),
        error(503, "upstream unavailable"),
        completion({"summary": "fallback model", "keywords": []}),
    )
    gateway, client = build(fake, max_transport_attempts=3)

    async with client:
        result = await complete(gateway)

    assert result.model == "anthropic/claude-haiku-4.5"  # type: ignore[attr-defined]
    assert result.substituted is True  # type: ignore[attr-defined]
    assert fake.models_called[:3] == ["google/gemini-2.5-flash"] * 3


async def test_a_rejected_key_fails_immediately_and_is_not_echoed() -> None:
    fake = FakeOpenRouter()
    fake.queue(error(401, "No auth credentials found"))
    gateway, client = build(fake)

    async with client:
        with pytest.raises(LLMAuthError) as raised:
            await complete(gateway)

    assert len(fake.requests) == 1, "a bad key must not be retried against every model"
    assert API_KEY not in str(raised.value)


async def test_the_key_is_sent_as_a_bearer_header() -> None:
    fake = FakeOpenRouter()
    fake.queue(completion({"summary": "s", "keywords": []}))
    gateway, client = build(fake)

    async with client:
        await complete(gateway)

    assert fake.requests[0].headers["authorization"] == f"Bearer {API_KEY}"
    assert API_KEY not in str(fake.bodies[0])


async def test_anthropic_system_blocks_are_marked_cacheable() -> None:
    fake = FakeOpenRouter()
    fake.queue(completion({"summary": "s", "keywords": []}))
    gateway, client = build(fake)

    async with client:
        await complete(gateway, TaskClass.CLASSIFY)  # routes to anthropic/claude-haiku-4.5

    system = fake.body(0)["messages"][0]["content"]
    assert system[0]["cache_control"] == {"type": "ephemeral"}


def test_strict_schema_pins_additional_properties_and_required() -> None:
    schema = _strict_schema(Brief)
    assert schema["additionalProperties"] is False
    # `keywords` has a default, so Pydantic would leave it out of `required`;
    # strict mode rejects that.
    assert schema["required"] == ["keywords", "summary"]


def test_backoff_prefers_retry_after_then_grows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.llm.gateway.BACKOFF_BASE_SECONDS", 1.5)
    told = httpx.Response(429, headers={"retry-after": "7"})
    assert _backoff(1, told) == 7.0

    # Without a header: 2^n * 1.5s, jittered into [50%, 100%] of it.
    first = _backoff(1, None)
    second = _backoff(2, None)
    assert 1.5 <= first <= 3.0
    assert 3.0 <= second <= 6.0


def test_backoff_caps_a_hostile_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("agent.llm.gateway.BACKOFF_BASE_SECONDS", 1.5)
    assert _backoff(1, httpx.Response(429, headers={"retry-after": "86400"})) == 60.0
