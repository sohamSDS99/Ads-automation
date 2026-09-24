"""The plumbing `images.py` and `videos.py` share: where OpenRouter is, the
headers every call carries (the gateway's own), and the two ways a call fails.

Neither error ever carries the key or a request body: `ProviderRejected`
carries OpenRouter's *response* body verbatim, which is what a person needs to
see and never contains what was sent.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx

from agent.llm.gateway import auth_headers

#: How the two recorded "no such model" answers read: the images route's 404
#: `No model found for "acme/no-such-model"` and the videos route's 400
#: `Model acme/no-such-model does not exist` (tests/fixtures/openrouter/).
_MODEL_GONE = re.compile(r"no model found|does not exist", re.IGNORECASE)


class ProviderRejected(RuntimeError):
    """OpenRouter answered 4xx. `body` is its response, verbatim."""

    def __init__(self, status: int, body: Any) -> None:
        super().__init__(f"OpenRouter refused the request ({status}): {message_of(body)[:300]}")
        self.status = status
        self.body = body

    @property
    def code(self) -> Literal["model_unavailable", "provider_rejected"]:
        """`model_unavailable` when the model itself is gone — never a cue to
        try another one (Law 36): the user chose this model."""
        if self.status == 404 or _MODEL_GONE.search(message_of(self.body)):
            return "model_unavailable"
        return "provider_rejected"


class ProviderUnavailable(RuntimeError):
    """OpenRouter could not be reached, or failed in a way that is not an answer."""


@dataclass(frozen=True, slots=True)
class MediaApi:
    """One OpenRouter account, reachable through one connection pool."""

    client: httpx.AsyncClient
    api_key: str
    base_url: str
    referer: str = ""

    def url(self, path: str) -> str:
        return f"{self.base_url.rstrip('/')}{path}"

    def headers(self) -> dict[str, str]:
        return auth_headers(self.api_key, referer=self.referer)


def response_body(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def message_of(body: Any) -> str:
    """OpenRouter's `error.message`, or the body itself when it has none."""
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("message") is not None:
            return str(error["message"])
        if error is not None:
            return str(error)
    return str(body)


class JobNotFound(ProviderRejected):
    """A poll or download for a job id OpenRouter does not know (its 404)."""

    @property
    def code(self) -> Literal["job_not_found"]:  # type: ignore[override]
        return "job_not_found"
