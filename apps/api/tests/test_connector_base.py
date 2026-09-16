"""Retries, credential handling, and the partial-success contract."""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from agent.config import Settings
from agent.connectors.base import (
    BaseConnector,
    ConnectorAuthError,
    ConnectorContext,
    ConnectorDegraded,
    ConnectorError,
    ConnectorRateLimited,
    build_client,
    with_retries,
)
from agent.db.models import EvidenceSource
from agent.evidence.normalize import EvidenceDraft

TEST_KEY = "dW5pdC10ZXN0LWtleS0zMi1ieXRlcy1leGFjdGx5ISE="


def settings(**kwargs: Any) -> Settings:
    return Settings(app_encryption_key=TEST_KEY, **kwargs)


class Dummy(BaseConnector):
    name = "dummy"
    source = EvidenceSource.DERIVED

    async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
        return [self.draft("thing", params)]


def test_a_connector_without_a_name_is_refused_at_class_definition() -> None:
    """Catching this at import beats catching it when the registry returns None."""
    with pytest.raises(TypeError, match="name"):

        class Nameless(BaseConnector):
            source = EvidenceSource.DERIVED

            async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
                return []


def test_a_connector_without_a_source_is_refused() -> None:
    with pytest.raises(TypeError, match="source"):

        class Sourceless(BaseConnector):
            name = "sourceless"

            async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
                return []


async def test_draft_stamps_the_connectors_source() -> None:
    drafts = await Dummy(ConnectorContext(settings=settings())).fetch({"a": 1})
    assert drafts[0].source is EvidenceSource.DERIVED
    assert drafts[0].kind == "thing"


def test_require_names_every_missing_credential_at_once() -> None:
    """One round trip for the user, not three."""
    context = ConnectorContext(credentials={"client_id": "x"}, settings=settings())
    with pytest.raises(ConnectorAuthError) as caught:
        context.require("client_id", "client_secret", "refresh_token")
    message = str(caught.value)
    assert "client_secret" in message
    assert "refresh_token" in message
    assert "client_id" not in message.split(":", 1)[1]


def test_require_treats_an_empty_string_as_missing() -> None:
    context = ConnectorContext(credentials={"token": ""}, settings=settings())
    with pytest.raises(ConnectorAuthError):
        context.require("token")


def test_require_returns_values_in_the_order_asked() -> None:
    context = ConnectorContext(credentials={"a": "1", "b": "2"}, settings=settings())
    assert context.require("b", "a") == ("2", "1")


async def test_retries_then_succeeds() -> None:
    attempts = {"count": 0}

    async def flaky() -> str:
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise httpx.ConnectError("boom")
        return "ok"

    assert await with_retries(flaky, attempts=3, base_delay_s=0.0) == "ok"
    assert attempts["count"] == 3


async def test_an_auth_error_is_never_retried() -> None:
    """A rejected credential is rejected just as fast three times."""
    attempts = {"count": 0}

    async def denied() -> str:
        attempts["count"] += 1
        raise ConnectorAuthError("nope")

    with pytest.raises(ConnectorAuthError):
        await with_retries(denied, attempts=5, base_delay_s=0.0)
    assert attempts["count"] == 1


async def test_a_non_retryable_status_is_raised_immediately() -> None:
    """A 400 is our bug; retrying it just delays the report."""
    attempts = {"count": 0}

    async def bad_request() -> str:
        attempts["count"] += 1
        raise httpx.HTTPStatusError(
            "bad",
            request=httpx.Request("GET", "https://x.test"),
            response=httpx.Response(400, request=httpx.Request("GET", "https://x.test")),
        )

    with pytest.raises(httpx.HTTPStatusError):
        await with_retries(bad_request, attempts=3, base_delay_s=0.0)
    assert attempts["count"] == 1


async def test_a_retryable_status_is_retried() -> None:
    attempts = {"count": 0}

    async def unavailable() -> str:
        attempts["count"] += 1
        raise httpx.HTTPStatusError(
            "unavailable",
            request=httpx.Request("GET", "https://x.test"),
            response=httpx.Response(503, request=httpx.Request("GET", "https://x.test")),
        )

    with pytest.raises(ConnectorError):
        await with_retries(unavailable, attempts=3, base_delay_s=0.0)
    assert attempts["count"] == 3


async def test_rate_limiting_honours_retry_after() -> None:
    attempts = {"count": 0}

    async def limited() -> str:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise ConnectorRateLimited("slow down", retry_after_s=0.0)
        return "ok"

    assert await with_retries(limited, attempts=3, base_delay_s=0.0) == "ok"


async def test_exhausted_retries_name_the_last_failure() -> None:
    async def always() -> str:
        raise httpx.ConnectError("connection refused")

    with pytest.raises(ConnectorError, match="connection refused"):
        await with_retries(always, attempts=2, base_delay_s=0.0, label="probe")


def test_degraded_carries_the_partial_results() -> None:
    """The whole point: a partial failure must not discard what did come back."""
    drafts = [EvidenceDraft(source=EvidenceSource.WEB, kind="page", payload={"a": 1})]
    error = ConnectorDegraded("one of three failed", drafts)
    assert error.drafts == drafts
    assert error.reason == "one of three failed"
    assert not isinstance(error, ConnectorError)


def test_the_client_identifies_itself() -> None:
    """An unidentified crawler gets blocked, and deserves to be."""
    client = build_client(settings())
    try:
        assert "AdsResearchAgent" in client.headers["user-agent"]
        assert client.timeout.read == 30.0
    finally:
        pass
