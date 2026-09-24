"""Redis semaphores `media:image` (4) and `media:video` (2) (PRD §8.4, §9.5).

They bound how many submits are in flight across every worker at once. Each
slot is a **lease**: a member of a sorted set scored by its expiry, taken by
one Lua script that first drops expired leases. A worker that is killed while
holding a slot never releases it, and its lease simply runs out — which is the
property a `kill -9` needs and a plain counter cannot give.

Expiry is judged by the Redis server's own clock (`TIME`), so two workers with
skewed clocks agree on whose lease is alive.
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from agent.media.types import Modality

# KEYS[1] the set   ARGV: limit, token, lease ms
_ACQUIRE = """
local now = redis.call('TIME')
local ms = tonumber(now[1]) * 1000 + math.floor(tonumber(now[2]) / 1000)
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ms)
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[1]) then
  redis.call('ZADD', KEYS[1], ms + tonumber(ARGV[3]), ARGV[2])
  redis.call('PEXPIRE', KEYS[1], tonumber(ARGV[3]))
  return 1
end
return 0
"""


class SemaphoreTimeout(RuntimeError):
    """No slot came free in time."""


class MediaSemaphore:
    def __init__(
        self,
        redis: Any,
        modality: Modality,
        *,
        limit: int,
        lease_seconds: float,
        poll_seconds: float = 0.25,
    ) -> None:
        self._redis = redis
        self.key = f"media:{modality}"
        self._limit = limit
        self._lease_ms = int(lease_seconds * 1000)
        self._poll = poll_seconds
        self._acquire = redis.register_script(_ACQUIRE)

    async def acquire(self) -> str | None:
        """Take a slot now, or None if all are held."""
        token = secrets.token_hex(16)
        taken = await self._acquire(keys=[self.key], args=[self._limit, token, self._lease_ms])
        return token if taken else None

    async def release(self, token: str) -> None:
        await self._redis.zrem(self.key, token)

    @asynccontextmanager
    async def hold(self, *, timeout_seconds: float) -> AsyncIterator[None]:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout_seconds
        token = await self.acquire()
        while token is None:
            if loop.time() >= deadline:
                raise SemaphoreTimeout(
                    f"All {self._limit} {self.key} slots stayed busy for {timeout_seconds:g} s."
                )
            await asyncio.sleep(self._poll)
            token = await self.acquire()
        try:
            yield
        finally:
            await self.release(token)
