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
