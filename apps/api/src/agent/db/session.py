"""Async engine and session factory.

One engine per process, built lazily so that importing the models never opens a
connection (Alembic, the schema exporter and the tests all rely on that).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from agent.config import Settings, get_settings


@lru_cache(maxsize=1)
def get_engine() -> AsyncEngine:
    settings: Settings = get_settings()
    return create_async_engine(
        settings.async_database_url,
        pool_pre_ping=True,
        pool_size=5,
        max_overflow=10,
        echo=False,
    )


@lru_cache(maxsize=1)
def get_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(get_engine(), expire_on_commit=False, autoflush=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency yielding a session that rolls back on error."""
    async with get_sessionmaker()() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def dispose_engine() -> None:
    """Close the pool on shutdown."""
    if get_engine.cache_info().currsize:
        await get_engine().dispose()
