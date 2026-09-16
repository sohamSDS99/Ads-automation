"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent import __version__
from agent.api.middleware import SecurityHeadersMiddleware, SessionMiddleware
from agent.api.problems import install_problem_handlers
from agent.api.routes_audit import router as audit_router
from agent.api.routes_auth import router as auth_router
from agent.api.routes_evidence import router as evidence_router
from agent.api.routes_health import router as health_router
from agent.api.routes_invites import router as invites_router
from agent.api.routes_reports import close_worker_client
from agent.api.routes_reports import router as reports_router
from agent.api.routes_runs import router as runs_router
from agent.api.routes_sources import router as sources_router
from agent.api.routes_users import router as users_router
from agent.api.routes_workspace import router as workspace_router
from agent.auth.bootstrap import bootstrap_from_environment
from agent.config import Settings, get_settings
from agent.db.session import dispose_engine, get_sessionmaker
from agent.logging_setup import configure_logging
from agent.queue import close_arq_pool
from agent.redis_client import close_redis

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log = structlog.get_logger(__name__)
    log.info("api.startup", version=__version__)

    # First boot creates the workspace and its admin. A database that is not
    # migrated yet must not stop the process: Railway runs migrations in
    # `preDeployCommand`, and locally the container starts before `make migrate`.
    try:
        async with get_sessionmaker()() as session:
            await bootstrap_from_environment(session, get_settings())
    except Exception as exc:  # noqa: BLE001 — startup must survive an unmigrated DB
        log.warning("bootstrap.skipped", error=str(exc))

    yield
    await close_worker_client()
    await close_arq_pool()
    await close_redis()
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

    # Order matters: the security headers wrap everything, including the
    # problem+json responses the session layer emits on a CSRF failure.
    app.add_middleware(SessionMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)

    install_problem_handlers(app)

    for router in (
        health_router,
        auth_router,
        invites_router,
        users_router,
        audit_router,
        workspace_router,
        runs_router,
        evidence_router,
        sources_router,
        reports_router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    return app


app = create_app()
