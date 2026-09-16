"""A scripted OpenRouter, as an `httpx.MockTransport`.

Every gateway test drives the real `LLMGateway` — the ladder, the retries, the
cost arithmetic — against this. Asserting on what the gateway *sent* is the only
way to prove things like "the second call actually carried a forced tool choice"
rather than trusting that it did.
"""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Callable
from typing import Any

import httpx

DEFAULT_PRICES: dict[str, dict[str, str]] = {
    "google/gemini-2.5-flash": {"prompt": "0.000001", "completion": "0.000002"},
    "anthropic/claude-haiku-4.5": {"prompt": "0.000002", "completion": "0.000004"},
    "anthropic/claude-opus-4.6": {"prompt": "0.00001", "completion": "0.00003"},
    "openai/gpt-5.2": {"prompt": "0.000005", "completion": "0.000015"},
}

DEFAULT_USAGE = {"prompt_tokens": 100, "completion_tokens": 50}


def completion(
    payload: Any, *, model: str = "google/gemini-2.5-flash", usage: dict[str, int] | None = None
) -> httpx.Response:
    """A normal content response. `payload` is dumped to JSON unless it is already a string."""
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return httpx.Response(
        200,
        json={
            "id": "gen-1",
            "model": model,
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}}],
            "usage": usage or DEFAULT_USAGE,
        },
    )


def tool_completion(
    payload: Any, *, name: str = "output", model: str = "google/gemini-2.5-flash"
) -> httpx.Response:
    """A forced tool call, where the arguments carry the structured output."""
    return httpx.Response(
        200,
        json={
            "id": "gen-2",
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(payload)},
                            }
                        ],
                    },
                }
            ],
            "usage": DEFAULT_USAGE,
        },
    )


def error(status_code: int, message: str, **headers: str) -> httpx.Response:
    return httpx.Response(status_code, json={"error": {"message": message}}, headers=headers)


class FakeOpenRouter:
    """A queue of scripted responses plus a log of what was asked."""

    def __init__(self, prices: dict[str, dict[str, str]] | None = None) -> None:
        self.prices = prices if prices is not None else DEFAULT_PRICES
        self.requests: list[httpx.Request] = []
        self.bodies: list[dict[str, Any]] = []
        self.catalogue_calls = 0
        self._queue: deque[httpx.Response | Callable[[httpx.Request], httpx.Response]] = deque()
        self.default: httpx.Response | None = None

    # -- scripting ---------------------------------------------------------

    def queue(self, *responses: httpx.Response | Callable[[httpx.Request], httpx.Response]) -> None:
        self._queue.extend(responses)

    def always(self, response: httpx.Response) -> None:
        """Answer every completion with this, forever. Copied per call."""
        self.default = response

    # -- transport ---------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/models"):
            self.catalogue_calls += 1
            return httpx.Response(
                200,
                json={
                    "data": [
                        {"id": model, "pricing": pricing} for model, pricing in self.prices.items()
                    ]
                },
            )

        self.requests.append(request)
        self.bodies.append(json.loads(request.content or b"{}"))
        if self._queue:
            scripted = self._queue.popleft()
            return scripted(request) if callable(scripted) else scripted
        if self.default is not None:
            return httpx.Response(
                self.default.status_code,
                content=self.default.content,
                headers={"content-type": "application/json"},
            )
        raise AssertionError(
            f"unscripted completion request #{len(self.requests)} to {request.url}"
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))

    # -- assertions --------------------------------------------------------

    @property
    def models_called(self) -> list[str]:
        return [body.get("model", "") for body in self.bodies]

    def body(self, index: int) -> dict[str, Any]:
        return self.bodies[index]
