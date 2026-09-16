"""One Redis connection pool per process.

Sessions, the login rate limiter and (from P1) the run locks all reach Redis on
the request path, so a pool is created once and shared rather than dialled per
call.
"""

from __future__ import annotations

from functools import lru_cache

from redis.asyncio import Redis

from agent.config import get_settings


@lru_cache(maxsize=1)
def get_redis() -> Redis:
    client: Redis = Redis.from_url(get_settings().redis_url, decode_responses=False)
    return client


async def close_redis() -> None:
    if get_redis.cache_info().currsize:
        await get_redis().aclose()
