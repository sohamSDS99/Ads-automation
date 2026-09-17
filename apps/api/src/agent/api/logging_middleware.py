"""Bind who and what to every log line a request produces.

Until P8 a log line from inside a route said what happened but not to whom or
under which request, so two people hitting the same endpoint at the same second
produced two interleaved stories with no way to separate them. `structlog`'s
contextvars fix that: bound once here, merged into every event emitted anywhere
downstream — including from a repository three layers down that knows nothing
about HTTP.

Two things it does *not* do:

* **It does not log request bodies.** They carry credentials
  (`POST /credentials`), passwords (`POST /auth/login`) and a whole CSV
  (`POST /projects/{id}/sources/csv`). The method, path and status are what a
  request log is for.
* **It does not bind the actor here.** The session is resolved one layer in, by
  `SessionMiddleware`, which binds it as soon as it knows. This layer stays
  outermost so a request rejected for CSRF — before any session exists — is
  still logged rather than vanishing.

Raw ASGI, like its neighbours, for the reason `middleware.py` gives:
`BaseHTTPMiddleware` breaks Server-Sent Events, and `GET /runs/{id}/events` is
one.
"""

from __future__ import annotations

import time
import uuid

import structlog
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = structlog.get_logger(__name__)

#: Echoed back so a browser bug report and a log line can be matched up.
REQUEST_ID_HEADER = "x-request-id"

#: Paths that are not worth a line each. `/health` is polled by the platform
#: every few seconds and would be most of the log by volume, saying nothing.
QUIET_PATHS = frozenset({"/api/v1/health"})

#: Above this, a request is slow enough to be worth noticing on its own.
SLOW_REQUEST_MS = 2_000


def bind_actor(user_id: uuid.UUID) -> None:
    """Called by `SessionMiddleware` as soon as the session cookie resolves."""
    structlog.contextvars.bind_contextvars(actor_id=str(user_id))


def bind_actor_role(role: str) -> None:
    """Called by `auth.deps` once the `user` row is read.

    Separate from `bind_actor` because the session deliberately does not cache a
    role — `SessionRecord`'s own docstring says why — so the two facts become
    known one layer apart, and the log should not wait for the second to record
    the first.
    """
    structlog.contextvars.bind_contextvars(actor_role=role)


class RequestContextMiddleware:
    """Bind a request id and its shape, then log one line when it finishes."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # Cleared, not merely rebound. ASGI servers reuse tasks, and a leftover
        # `actor_id` from the previous request on the same task would attribute
        # this one to the wrong person — a quiet, plausible, wrong audit trail.
        structlog.contextvars.clear_contextvars()

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        path = scope.get("path", "")
        method = scope.get("method", "")
        structlog.contextvars.bind_contextvars(
            request_id=request_id, http_method=method, http_path=path
        )

        started = time.perf_counter()
        status_code = 500

        async def wrapped(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message).setdefault(REQUEST_ID_HEADER, request_id)
            await send(message)

        try:
            await self.app(scope, receive, wrapped)
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1000
            if path not in QUIET_PATHS:
                # `warning` for a 5xx so the level alone separates "this request
                # failed" from "this request was refused", which is a normal
                # thing for an authorization boundary to do all day.
                emit = log.warning if status_code >= 500 else log.info
                emit(
                    "http.request",
                    status=status_code,
                    duration_ms=round(elapsed_ms, 1),
                    slow=elapsed_ms > SLOW_REQUEST_MS,
                )
            structlog.contextvars.clear_contextvars()


def _incoming_request_id(scope: Scope) -> str | None:
    """Reuse the proxy's id when there is one, so one request has one id end to end."""
    headers: list[tuple[bytes, bytes]] = scope.get("headers", [])
    for key, value in headers:
        if key.lower() == REQUEST_ID_HEADER.encode():
            candidate = value.decode("latin-1").strip()
            # Bounded and filtered: this reaches the log verbatim, and a header
            # is attacker-controlled. A newline in it would forge a log entry.
            if candidate and len(candidate) <= 128 and candidate.isprintable():
                return candidate
    return None
