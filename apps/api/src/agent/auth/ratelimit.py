"""Login throttling: 5 attempts per 15 minutes per (email, IP). PRD §6.1.5.

Counting on the pair rather than on the IP alone is deliberate. Keying on IP
only would let one office NAT lock out a whole team; keying on email only would
let anyone lock a colleague out of their own account by failing six logins
against it.

The email is hashed into the key so a Redis dump does not double as a list of
who has an account here.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta

from redis.asyncio import Redis

MAX_ATTEMPTS = 5
WINDOW = timedelta(minutes=15)
KEY_PREFIX = "login_attempts:"


def _key(email: str, ip: str | None) -> str:
    digest = hashlib.sha256(email.strip().casefold().encode()).hexdigest()[:32]
    return f"{KEY_PREFIX}{digest}:{ip or 'unknown'}"


@dataclass(frozen=True, slots=True)
class LockoutState:
    locked: bool
    attempts: int
    retry_after_seconds: int


class LoginRateLimiter:
    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    async def check(self, email: str, ip: str | None) -> LockoutState:
        """Read the counter without touching it. Called before the password is verified."""
        key = _key(email, ip)
        pipe = self._redis.pipeline()
        pipe.get(key)
        pipe.ttl(key)
        raw, ttl = await pipe.execute()
        attempts = int(raw) if raw is not None else 0
        retry_after = max(int(ttl), 0) if ttl is not None else 0
        return LockoutState(
            locked=attempts >= MAX_ATTEMPTS,
            attempts=attempts,
            retry_after_seconds=retry_after or int(WINDOW.total_seconds()),
        )

    async def record_failure(self, email: str, ip: str | None) -> LockoutState:
        """Count one failed attempt and report where that leaves the caller."""
        key = _key(email, ip)
        pipe = self._redis.pipeline()
        pipe.incr(key)
        # Fixed window: the expiry is set from the first failure and not
        # extended, so a locked-out caller is always released 15 minutes after
        # their first miss rather than being held indefinitely by retrying.
        pipe.expire(key, WINDOW, nx=True)
        pipe.ttl(key)
        attempts, _, ttl = await pipe.execute()
        return LockoutState(
            locked=int(attempts) >= MAX_ATTEMPTS,
            attempts=int(attempts),
            retry_after_seconds=max(int(ttl), 0) if ttl is not None else 0,
        )

    async def clear(self, email: str, ip: str | None) -> None:
        """A successful login wipes the counter for that pair."""
        await self._redis.delete(_key(email, ip))


# ---------------------------------------------------------------------------
# General request throttling (P8)
# ---------------------------------------------------------------------------
#
# The login limiter above exists to stop password guessing and is deliberately
# harsh. This one exists for a different reason: to stop one client — a retry
# loop, a stuck tab, a script someone wrote against the API — from consuming a
# shared worker's capacity or a shared OpenRouter budget.
#
# So the limits are generous by design. A limit a person can reach by using the
# product is a bug, not a defence, and the two endpoints with a *low* ceiling
# are the two that cost real money per call.


@dataclass(frozen=True, slots=True)
class Quota:
    """A fixed window: `limit` calls per `window_seconds`, per caller."""

    name: str
    limit: int
    window_seconds: int


#: Every mutating request. Sized so a person cannot reach it: an optimistic UI
#: that saves on every keystroke would, which is why the wizard debounces.
WRITE_QUOTA = Quota("write", limit=120, window_seconds=60)

#: Launching a run. Each one can cost dollars and holds a worker slot for the
#: better part of an hour, and the run lock already permits exactly one per
#: project — this bounds someone cycling across projects.
RUN_QUOTA = Quota("run", limit=10, window_seconds=60)

#: Rendering an export. Cheap per call but each one is a worker job, and the
#: split-button makes five of them very easy to click.
EXPORT_QUOTA = Quota("export", limit=30, window_seconds=60)


@dataclass(frozen=True, slots=True)
class QuotaState:
    allowed: bool
    remaining: int
    retry_after_seconds: int


class RequestRateLimiter:
    """Fixed-window counters in Redis, one per (quota, caller)."""

    def __init__(self, redis: Redis) -> None:
        self._redis = redis

    @staticmethod
    def key(quota: Quota, caller: str) -> str:
        return f"ratelimit:{quota.name}:{caller}"

    async def consume(self, quota: Quota, caller: str) -> QuotaState:
        """Count this call and say whether it is allowed.

        Counts first and asks afterwards. Checking before incrementing would let
        two concurrent requests both read `limit - 1` and both proceed; over the
        window that is a leak of exactly the concurrency, which is the traffic
        shape a runaway client has.
        """
        key = self.key(quota, caller)
        pipe = self._redis.pipeline()
        pipe.incr(key)
        # `nx` so the window is anchored to the first call in it rather than
        # being extended by every call — a caller who keeps trying is released
        # on schedule instead of being held indefinitely by their own retries.
        pipe.expire(key, quota.window_seconds, nx=True)
        pipe.ttl(key)
        used, _, ttl = await pipe.execute()
        used = int(used)
        retry_after = max(int(ttl), 0) if ttl is not None else quota.window_seconds
        return QuotaState(
            allowed=used <= quota.limit,
            remaining=max(quota.limit - used, 0),
            retry_after_seconds=retry_after or quota.window_seconds,
        )
