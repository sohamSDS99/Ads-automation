"""GET /api/v1/health — the Railway healthcheck target and the web shell's status dot."""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Response, status
from redis.asyncio import Redis
from sqlalchemy import text

from agent import __version__
from agent.api.schemas import DependencyState, HealthResponse
from agent.config import get_settings
from agent.db.session import get_sessionmaker

log = structlog.get_logger(__name__)

router = APIRouter(tags=["health"])


async def _check_db() -> DependencyState:
    try:
        async with get_sessionmaker()() as session:
            await session.execute(text("SELECT 1"))
    except Exception as exc:  # noqa: BLE001 — health must never raise
        log.warning("health.db_unreachable", error=str(exc))
        return "error"
    return "ok"


async def _check_redis() -> DependencyState:
    client: Redis | None = None
    try:
        client = Redis.from_url(get_settings().redis_url)
        await client.ping()
    except Exception as exc:  # noqa: BLE001 — health must never raise
        log.warning("health.redis_unreachable", error=str(exc))
        return "error"
    finally:
        if client is not None:
            await client.aclose()
    return "ok"


@router.get("/health", response_model=HealthResponse, summary="Service health")
async def health(response: Response) -> HealthResponse:
    db_state = await _check_db()
    redis_state = await _check_redis()
    healthy = db_state == "ok" and redis_state == "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthResponse(
        status="ok" if healthy else "degraded",
        db=db_state,
        redis=redis_state,
        version=__version__,
    )
