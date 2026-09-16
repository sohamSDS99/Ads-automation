"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent import __version__
from agent.api.routes_health import router as health_router
from agent.config import Settings, get_settings
from agent.db.session import dispose_engine
from agent.logging_setup import configure_logging

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log = structlog.get_logger(__name__)
    log.info("api.startup", version=__version__)
    yield
    await dispose_engine()
    log.info("api.shutdown")


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings)

    app = FastAPI(
        title="Paid Ads Research Agent",
        version=__version__,
        docs_url=f"{API_PREFIX}/docs",
        openapi_url=f"{API_PREFIX}/openapi.json",
        lifespan=lifespan,
    )

    # Local Compose only. In production the browser is same-origin with `web`
    # (next.config.ts rewrites /api/v1/* over the private network), so no
    # cross-origin request is ever made and this middleware matches nothing.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(health_router, prefix=API_PREFIX)
    return app


app = create_app()
