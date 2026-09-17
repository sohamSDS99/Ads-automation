"""The connector contract.

PRD §9: "All connectors implement `BaseConnector.fetch(params) -> list[Evidence
Draft]` and are independently testable with recorded VCR cassettes." The second
half of that sentence is the design constraint that shapes this file — a
connector takes its credentials and its HTTP client as arguments and touches
neither the database nor the `Credential` table. That is what lets a cassette
and a dict stand in for the entire rest of the system.

It also keeps P2 independent of credential resolution (user > project >
workspace), which belongs to the phase that owns the vault. When that lands,
wiring it up is a call site, not a change in here.
"""

from __future__ import annotations

import abc
import asyncio
import random
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

from agent.config import Settings, get_settings
from agent.db.models import EvidenceSource
from agent.evidence.normalize import EvidenceDraft

__all__ = [
    "BaseConnector",
    "ConnectorAuthError",
    "ConnectorContext",
    "ConnectorDegraded",
    "ConnectorError",
    "ConnectorRateLimited",
    "ConnectorStatus",
    "EvidenceDraft",
    "build_client",
    "with_retries",
]

log = structlog.get_logger(__name__)

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


class ConnectorError(RuntimeError):
    """The fetch failed and the caller should treat the source as unavailable."""


class ConnectorAuthError(ConnectorError):
    """Credentials are missing, expired or rejected. Retrying will not help."""


class ConnectorRateLimited(ConnectorError):
    """Upstream asked us to slow down."""

    def __init__(self, message: str, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class ConnectorDegraded(Exception):
    """Partial success: some evidence was retrieved, some was not.

    Not an error. PRD §9.2 requires the run to continue and the report to flag
    reduced coverage, so this carries the drafts that *did* come back rather
    than discarding them alongside the failure.
    """

    def __init__(self, reason: str, drafts: list[EvidenceDraft] | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.drafts = drafts or []


@dataclass(slots=True)
class ConnectorStatus:
    """The answer to `POST /credentials/{id}/test`."""

    ok: bool
    detail: str
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ConnectorContext:
    """Everything a connector needs that is not its parameters.

    `credentials` is a plain dict of already-decrypted values. Connectors never
    see ciphertext, never see the vault, and never learn which scope a secret
    came from.
    """

    credentials: dict[str, str] = field(default_factory=dict)
    settings: Settings = field(default_factory=get_settings)
    client: httpx.AsyncClient | None = None
    run_id: str | None = None
    project_id: str | None = None
    #: The exit to crawl ordinary sites through, resolved by
    #: `connectors/proxy.proxy_url` from the workspace's Webshare key. None
    #: means crawl direct, which is the right behaviour for a deployment with
    #: no proxy account rather than an error.
    #:
    #: It sits beside `credentials` rather than inside it because it is not
    #: this connector's credential: one Webshare account serves every
    #: connector that fetches a page, and `web_crawler` must not have to hold
    #: a key it does not own in order to be handed a transport. `require()`
    #: therefore never sees it, and a connector that ignores it still works.
    crawl_proxy: str | None = None

    def require(self, *names: str) -> tuple[str, ...]:
        """Read credential values, naming every one that is missing at once."""
        missing = [name for name in names if not self.credentials.get(name)]
        if missing:
            raise ConnectorAuthError(f"missing credential values: {', '.join(sorted(missing))}")
        return tuple(self.credentials[name] for name in names)


def build_client(
    settings: Settings, *, proxy: str | None = None, **kwargs: Any
) -> httpx.AsyncClient:
    """An HTTP client that identifies itself and gives up in bounded time.

    `proxy` is the Webshare exit a crawl leaves through (`connectors/proxy.py`).
    It is a keyword of its own rather than one more passthrough kwarg because
    the value carries a password in its userinfo, and a named parameter is what
    lets a reader see, at each call site, whether that URL is being handed to
    an upstream this repo has decided may see our traffic.
    """
    headers = {"User-Agent": settings.connector_user_agent, **kwargs.pop("headers", {})}
    return httpx.AsyncClient(
        headers=headers,
        timeout=httpx.Timeout(settings.connector_timeout_s),
        follow_redirects=True,
        proxy=proxy,
        **kwargs,
    )


async def with_retries[T](
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int = 3,
    base_delay_s: float = 0.5,
    label: str = "connector",
) -> T:
    """Retry on transport errors and retryable status codes, with jittered backoff.

    `ConnectorAuthError` is never retried — a rejected credential is rejected
    three times just as fast, and the log line that matters is the first one.
    """
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except ConnectorAuthError:
            raise
        except ConnectorRateLimited as exc:
            last = exc
            delay = (
                exc.retry_after_s if exc.retry_after_s is not None else base_delay_s * 2**attempt
            )
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            if isinstance(exc, httpx.HTTPStatusError) and (
                exc.response.status_code not in RETRYABLE_STATUS
            ):
                raise
            last = exc
            delay = base_delay_s * 2 ** (attempt - 1)

        if attempt == attempts:
            break
        # Jitter so a fleet of parallel fetches does not retry in lockstep.
        jitter = random.uniform(0, base_delay_s)  # noqa: S311 — backoff spread, not a secret
        log.warning(label + ".retry", attempt=attempt, delay_s=round(delay + jitter, 2))
        await asyncio.sleep(delay + jitter)

    raise ConnectorError(f"{label} failed after {attempts} attempts: {last}") from last


class BaseConnector(abc.ABC):
    """One evidence source.

    Subclasses declare `name` and `source`, and implement `fetch`. Everything
    else — retries, the HTTP client, credential lookup — arrives through
    `ConnectorContext` so that a test can supply all three.
    """

    #: Stable identifier used in logs, settings and the connector registry.
    name: str
    #: Which `EvidenceSource` every draft from this connector carries.
    source: EvidenceSource

    def __init__(self, context: ConnectorContext | None = None) -> None:
        self.context = context or ConnectorContext()
        self.settings = self.context.settings

    def __init_subclass__(cls, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if abc.ABC in cls.__bases__:
            return
        for attribute in ("name", "source"):
            if not getattr(cls, attribute, None):
                raise TypeError(f"{cls.__name__} must declare `{attribute}`")

    @abc.abstractmethod
    async def fetch(self, params: dict[str, Any]) -> list[EvidenceDraft]:
        """Retrieve evidence.

        Raises `ConnectorError` on failure, or `ConnectorDegraded` when some of
        the evidence came back and some did not.
        """

    async def test_connection(self) -> ConnectorStatus:
        """Cheapest call that proves the credentials work. Overridden per source."""
        return ConnectorStatus(ok=True, detail="no connectivity test implemented")

    def draft(
        self,
        kind: str,
        payload: dict[str, Any],
        *,
        source_url: str | None = None,
        content_text: str | None = None,
    ) -> EvidenceDraft:
        """Build a draft already stamped with this connector's source."""
        return EvidenceDraft(
            source=self.source,
            kind=kind,
            payload=payload,
            source_url=source_url,
            content_text=content_text,
        )
