"""`POST /api/v1/images` — synchronous, base64 out (PRD §9.1 item 3, §23.1 item 4).

The request is validated against the pinned capability record **before** a
byte leaves (Law 36): an unsupported field raises `CapabilityUnsupported` and
OpenRouter never sees it. What goes on the wire is exactly the fields the
request sets — the record allowed each one — plus `provider={only:[tag],
allow_fallbacks:false}` when the choice pinned an endpoint.

Image generation is all-or-nothing billed and a failed generation is a `502`,
unbilled (PRD §8.4), so a 502 is retried three times with backoff. Nothing
else is: another 5xx or a transport error may have been billed, and is the
job layer's to classify; a 4xx is OpenRouter's answer and is raised with its
body verbatim. One successful call is one ledger entry.
"""

from __future__ import annotations

import asyncio
import base64
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog

from agent.llm.ledger import RunLedger, Usage
from agent.media.capability import CapabilityUnsupported, validate
from agent.media.http import MediaApi, ProviderRejected, ProviderUnavailable, response_body
from agent.media.types import CapabilityRecord, ImageRequest, ProviderPreferences, ReferenceImage

log = structlog.get_logger(__name__)

#: One try and three retries of an unbilled 502 (§23.1 item 4: "retried x3").
MAX_ATTEMPTS = 4
BACKOFF_BASE_SECONDS = 1.5


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    data: bytes
    #: Present whenever OpenRouter could identify the format.
    media_type: str | None


@dataclass(frozen=True, slots=True)
class ImageResult:
    images: list[GeneratedImage]
    #: `usage.cost`; None only if OpenRouter did not report one.
    cost_usd: Decimal | None
    usage: Usage
    attempts: int


class ImageClient:
    def __init__(
        self,
        api: MediaApi,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._api = api
        self._sleep = sleep

    async def generate(
        self,
        request: ImageRequest,
        *,
        capability: CapabilityRecord,
        ledger: RunLedger | None = None,
    ) -> ImageResult:
        """Generate, decode, and record the cost once. Raises
        `CapabilityUnsupported` (nothing sent), `ProviderRejected` (4xx) or
        `ProviderUnavailable`."""
        request = pinned(request, capability)
        errors = validate(request, capability)
        if errors:
            raise CapabilityUnsupported(errors)

        payload = wire_image(request)
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = await self._api.client.post(
                    self._api.url("/images"), json=payload, headers=self._api.headers()
                )
            except httpx.HTTPError as exc:
                raise ProviderUnavailable(
                    f"OpenRouter could not be reached for an image ({type(exc).__name__})."
                ) from exc
            if response.status_code == 502 and attempt < MAX_ATTEMPTS:
                delay = _backoff(attempt)
                log.info("media.image_retry", model=request.model, attempt=attempt, delay_s=delay)
                await self._sleep(delay)
                continue
            if response.status_code >= 500:
                raise ProviderUnavailable(
                    f"OpenRouter answered {response.status_code} for an image"
                    + (f" after {attempt} attempts." if attempt > 1 else ".")
                )
            if response.status_code >= 400:
                raise ProviderRejected(response.status_code, response_body(response))
            return self._result(request, response, attempt, ledger)
        raise AssertionError("unreachable: the last attempt returns or raises")

    def _result(
        self,
        request: ImageRequest,
        response: httpx.Response,
        attempts: int,
        ledger: RunLedger | None,
    ) -> ImageResult:
        try:
            payload = response.json()
            images = [
                GeneratedImage(
                    data=base64.b64decode(item["b64_json"], validate=True),
                    media_type=item.get("media_type"),
                )
                for item in payload["data"]
            ]
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                f"OpenRouter returned an image response this client cannot read "
                f"({type(exc).__name__})."
            ) from exc
        usage_payload = payload.get("usage") or {}
        usage = Usage.from_payload(usage_payload)
        cost = _cost(usage_payload.get("cost"))
        if ledger is not None and cost is not None:
            ledger.record(usage=usage, cost=cost)
        log.info(
            "media.image_generated",
            model=request.model,
            images=len(images),
            cost_usd=str(cost) if cost is not None else None,
            attempts=attempts,
        )
        return ImageResult(images=images, cost_usd=cost, usage=usage, attempts=attempts)


def pinned(request: ImageRequest, capability: CapabilityRecord) -> ImageRequest:
    """A choice that pins an endpoint sends `only=[tag]`, no fallbacks."""
    if capability.provider_tag is None or request.provider is not None:
        return request
    return request.model_copy(
        update={
            "provider": ProviderPreferences(only=[capability.provider_tag], allow_fallbacks=False)
        }
    )


def wire_image(request: ImageRequest) -> dict[str, Any]:
    """The JSON body: every set field, references as base64 data URLs."""
    payload = request.model_dump(mode="json", exclude_none=True, exclude={"input_references"})
    if request.input_references:
        payload["input_references"] = [data_url_part(ref) for ref in request.input_references]
    return payload


def data_url_part(reference: ReferenceImage) -> dict[str, Any]:
    """An OpenRouter `ContentPartImage` carrying the bytes inline. The Volume
    has no public URL, and none may be invented (PRD §9.1 item 5)."""
    encoded = base64.b64encode(reference.data).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{reference.media_type};base64,{encoded}"},
    }


def _backoff(attempt: int) -> float:
    """`1.5 s · 2^(n-1)`, ±25 % jitter, so three retries wait ~1.5, 3, 6 s."""
    return BACKOFF_BASE_SECONDS * 2.0 ** (attempt - 1) * (0.75 + random.random() / 2)  # noqa: S311


def _cost(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
