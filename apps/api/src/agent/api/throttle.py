"""Per-caller request quotas, as FastAPI dependencies.

A dependency rather than middleware, deliberately. Middleware would have to
guess which requests are expensive from the path, and the two calls that cost
real money — launching a run, rendering an export — would get the same ceiling
as reading a project list. Declaring the quota on the route puts the limit next
to the thing being limited, the same way `require(Permission)` puts the
authorization there.

The caller is the **session**, falling back to the IP for a request that has
none. Keying on the session is what makes the limit fair in an office: one NAT
is one address, and limiting by address would let one person's runaway script
throttle their whole team.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Annotated

import structlog
from fastapi import Depends, Request

from agent.api import problems
from agent.api.middleware import client_ip
from agent.auth.deps import REQUEST_STATE_SESSION, Principal, current_user
from agent.auth.ratelimit import Quota, QuotaState, RequestRateLimiter
from agent.auth.sessions import SessionRecord
from agent.redis_client import get_redis

log = structlog.get_logger(__name__)

#: Standard-ish response headers, so a client can back off before being refused
#: rather than by being refused.
LIMIT_HEADER = "x-ratelimit-limit"
REMAINING_HEADER = "x-ratelimit-remaining"


def caller_id(request: Request) -> str:
    """Who is being counted: the session if there is one, else the address."""
    record: SessionRecord | None = getattr(request.state, REQUEST_STATE_SESSION, None)
    if record is not None:
        return f"user:{record.user_id}"
    return f"ip:{client_ip(request) or 'unknown'}"


def throttle(quota: Quota) -> Callable[[Request, Principal], Awaitable[Principal]]:
    """Build the dependency that meters one route.

    Depends on `current_user` so the quota is charged *after* authentication:
    an unauthenticated caller is already refused by the guard, and counting them
    here would let an anonymous flood exhaust a real user's window.
    """

    async def _meter(
        request: Request, principal: Annotated[Principal, Depends(current_user)]
    ) -> Principal:
        state: QuotaState = await RequestRateLimiter(get_redis()).consume(
            quota, f"user:{principal.user.id}"
        )
        if not state.allowed:
            log.info(
                "ratelimit.refused",
                quota=quota.name,
                path=request.url.path,
                retry_after=state.retry_after_seconds,
            )
            raise problems.Problem(
                status_code=429,
                title="Too many requests",
                detail=(
                    f"This endpoint accepts {quota.limit} requests every "
                    f"{quota.window_seconds}s. Try again in {state.retry_after_seconds}s."
                ),
                type_=problems.TYPE_RATE_LIMITED,
                headers={
                    "retry-after": str(state.retry_after_seconds),
                    LIMIT_HEADER: str(quota.limit),
                    REMAINING_HEADER: "0",
                },
            )
        return principal

    return _meter
