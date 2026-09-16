"""The cost ledger (PRD §8.3).

OpenRouter reports token `usage` per call; `GET /models` publishes per-token
prices. Multiplying the two is the only place a dollar figure is ever produced —
nothing estimates, and no model is asked what it costs.

Money is `Decimal` end to end. The `node_run.cost_usd` / `run.cost_usd` columns
are `Numeric(12,4)`, so a value is quantized once, on the way to the database,
and the un-quantized total stays in the ledger — summing rounded node costs
would drift away from the run total by a cent an hour at 21 nodes a run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)

#: `node_run.cost_usd` and `run.cost_usd` are Numeric(12,4).
MONEY = Decimal("0.0001")

#: The catalogue changes when OpenRouter adds models or moves a price, neither
#: of which happens inside one run.
CATALOGUE_TTL = timedelta(hours=6)


class PricingUnavailable(RuntimeError):
    """The catalogue could not be read, so no call can be priced."""


@dataclass(frozen=True, slots=True)
class Usage:
    """Tokens billed by one completion."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> Usage:
        data = payload or {}
        return cls(
            prompt_tokens=int(data.get("prompt_tokens") or 0),
            completion_tokens=int(data.get("completion_tokens") or 0),
        )

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            prompt_tokens=self.prompt_tokens + other.prompt_tokens,
            completion_tokens=self.completion_tokens + other.completion_tokens,
        )


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per single token, as published by OpenRouter."""

    model: str
    prompt: Decimal
    completion: Decimal

    def cost(self, usage: Usage) -> Decimal:
        return self.prompt * usage.prompt_tokens + self.completion * usage.completion_tokens


def quantize_money(value: Decimal) -> Decimal:
    """Round to the four decimal places the schema stores."""
    return value.quantize(MONEY, rounding=ROUND_HALF_UP)


def _decimal(value: Any) -> Decimal:
    """OpenRouter publishes prices as strings; a missing one is free, not an error."""
    if value in (None, ""):
        return Decimal(0)
    try:
        return Decimal(str(value))
    except (ArithmeticError, ValueError):
        return Decimal(0)


class ModelCatalogue:
    """`GET /models`, cached. One instance per process; the TTL is a formality."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        base_url: str,
        api_key: str | None = None,
        ttl: timedelta = CATALOGUE_TTL,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._ttl = ttl
        self._prices: dict[str, ModelPrice] = {}
        self._fetched_at: datetime | None = None

    def _fresh(self) -> bool:
        return self._fetched_at is not None and datetime.now(UTC) - self._fetched_at < self._ttl

    async def prices(self) -> dict[str, ModelPrice]:
        if self._fresh():
            return self._prices
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
        try:
            response = await self._client.get(f"{self._base_url}/models", headers=headers)
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            if self._prices:
                # A stale price is worth far more than an aborted run.
                log.warning("llm.catalogue_stale", error=str(exc))
                return self._prices
            raise PricingUnavailable(
                f"could not read the OpenRouter model catalogue: {exc}"
            ) from exc

        parsed: dict[str, ModelPrice] = {}
        for entry in payload.get("data", []):
            model_id = entry.get("id")
            if not model_id:
                continue
            pricing = entry.get("pricing") or {}
            parsed[str(model_id)] = ModelPrice(
                model=str(model_id),
                prompt=_decimal(pricing.get("prompt")),
                completion=_decimal(pricing.get("completion")),
            )
        self._prices = parsed
        self._fetched_at = datetime.now(UTC)
        log.info("llm.catalogue_loaded", models=len(parsed))
        return self._prices

    async def price_of(self, model: str) -> ModelPrice:
        """The price of one model. An unlisted model prices at zero, loudly.

        Refusing to run because a price is missing would turn a catalogue gap
        into an outage; under-reporting cost is the lesser failure, and it is
        visible in the log and in a run whose cost does not add up.
        """
        prices = await self.prices()
        price = prices.get(model)
        if price is None:
            log.warning("llm.price_missing", model=model)
            return ModelPrice(model=model, prompt=Decimal(0), completion=Decimal(0))
        return price


class BudgetExceeded(RuntimeError):
    """The run spent past `max_run_cost_usd` (PRD §7.2 item 7)."""

    def __init__(self, spent: Decimal, cap: Decimal) -> None:
        super().__init__(f"run cost ${quantize_money(spent)} exceeds the cap of ${cap}")
        self.spent = spent
        self.cap = cap


@dataclass
class RunLedger:
    """Everything one run has spent, and the cap it may not spend past."""

    cap_usd: Decimal
    spent_usd: Decimal = Decimal(0)
    usage: Usage = field(default_factory=Usage)

    def record(self, *, usage: Usage, cost: Decimal) -> None:
        self.usage = self.usage + usage
        self.spent_usd += cost

    @property
    def token_in(self) -> int:
        return self.usage.prompt_tokens

    @property
    def token_out(self) -> int:
        return self.usage.completion_tokens

    @property
    def exceeded(self) -> bool:
        return self.spent_usd > self.cap_usd

    def enforce(self) -> None:
        """Raise if the run has spent past its cap. Checked between nodes, never mid-call."""
        if self.exceeded:
            raise BudgetExceeded(self.spent_usd, self.cap_usd)
