"""The OpenRouter gateway (PRD §8).

One entry point — `complete_structured` — and a hard promise: what comes back
is a validated instance of the caller's Pydantic model, or an exception. No
node ever parses model output.

Getting there takes a ladder, because "strict JSON schema" is not universally
supported and the router's fallbacks cross vendors:

1. `response_format={"type":"json_schema", …, "strict":true}` — the contract the
   model is *told* to honour and the provider enforces.
2. forced tool call — the same schema as a single function the model must call.
3. schema in the prompt, then salvage and validate ourselves.

A rung is only descended when the one above is refused or produces something
that will not validate. Below the ladder sit transport retries (429/5xx with
`Retry-After`), and below those the router's next model.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from typing import Any

import httpx
import structlog
from pydantic import BaseModel, ValidationError

from agent.config import Settings
from agent.llm.ledger import ModelCatalogue, Usage
from agent.llm.router import MEDIA_TASK_CLASSES, ModelChoice, TaskClass

log = structlog.get_logger(__name__)

#: Requests in flight across the whole process (PRD §8.4, "shared AsyncLimiter").
#: Four is the executor's wave width, so a full wave never queues on itself.
MAX_CONCURRENCY = 4

#: A ceiling well under OpenRouter's account limits; the point is to arrive at a
#: 429 rarely rather than to squeeze the last request per second out of the key.
REQUESTS_PER_SECOND = 4.0

#: Transport attempts per (model, strategy) before the next rung or model.
MAX_TRANSPORT_ATTEMPTS = 3

#: `2^n * 1.5s` with jitter, the same shape the executor uses for node retries.
BACKOFF_BASE_SECONDS = 1.5

#: A model that ignores this many characters of instruction is not going to be
#: repaired by more of them.
MAX_REPAIR_PASSES = 1

_JSON_FENCE = re.compile(r"```(?:json)?\s*(?P<body>.+?)\s*```", re.DOTALL)

#: Provider errors that mean "this rung, not this model". Matched case-folded
#: against the error body, which is the only signal OpenRouter gives.
_UNSUPPORTED_MARKERS = (
    "response_format",
    "json_schema",
    "structured output",
    "structured outputs",
    "tool_choice",
    "tools are not supported",
    "function calling",
)


class Strategy(StrEnum):
    """The rungs of the ladder, strongest first."""

    STRICT_SCHEMA = "strict_schema"
    TOOL_CALL = "tool_call"
    PROMPT_JSON = "prompt_json"


class LLMError(RuntimeError):
    """Base for every gateway failure."""


class LLMAuthError(LLMError):
    """The key was rejected. Never retried, never fallen back from — every model fails it."""


class LLMTransportError(LLMError):
    """The provider could not be reached, or kept failing, for one model."""


class MediaTaskClassError(LLMError):
    """A media task class reached the text gateway (Stage 04 PRD §7.1, law 36)."""

    def __init__(self, task_class: TaskClass) -> None:
        super().__init__(
            f"{task_class.value} is routed only through media/ — the text gateway never "
            "serves it, so no capability check, budget reservation or G7 gate is skipped."
        )
        self.task_class = task_class


class StructuredOutputError(LLMError):
    """No model on the chain produced output matching the schema."""


class _StrategyUnsupported(LLMError):
    """Internal: this model refuses this rung. Try the next one down."""


@dataclass(frozen=True, slots=True)
class StructuredCompletion[T: BaseModel]:
    """A validated model output plus everything the ledger and the UI need."""

    value: T
    model: str
    strategy: Strategy
    usage: Usage
    cost_usd: Decimal
    latency_ms: int
    repairs: int
    raw_text: str
    prompt: str
    substituted: bool
    """True when the router's primary model was not the one that answered (PRD §16)."""


class RateLimiter:
    """A token bucket plus a concurrency cap, shared by every caller in the process."""

    def __init__(
        self, *, rate: float = REQUESTS_PER_SECOND, concurrency: int = MAX_CONCURRENCY
    ) -> None:
        self._rate = rate
        self._capacity = max(1.0, rate)
        self._tokens = self._capacity
        self._updated = 0.0
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(concurrency)

    async def __aenter__(self) -> None:
        await self._slots.acquire()
        try:
            await self._take()
        except BaseException:
            self._slots.release()
            raise

    async def __aexit__(self, *_exc: object) -> None:
        self._slots.release()

    async def _take(self) -> None:
        while True:
            async with self._lock:
                now = asyncio.get_running_loop().time()
                if self._updated == 0.0:
                    self._updated = now
                self._tokens = min(
                    self._capacity, self._tokens + (now - self._updated) * self._rate
                )
                self._updated = now
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) / self._rate
            await asyncio.sleep(wait)


#: `X-Title` on every OpenRouter request — how the calls are labelled on the
#: account's activity page.
DEFAULT_TITLE = "Paid Ads Research Agent"

#: The process-wide limiter. Tests build their own.
_LIMITER = RateLimiter()


class LLMGateway:
    """Structured completions over OpenRouter, with fallbacks and a cost readout."""

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        api_key: str,
        catalogue: ModelCatalogue,
        base_url: str,
        referer: str = "",
        title: str = DEFAULT_TITLE,
        limiter: RateLimiter | None = None,
        max_transport_attempts: int = MAX_TRANSPORT_ATTEMPTS,
    ) -> None:
        self._client = client
        self._api_key = api_key
        self._catalogue = catalogue
        self._base_url = base_url.rstrip("/")
        self._referer = referer
        self._title = title
        self._limiter = limiter or _LIMITER
        self._max_transport_attempts = max_transport_attempts

    # -- public ------------------------------------------------------------

    async def complete_structured[T: BaseModel](
        self,
        *,
        output_model: type[T],
        system: str,
        user: str,
        choice: ModelChoice,
        on_progress: Callable[[str], Awaitable[None]] | None = None,
    ) -> StructuredCompletion[T]:
        """Return a validated `output_model`, or raise.

        Walks the router's chain; for each model walks the ladder; for each rung
        retries the transport. The first validated object wins.

        IMAGE_GEN and VIDEO_GEN are refused before anything leaves the process
        (Stage 04 PRD §7.1): they are served only through `media/`, where the
        request is capability-validated, budget-reserved and G7-gated first.
        """
        if choice.task_class in MEDIA_TASK_CLASSES:
            raise MediaTaskClassError(choice.task_class)
        failures: list[str] = []
        primary = choice.primary

        for model in choice.chain:
            for strategy in Strategy:
                if on_progress is not None:
                    await on_progress(f"{model} · {strategy.value}")
                try:
                    completion = await self._attempt(
                        model=model,
                        strategy=strategy,
                        output_model=output_model,
                        system=system,
                        user=user,
                        choice=choice,
                    )
                except _StrategyUnsupported as exc:
                    failures.append(f"{model}/{strategy.value}: unsupported ({exc})")
                    continue
                except StructuredOutputError as exc:
                    failures.append(f"{model}/{strategy.value}: invalid output ({exc})")
                    continue
                except LLMTransportError as exc:
                    # The model, not the rung, is the problem. Next model.
                    failures.append(f"{model}: transport ({exc})")
                    break

                if model != primary:
                    log.warning("llm.model_substituted", planned=primary, used=model)
                return StructuredCompletion(
                    value=completion.value,
                    model=model,
                    strategy=strategy,
                    usage=completion.usage,
                    cost_usd=completion.cost_usd,
                    latency_ms=completion.latency_ms,
                    repairs=completion.repairs,
                    raw_text=completion.raw_text,
                    prompt=completion.prompt,
                    substituted=model != primary,
                )

        raise StructuredOutputError(
            f"no model produced valid {output_model.__name__}: " + "; ".join(failures)
        )

    # -- one (model, strategy) --------------------------------------------

    async def _attempt[T: BaseModel](
        self,
        *,
        model: str,
        strategy: Strategy,
        output_model: type[T],
        system: str,
        user: str,
        choice: ModelChoice,
    ) -> StructuredCompletion[T]:
        schema = _strict_schema(output_model)
        messages = self._messages(
            model=model, system=system, user=user, strategy=strategy, schema=schema
        )
        prompt_text = _render_prompt(messages)

        started = asyncio.get_running_loop().time()
        usage = Usage()
        repairs = 0
        raw_text = ""
        last_error = ""

        while True:
            payload = self._payload(
                model=model, messages=messages, strategy=strategy, schema=schema, choice=choice
            )
            body = await self._post(payload)
            usage = usage + Usage.from_payload(body.get("usage"))
            raw_text = _extract_text(body, strategy)

            try:
                value = _parse_into(output_model, raw_text)
            except (ValueError, ValidationError) as exc:
                last_error = _short(exc)
                if repairs >= MAX_REPAIR_PASSES:
                    break
                # PRD §7.2 item 3: one repair pass, with the validation error
                # handed back to the model, before the attempt counts as spent.
                repairs += 1
                log.info("llm.repair_pass", model=model, strategy=strategy.value)
                messages = [
                    *messages,
                    {"role": "assistant", "content": raw_text},
                    {"role": "user", "content": _repair_instruction(last_error, schema)},
                ]
                continue

            price = await self._catalogue.price_of(model)
            latency_ms = int((asyncio.get_running_loop().time() - started) * 1000)
            return StructuredCompletion(
                value=value,
                model=model,
                strategy=strategy,
                usage=usage,
                cost_usd=price.cost(usage),
                latency_ms=latency_ms,
                repairs=repairs,
                raw_text=raw_text,
                prompt=prompt_text,
                substituted=False,
            )

        raise StructuredOutputError(last_error or "empty response")

    # -- transport ---------------------------------------------------------

    async def _post(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST one completion, retrying 429 and 5xx. Never logs the key."""
        url = f"{self._base_url}/chat/completions"
        last: str = ""

        for attempt in range(1, self._max_transport_attempts + 1):
            async with self._limiter:
                try:
                    response = await self._client.post(url, json=payload, headers=self._headers())
                except httpx.HTTPError as exc:
                    last = f"{type(exc).__name__}: {exc}"
                    response = None

            if response is not None:
                if response.status_code < 400:
                    try:
                        return dict(response.json())
                    except ValueError as exc:
                        raise LLMTransportError(f"provider returned non-JSON: {exc}") from exc

                detail = _error_detail(response)
                if response.status_code in (401, 403):
                    raise LLMAuthError(f"OpenRouter rejected the key ({response.status_code})")
                if response.status_code < 500 and response.status_code != 429:
                    if _is_unsupported(detail):
                        raise _StrategyUnsupported(detail)
                    raise LLMTransportError(f"{response.status_code}: {detail}")
                last = f"{response.status_code}: {detail}"

            if attempt == self._max_transport_attempts:
                break
            delay = _backoff(attempt, response)
            log.info(
                "llm.retrying",
                model=payload.get("model"),
                attempt=attempt,
                delay_s=round(delay, 2),
                reason=last[:120],
            )
            await asyncio.sleep(delay)

        raise LLMTransportError(last or "request failed")

    def _headers(self) -> dict[str, str]:
        return auth_headers(self._api_key, referer=self._referer, title=self._title)

    # -- request shaping ---------------------------------------------------

    def _messages(
        self, *, model: str, system: str, user: str, strategy: Strategy, schema: dict[str, Any]
    ) -> list[dict[str, Any]]:
        instruction = system
        if strategy is Strategy.PROMPT_JSON:
            instruction = (
                f"{system}\n\nReply with a single JSON object and nothing else — no prose, "
                f"no code fence. It must validate against this JSON Schema:\n"
                f"{json.dumps(schema, separators=(',', ':'))}"
            )
        return [
            {"role": "system", "content": _system_content(model, instruction)},
            {"role": "user", "content": user},
        ]

    def _payload(
        self,
        *,
        model: str,
        messages: Sequence[dict[str, Any]],
        strategy: Strategy,
        schema: dict[str, Any],
        choice: ModelChoice,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model,
            "messages": list(messages),
            "temperature": choice.temperature,
            "top_p": choice.top_p,
        }
        if strategy is Strategy.STRICT_SCHEMA:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": schema.get("title", "output"),
                    "strict": True,
                    "schema": schema,
                },
            }
        elif strategy is Strategy.TOOL_CALL:
            name = _tool_name(schema)
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": name,
                        "description": "Return the result in this exact shape.",
                        "parameters": schema,
                    },
                }
            ]
            payload["tool_choice"] = {"type": "function", "function": {"name": name}}
        return payload


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _system_content(model: str, text: str) -> Any:
    """System block, marked cacheable where the provider honours it (PRD §8.5).

    Anthropic models bill a cache write once and reads at a discount, and the
    system block is identical across every node in a stage. Other vendors take
    a plain string; sending them a content-block array is a needless difference.
    """
    if model.startswith("anthropic/"):
        return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]
    return text


def _strict_schema(model: type[BaseModel]) -> dict[str, Any]:
    """`model_json_schema()`, tightened to what `strict: true` accepts.

    Strict mode rejects an object that does not pin `additionalProperties:false`
    and does not list every property as required, including the optional ones —
    optionality is expressed by allowing null, not by omission.
    """
    schema = model.model_json_schema()
    _tighten(schema)
    return schema


def _tighten(node: Any) -> None:
    if isinstance(node, dict):
        if node.get("type") == "object" and "properties" in node:
            node["additionalProperties"] = False
            node["required"] = sorted(node["properties"].keys())
        for value in node.values():
            _tighten(value)
    elif isinstance(node, list):
        for item in node:
            _tighten(item)


def _tool_name(schema: dict[str, Any]) -> str:
    raw = str(schema.get("title") or "output")
    cleaned = re.sub(r"[^a-zA-Z0-9_-]", "_", raw)
    return cleaned[:64] or "output"


def _extract_text(body: dict[str, Any], strategy: Strategy) -> str:
    choices = body.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    if strategy is Strategy.TOOL_CALL:
        calls = message.get("tool_calls") or []
        if calls:
            return str(calls[0].get("function", {}).get("arguments") or "")
    content = message.get("content")
    if isinstance(content, list):
        # Some providers return content blocks even for plain completions.
        return "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return str(content or "")


def _parse_into[T: BaseModel](model: type[T], text: str) -> T:
    if not text.strip():
        raise ValueError("empty completion")
    return model.model_validate(_loads(text))


def _loads(text: str) -> Any:
    """`json.loads`, with the two malformations models actually produce salvaged.

    A fenced block and a sentence wrapped around the object are common enough at
    the bottom rung of the ladder to be worth handling here; anything more
    creative fails validation and triggers the repair pass instead.
    """
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = _JSON_FENCE.search(text)
    if fenced:
        try:
            return json.loads(fenced.group("body"))
        except json.JSONDecodeError:
            pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(f"no JSON object in completion: {exc}") from exc
    raise ValueError("no JSON object in completion")


def _repair_instruction(error: str, schema: dict[str, Any]) -> str:
    return (
        "That response did not validate. The error was:\n"
        f"{error}\n\n"
        "Return the corrected JSON object only — no prose, no code fence — matching:\n"
        f"{json.dumps(schema, separators=(',', ':'))}"
    )


def auth_headers(api_key: str, *, referer: str = "", title: str = DEFAULT_TITLE) -> dict[str, str]:
    """The headers every OpenRouter call carries. `media/` sends the same ones
    (Stage 04 §23.1 item 4), so they are spelled once."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "X-Title": title,
    }
    if referer:
        headers["HTTP-Referer"] = referer
    return headers


def _error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text[:300]
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        return str(error.get("message") or error)[:300]
    return str(error or payload)[:300]


def _is_unsupported(detail: str) -> bool:
    lowered = detail.lower()
    return any(marker in lowered for marker in _UNSUPPORTED_MARKERS)


def _backoff(attempt: int, response: httpx.Response | None) -> float:
    """`Retry-After` when the provider sent one, else `2^n * 1.5s` with jitter."""
    if response is not None:
        header = response.headers.get("retry-after")
        if header:
            try:
                return min(60.0, float(header))
            except ValueError:
                pass
    return (2.0**attempt) * BACKOFF_BASE_SECONDS * (0.5 + random.random() / 2)  # noqa: S311


def _short(exc: Exception) -> str:
    text = str(exc).replace("\n", " ")
    return text[:500]


def _render_prompt(messages: Sequence[dict[str, Any]]) -> str:
    """The prompt as sent, flattened for `GET /runs/{id}/nodes/{node_id}`."""
    parts: list[str] = []
    for message in messages:
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        parts.append(f"[{message.get('role')}]\n{content}")
    return "\n\n".join(parts)


def build_gateway(
    *,
    api_key: str,
    settings: Settings,
    client: httpx.AsyncClient | None = None,
    limiter: RateLimiter | None = None,
) -> tuple[LLMGateway, httpx.AsyncClient]:
    """Build a gateway and the client it owns.

    The client is returned rather than hidden so the caller closes it — one run
    opens one connection pool and closes it at the end, instead of leaking one
    per node.
    """
    owned = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))
    catalogue = ModelCatalogue(owned, base_url=settings.openrouter_base_url, api_key=api_key)
    gateway = LLMGateway(
        client=owned,
        api_key=api_key,
        catalogue=catalogue,
        base_url=settings.openrouter_base_url,
        referer=settings.app_base_url,
        limiter=limiter,
    )
    return gateway, owned
