"""Two OpenRouter reads that are about the account, not about a completion.

`llm/gateway.py` owns calling models and `llm/ledger.py` owns pricing them for
the cost ledger. This module answers the two questions the *interface* asks:
"is this key any good, and what is left on it" for the key vault, and "what may
I choose from" for the model picker (PRD §13.4 step 3).

Both are deliberately outside the gateway: they must work for a key that has
just been typed into a form and may turn out to be wrong, which is not a state
the run path ever has to handle.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

#: OpenRouter prices per token; the picker shows price per million, which is the
#: unit every model card on the internet quotes.
PER_MILLION = Decimal(1_000_000)


class OpenRouterError(RuntimeError):
    """OpenRouter refused or could not be reached. Carries something showable."""

    def __init__(self, detail: str, *, status_code: int | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class KeyStatus:
    """What `POST /credentials/{id}/test` learned about an OpenRouter key."""

    label: str | None
    usage_usd: Decimal | None
    limit_usd: Decimal | None
    remaining_usd: Decimal | None
    is_free_tier: bool | None

    def as_meta(self) -> dict[str, Any]:
        """The part worth storing on the credential row. No secret in here."""
        return {
            "label": self.label,
            "usage_usd": _str(self.usage_usd),
            "limit_usd": _str(self.limit_usd),
            "remaining_usd": _str(self.remaining_usd),
            "is_free_tier": self.is_free_tier,
        }

    @property
    def detail(self) -> str:
        if self.remaining_usd is not None:
            return f"Key accepted. ${self.remaining_usd:.2f} of credit remaining."
        if self.usage_usd is not None:
            return f"Key accepted. ${self.usage_usd:.2f} used, no spending limit set."
        return "Key accepted."


@dataclass(frozen=True, slots=True)
class ModelInfo:
    """One row of the model picker."""

    id: str
    name: str
    context_length: int | None
    prompt_per_million: Decimal | None
    completion_per_million: Decimal | None
    #: True when the model can be asked for a schema-validated JSON object.
    #: PRD §8 requirement 1 makes that the difference between a model this
    #: product can route to and one it can only fall back to.
    supports_structured_output: bool


async def probe_key(client: httpx.AsyncClient, *, base_url: str, api_key: str) -> KeyStatus:
    """Validate a key and read its balance.

    `/auth/key` is the probe rather than `/models`, because `/models` answers
    for an unauthenticated caller too — it would call a wrong key valid.
    """
    payload = await _get(client, base_url, "/auth/key", api_key)
    data = payload.get("data") or {}
    usage = _decimal(data.get("usage"))
    limit = _decimal(data.get("limit"))
    remaining = _decimal(data.get("limit_remaining"))
    if remaining is None and limit is not None and usage is not None:
        remaining = limit - usage
    return KeyStatus(
        label=_str_or_none(data.get("label")),
        usage_usd=usage,
        limit_usd=limit,
        remaining_usd=remaining,
        is_free_tier=bool(data["is_free_tier"]) if "is_free_tier" in data else None,
    )


async def list_models(
    client: httpx.AsyncClient, *, base_url: str, api_key: str | None = None
) -> list[ModelInfo]:
    """The catalogue, priced per million tokens and sorted by id."""
    payload = await _get(client, base_url, "/models", api_key)
    models: list[ModelInfo] = []
    for entry in payload.get("data") or []:
        model_id = entry.get("id")
        if not model_id:
            continue
        pricing = entry.get("pricing") or {}
        models.append(
            ModelInfo(
                id=str(model_id),
                name=str(entry.get("name") or model_id),
                context_length=_int(entry.get("context_length")),
                prompt_per_million=_per_million(pricing.get("prompt")),
                completion_per_million=_per_million(pricing.get("completion")),
                supports_structured_output=_supports_structured_output(entry),
            )
        )
    models.sort(key=lambda model: model.id)
    log.info("openrouter.models_listed", count=len(models))
    return models


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _get(
    client: httpx.AsyncClient, base_url: str, path: str, api_key: str | None
) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    try:
        response = await client.get(f"{base_url.rstrip('/')}{path}", headers=headers)
    except httpx.HTTPError as exc:
        raise OpenRouterError(f"OpenRouter is unreachable: {exc}") from exc

    if response.status_code in (401, 403):
        raise OpenRouterError("OpenRouter rejected this key.", status_code=response.status_code)
    if response.status_code >= 400:
        raise OpenRouterError(
            f"OpenRouter answered {response.status_code}.", status_code=response.status_code
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise OpenRouterError("OpenRouter returned something that is not JSON.") from exc
    if not isinstance(body, dict):
        raise OpenRouterError("OpenRouter returned an unexpected shape.")
    return body


def _supports_structured_output(entry: dict[str, Any]) -> bool:
    """Whether the catalogue claims strict JSON-schema support for this model.

    OpenRouter reports this as a parameter name, and the name has moved before.
    Both spellings are accepted rather than one being guessed at.
    """
    parameters = entry.get("supported_parameters") or []
    if not isinstance(parameters, list):
        return False
    names = {str(item) for item in parameters}
    return bool(names & {"structured_outputs", "response_format"})


def _per_million(value: Any) -> Decimal | None:
    price = _decimal(value)
    return None if price is None else price * PER_MILLION


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _str(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _str_or_none(value: Any) -> str | None:
    return None if value is None else str(value)
