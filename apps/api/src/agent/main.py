"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from agent import __version__
from agent.api import API_PREFIX as _API_PREFIX
from agent.api.logging_middleware import RequestContextMiddleware
from agent.api.middleware import (
    SecurityHeadersMiddleware,
    SessionMiddleware,
    WriteThrottleMiddleware,
)
from agent.api.problems import install_problem_handlers
from agent.api.routes_amendments import router as amendments_router
from agent.api.routes_approvals import router as approvals_router
from agent.api.routes_audit import router as audit_router
from agent.api.routes_auth import router as auth_router
from agent.api.routes_claims import router as claims_router
from agent.api.routes_connections import router as connections_router
from agent.api.routes_creative import router as creative_router
from agent.api.routes_creative_runs import router as creative_runs_router
from agent.api.routes_documents import router as documents_router
from agent.api.routes_evidence import router as evidence_router
from agent.api.routes_governance import router as governance_router
from agent.api.routes_guidelines import router as guidelines_router
from agent.api.routes_health import router as health_router
from agent.api.routes_invites import router as invites_router
from agent.api.routes_media import router as media_router
from agent.api.routes_media_library import router as media_library_router
from agent.api.routes_models import router as models_router
from agent.api.routes_plan import router as plan_router
from agent.api.routes_platform import router as platform_router
from agent.api.routes_projects import router as projects_router
from agent.api.routes_reports import router as reports_router
from agent.api.routes_runs import router as runs_router
from agent.api.routes_schedules import router as schedules_router
from agent.api.routes_sources import router as sources_router
from agent.api.routes_tasks import router as tasks_router
from agent.api.routes_users import router as users_router
from agent.api.routes_workspace import router as workspace_router
from agent.api.worker_files import close_worker_client
from agent.auth.bootstrap import bootstrap_from_environment
from agent.config import Settings, get_settings
from agent.creative.constants import get_creative_constants
from agent.db.session import dispose_engine, get_sessionmaker
from agent.guardrails.matchers import lexicon
from agent.logging_setup import configure_logging
from agent.planning.constants import get_planning_constants
from agent.queue import close_arq_pool
from agent.redis_client import close_redis

API_PREFIX = _API_PREFIX


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    log = structlog.get_logger(__name__)
    log.info("api.startup", version=__version__)

    # Deliberately outside the try below, and deliberately before anything else:
    # global law 15 says a planning constant without a `source` fails startup,
    # and a process that came up anyway would go on to write plans citing a
    # threshold nobody can check. `ConstantsError` names the key.
    constants = get_planning_constants()
    log.info("planning.constants.loaded", version=constants.version)
    # Stage 04's law 25, same place and same reason: `CreativeConstantsError`
    # names the key that has no source, and the process does not come up.
    creative = get_creative_constants()
    log.info("creative.constants.loaded", version=creative.version)

    # The linter's lemmatiser loads its dictionary on first use; a person
    # editing copy in the Ad Studio should not be the one who waits for it.
    try:
        lexicon.warm()
    except Exception as exc:  # noqa: BLE001 — a slower first lint, not a reason to stay down
        log.warning("lexicon.warm_skipped", error=str(exc))

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

    # Innermost of the stack, so it runs with the session already resolved and
    # can meter a user rather than an address.
    app.add_middleware(WriteThrottleMiddleware, settings=settings)

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

    # Order matters, and Starlette applies these outermost-last. The request
    # context is therefore the outermost of the three: a request rejected for
    # CSRF, before any session exists, still gets a log line and a request id.
    # The security headers then wrap everything below them, including the
    # problem+json responses the session layer emits on that CSRF failure.
    app.add_middleware(SessionMiddleware, settings=settings)
    app.add_middleware(SecurityHeadersMiddleware, settings=settings)
    app.add_middleware(RequestContextMiddleware)

    install_problem_handlers(app)

    for router in (
        health_router,
        auth_router,
        invites_router,
        users_router,
        audit_router,
        workspace_router,
        platform_router,
        projects_router,
        connections_router,
        models_router,
        runs_router,
        schedules_router,
        approvals_router,
        evidence_router,
        sources_router,
        documents_router,
        reports_router,
        plan_router,
        guidelines_router,
        creative_router,
        creative_runs_router,
        media_router,
        media_library_router,
        claims_router,
        tasks_router,
        governance_router,
        amendments_router,
    ):
        app.include_router(router, prefix=API_PREFIX)
    return app


app = create_app()
