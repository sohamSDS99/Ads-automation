"""Session resolution, CSRF and security headers.

Both are written as raw ASGI middleware rather than `BaseHTTPMiddleware`.
`BaseHTTPMiddleware` pumps the response through an anyio stream, which is the
documented way to break Server-Sent Events — and `GET /runs/{id}/events` is an
SSE endpoint from P1 onward. Intercepting `http.response.start` costs twenty
extra lines and leaves streaming untouched.
"""

from __future__ import annotations

import hmac
from collections.abc import MutableMapping
from datetime import UTC, datetime, timedelta
from http.cookies import SimpleCookie
from ipaddress import ip_address
from typing import Any

import structlog
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agent.api import problems
from agent.auth.deps import REQUEST_STATE_SESSION
from agent.auth.sessions import IDLE_TIMEOUT, SessionRecord, SessionStore, new_csrf_token
from agent.config import Settings
from agent.redis_client import get_redis

log = structlog.get_logger(__name__)

CSRF_COOKIE_NAME = "csrf"
CSRF_HEADER_NAME = "x-csrf-token"

#: Methods that may not change state, so they need no CSRF token. `GET` covers
#: the SSE stream, which is the one endpoint that could not send a header anyway
#: (`EventSource` has no API for it).
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})

#: Re-writing the session on every request would mean a Redis round trip per
#: page load for no gain: the idle window is twelve hours, so sliding it at
#: minute granularity is indistinguishable from sliding it continuously.
TOUCH_INTERVAL = timedelta(seconds=60)


def csrf_cookie_kwargs(settings: Settings) -> dict[str, Any]:
    """Cookie attributes for the CSRF token.

    Not `HttpOnly` — by design. The browser has to read this one to echo it back
    in `X-CSRF-Token`, which is exactly what makes the double-submit work: a
    cross-site attacker can cause the cookie to be *sent* but cannot *read* it.
    """
    return {
        "httponly": False,
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "path": "/",
        "max_age": int(IDLE_TIMEOUT.total_seconds()),
    }


def session_cookie_kwargs(settings: Settings) -> dict[str, Any]:
    return {
        "httponly": True,
        "secure": settings.cookie_secure,
        "samesite": "lax",
        "path": "/",
        "max_age": settings.session_ttl_days * 24 * 60 * 60,
    }


class SessionMiddleware:
    """Resolve the session cookie, slide its TTL, and enforce CSRF on writes."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive)
        store = SessionStore(get_redis())

        record = await self._resolve(request, store)
        state: MutableMapping[str, Any] = scope.setdefault("state", {})
        state[REQUEST_STATE_SESSION] = record

        cookie_csrf = request.cookies.get(CSRF_COOKIE_NAME)

        if request.method not in SAFE_METHODS:
            failure = self._csrf_failure(request, record, cookie_csrf)
            if failure is not None:
                log.info(
                    "auth.csrf_rejected",
                    path=request.url.path,
                    method=request.method,
                    reason=failure,
                )
                response = problems.Problem(
                    status_code=403,
                    title="CSRF check failed",
                    detail=(
                        "This request is missing a valid X-CSRF-Token header. "
                        "Reload the page and try again."
                    ),
                    type_=problems.TYPE_CSRF,
                ).to_response(instance=request.url.path)
                await response(scope, receive, send)
                return

        # A caller with no usable CSRF cookie gets one minted on the way out, so
        # the very first GET of a session (including `/login`'s priming call)
        # leaves the browser able to make its first write.
        mint_csrf: str | None = None
        if record is not None and cookie_csrf != record.csrf_token:
            mint_csrf = record.csrf_token
        elif record is None and not cookie_csrf:
            mint_csrf = new_csrf_token()

        if mint_csrf is None:
            await self.app(scope, receive, send)
            return

        await self.app(scope, receive, self._send_with_csrf(send, mint_csrf))

    async def _resolve(self, request: Request, store: SessionStore) -> SessionRecord | None:
        sid = request.cookies.get(self.settings.session_cookie_name)
        if not sid:
            return None
        record = await store.read(sid)
        if record is None:
            return None
        if record.last_seen_at + TOUCH_INTERVAL < datetime.now(UTC):
            record = await store.touch(record)
        return record

    def _csrf_failure(
        self, request: Request, record: SessionRecord | None, cookie_csrf: str | None
    ) -> str | None:
        header = request.headers.get(CSRF_HEADER_NAME)
        if not header:
            return "missing-header"
        # With a session, the session's own token is the authority, so a stale
        # cookie left over from before sign-in cannot be replayed.
        expected = record.csrf_token if record is not None else cookie_csrf
        if not expected:
            return "missing-cookie"
        if not _constant_time_equal(header, expected):
            return "mismatch"
        return None

    def _send_with_csrf(self, send: Send, token: str) -> Send:
        settings = self.settings

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start" and not _sets_csrf_cookie(message):
                headers = MutableHeaders(scope=message)
                headers.append("set-cookie", _cookie_header(CSRF_COOKIE_NAME, token, settings))
            await send(message)

        return wrapped


class SecurityHeadersMiddleware:
    """The four headers from PRD §19.1 item 5, on every response."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def wrapped(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers.setdefault("x-content-type-options", "nosniff")
                headers.setdefault("x-frame-options", "DENY")
                headers.setdefault("referrer-policy", "no-referrer")
                # HSTS is only meaningful over TLS, and pinning it from a local
                # http:// origin poisons the browser's cache for localhost.
                if self.settings.cookie_secure:
                    headers.setdefault(
                        "strict-transport-security", "max-age=31536000; includeSubDomains"
                    )
            await send(message)

        await self.app(scope, receive, wrapped)


def _sets_csrf_cookie(message: Message) -> bool:
    """Whether the route already issued its own CSRF cookie.

    `/auth/login`, `/auth/password` and `/invites/{token}/accept` all mint a
    token bound to the session they just created. Appending the middleware's
    default as well would send two `Set-Cookie: csrf=` headers, the browser would
    keep the last, and the value the client was handed in the body would no
    longer match the cookie — a guaranteed 403 on its first write.
    """
    return any(
        key.lower() == b"set-cookie" and value.lower().startswith(b"csrf=")
        for key, value in message.get("headers", [])
    )


def _cookie_header(name: str, value: str, settings: Settings) -> str:
    cookie: SimpleCookie = SimpleCookie()
    cookie[name] = value
    morsel = cookie[name]
    kwargs = csrf_cookie_kwargs(settings)
    morsel["path"] = kwargs["path"]
    morsel["max-age"] = str(kwargs["max_age"])
    morsel["samesite"] = "Lax"
    if kwargs["secure"]:
        morsel["secure"] = True
    return morsel.OutputString()


def _constant_time_equal(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode(), right.encode())


def client_ip(request: Request) -> str | None:
    """The browser's address, or None if nothing trustworthy is on the request.

    `api` has no public ingress: every request arrives through the `web`
    service's rewrite, so `request.client.host` is always a peer container. The
    real address is whatever the proxy put at the head of `X-Forwarded-For`.
    Anything that does not parse as an IP is dropped rather than stored — the
    `audit_log.ip` column is `INET` and a spoofed header should cost a NULL, not
    a failed insert.
    """
    forwarded = request.headers.get("x-forwarded-for")
    candidate = forwarded.split(",")[0].strip() if forwarded else None
    if not candidate and request.client is not None:
        candidate = request.client.host
    if not candidate:
        return None
    try:
        return str(ip_address(candidate))
    except ValueError:
        return None


def user_agent(request: Request) -> str | None:
    value = request.headers.get("user-agent")
    return value[:255] if value else None
