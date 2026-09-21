"""Fixtures for the suite that needs a real Postgres and a real Redis.

Run it with `make test-integration`, which is `docker compose run --rm test`.
It cannot run on the host: `api`, `postgres` and `redis` publish no host ports
(PRD §5.2, and `tests/test_deployment_config.py` enforces it), so the only place
those hostnames resolve is inside the compose network.

The suite owns its own database, `agent_test`, because it truncates every table
between tests. Pointing it at the database `make up` gave you would empty it.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

API_ROOT = Path(__file__).resolve().parents[2]

# `scripts/check_route_guards.py` is imported by the authz coverage test.
sys.path.insert(0, str(API_ROOT / "scripts"))

# Captured at import time: the root conftest's autouse fixture scrubs DATABASE_*
# and REDIS_* out of the environment before every test, and these are the values
# the compose `test` service actually set.
REAL_DATABASE_URL = os.environ.get("DATABASE_URL", "")
REAL_REDIS_URL = os.environ.get("REDIS_URL", "")

TABLES = (
    "audit_log",
    "plan_calc",
    "campaign_plan",
    "research_acceptance",
    "export",
    "report",
    "approval",
    "evidence",
    "node_run",
    "run",
    "schedule",
    "credential",
    "project",
    "invite",
    "membership",
    '"user"',  # `user` is a reserved word in Postgres
    "workspace",
)

ADMIN_PASSWORD = "quarry-lantern-98-fog"  # noqa: S105 — a test fixture's password


def _requires_stack() -> None:
    if "postgres" not in REAL_DATABASE_URL or not REAL_REDIS_URL:
        pytest.exit(
            "The integration suite needs the compose stack. Run `make test-integration`, "
            f"not plain pytest. (DATABASE_URL={REAL_DATABASE_URL!r})",
            returncode=1,
        )


@pytest.fixture(scope="session", autouse=True)
def migrated_database() -> None:
    """Create `agent_test` if it is missing, then bring it to head.

    Uses the same `alembic upgrade head` Railway runs in `preDeployCommand`, so
    the suite exercises the real migration path rather than `create_all`, which
    would silently paper over a missing enum or index.
    """
    _requires_stack()
    import asyncio

    import asyncpg

    target = REAL_DATABASE_URL.rsplit("/", 1)[-1]
    maintenance = REAL_DATABASE_URL.rsplit("/", 1)[0] + "/agent"

    async def create() -> None:
        conn = await asyncpg.connect(maintenance)
        try:
            exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", target)
            if not exists:
                await conn.execute(f'CREATE DATABASE "{target}"')
        finally:
            await conn.close()

    asyncio.run(create())

    result = subprocess.run(  # noqa: S603
        ["alembic", "upgrade", "head"],  # noqa: S607
        cwd=API_ROOT,
        env={**os.environ, "DATABASE_URL": REAL_DATABASE_URL},
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        pytest.exit(f"alembic upgrade head failed:\n{result.stdout}\n{result.stderr}", returncode=1)


@pytest.fixture(autouse=True)
def stack_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Put the real stack URLs back after the root conftest scrubbed them.

    Runs after the root autouse fixture because child-directory conftest
    fixtures are set up last.
    """
    monkeypatch.setenv("DATABASE_URL", REAL_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", REAL_REDIS_URL)
    monkeypatch.setenv("APP_BASE_URL", "http://localhost:3000")
    monkeypatch.setenv("COOKIE_SECURE", "false")
    # The model key is the deployment's now, not a row. Runs resolve it from
    # here, so a suite without it is a suite where every run refuses to start
    # for a reason that has nothing to do with what is being tested.
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-key")
    monkeypatch.delenv("BOOTSTRAP_ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("BOOTSTRAP_ADMIN_PASSWORD", raising=False)

    from agent.config import get_settings

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest_asyncio.fixture(autouse=True)
async def clean_state(stack_environment: None) -> AsyncIterator[None]:
    """Empty every table and the Redis test database before each test."""
    from agent.db.session import get_sessionmaker
    from agent.redis_client import get_redis

    async with get_sessionmaker()() as session:
        await session.execute(
            sa.text(f"TRUNCATE TABLE {', '.join(TABLES)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    await get_redis().flushdb()
    yield


@pytest_asyncio.fixture
async def db() -> AsyncIterator[AsyncSession]:
    from agent.db.session import get_sessionmaker

    async with get_sessionmaker()() as session:
        yield session


class ApiClient:
    """An httpx client that behaves like the browser the API expects.

    Cookies are kept, and every mutating call carries `X-CSRF-Token` taken from
    the `csrf` cookie — which is exactly what `lib/api.ts` does on the frontend.
    `raw` is the same client without that courtesy, for the tests that are about
    CSRF itself.
    """

    PREFIX = "/api/v1"

    def __init__(self, client: AsyncClient) -> None:
        self.raw = client

    async def _csrf(self) -> str:
        token = self.raw.cookies.get("csrf")
        if not token:
            await self.raw.get(f"{self.PREFIX}/auth/csrf")
            token = self.raw.cookies.get("csrf")
        return token or ""

    async def get(self, path: str, **kwargs: Any) -> httpx.Response:
        # The table-driven authz tests pass `json=` uniformly for every method.
        kwargs.pop("json", None)
        return await self.raw.get(f"{self.PREFIX}{path}", **kwargs)

    async def _mutate(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        headers = {"X-CSRF-Token": await self._csrf(), **kwargs.pop("headers", {})}
        return await self.raw.request(method, f"{self.PREFIX}{path}", headers=headers, **kwargs)

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._mutate("POST", path, **kwargs)

    async def patch(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._mutate("PATCH", path, **kwargs)

    async def delete(self, path: str, **kwargs: Any) -> httpx.Response:
        return await self._mutate("DELETE", path, **kwargs)

    async def login(self, email: str, password: str) -> httpx.Response:
        return await self.post("/auth/login", json={"email": email, "password": password})

    async def logout(self) -> None:
        await self.post("/auth/logout")
        self.raw.cookies.clear()


def build_client(overrides: dict[Any, Any] | None = None) -> ApiClient:
    from agent.main import create_app

    app = create_app()
    # FastAPI resolves a dependency when the route is declared, so patching the
    # module attribute afterwards changes nothing. `dependency_overrides` is the
    # only hook that actually swaps one out.
    for dependency, replacement in (overrides or {}).items():
        app.dependency_overrides[dependency] = replacement
    transport = ASGITransport(app=app)
    return ApiClient(AsyncClient(transport=transport, base_url="http://testserver"))


@pytest.fixture
def api_dependency_overrides() -> dict[Any, Any]:
    """Dependency overrides applied to every app a test builds.

    Empty by default. A module that needs one — a client pointed at an
    in-process file server, say — redefines this fixture.
    """
    return {}


@pytest_asyncio.fixture
async def client(api_dependency_overrides: dict[Any, Any]) -> AsyncIterator[ApiClient]:
    api = build_client(api_dependency_overrides)
    async with api.raw:
        yield api


@pytest_asyncio.fixture
async def second_client(api_dependency_overrides: dict[Any, Any]) -> AsyncIterator[ApiClient]:
    """A second browser, with its own cookie jar."""
    api = build_client(api_dependency_overrides)
    async with api.raw:
        yield api


class Workspace:
    """A bootstrapped workspace and a factory for members of any role."""

    def __init__(self, admin_email: str) -> None:
        self.admin_email = admin_email
        self.admin_password = ADMIN_PASSWORD


@pytest_asyncio.fixture
async def workspace(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Workspace]:
    """Bootstrap the workspace with one active admin."""
    from agent.auth.bootstrap import create_first_admin
    from agent.db.session import get_sessionmaker

    email = "admin@example.com"
    async with get_sessionmaker()() as session:
        await create_first_admin(session, email=email, password=ADMIN_PASSWORD, name="Admin")
        await session.commit()
    yield Workspace(email)


@pytest_asyncio.fixture
async def admin(client: ApiClient, workspace: Workspace) -> ApiClient:
    """A client already signed in as the workspace admin."""
    response = await client.login(workspace.admin_email, workspace.admin_password)
    assert response.status_code == 200, response.text
    return client


async def make_member(admin: ApiClient, role: str, *, email: str | None = None) -> tuple[str, str]:
    """Invite and accept one member of `role`. Returns (email, password)."""
    address = email or f"{role}-{uuid.uuid4().hex[:8]}@example.com"
    created = await admin.post(
        "/users/invite", json={"email": address, "name": role.title(), "role": role}
    )
    assert created.status_code == 201, created.text
    token = created.json()["link"].rsplit("/", 1)[-1]

    joiner = build_client()
    async with joiner.raw:
        accepted = await joiner.post(
            f"/invites/{token}/accept",
            json={"name": role.title(), "password": ADMIN_PASSWORD},
        )
        assert accepted.status_code == 200, accepted.text
    return address, ADMIN_PASSWORD


@pytest_asyncio.fixture
async def signed_in_as(admin: ApiClient) -> AsyncIterator[Any]:
    """Factory: `await signed_in_as("operator")` returns a client signed in as one."""
    clients: list[AsyncClient] = []

    async def factory(role: str) -> ApiClient:
        email, password = await make_member(admin, role)
        api = build_client()
        clients.append(api.raw)
        response = await api.login(email, password)
        assert response.status_code == 200, response.text
        return api

    yield factory
    for raw in clients:
        await raw.aclose()


# ---------------------------------------------------------------------------
# P1: projects, credentials and runs
#
# There is no project or credential API yet — PRD §14 assigns both to the
# frontend phases — so the rows a run needs are created directly. When P6 ships
# those routes these fixtures should start going through them instead.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def admin_user(db: AsyncSession, workspace: Workspace) -> Any:
    """The bootstrapped admin row, for fixtures that need a `created_by`."""
    from agent.db.models import User

    result = await db.execute(sa.select(User).where(User.email == workspace.admin_email))
    return result.scalar_one()


@pytest_asyncio.fixture
async def project(db: AsyncSession, admin_user: Any, workspace_id: uuid.UUID) -> Any:
    """One project with OpenRouter connected — the minimum a run needs.

    The key itself is not here and cannot be: it is read from
    `Settings.openrouter_api_key`, which `tests/conftest.py` sets for the whole
    suite. What the fixture owns is the workspace's decision to use it.
    """
    from agent.db.models import CredentialKind, Project, SourceConnection

    row = Project(
        workspace_id=workspace_id,
        created_by=admin_user.id,
        name="SDS Manager",
        domain="sdsmanager.com",
        product_context={"pitch": "safety data sheet management"},
        # With the currency: `Market` requires one, so a fixture without it is a
        # project whose own detail response cannot be built. Nothing noticed
        # until a test read this project back instead of overwriting it first.
        markets=[{"country": "US", "language": "en", "currency": "USD"}],
        settings={},
    )
    db.add(row)
    db.add(
        SourceConnection(
            id=uuid.uuid4(),
            workspace_id=workspace_id,
            kind=CredentialKind.OPENROUTER,
            connected_by=admin_user.id,
        )
    )
    await db.commit()
    return row


@pytest.fixture(autouse=True)
def fast_llm_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Take the gateway's real rate limit out of the test clock.

    `agent.llm.gateway` throttles to 4 requests/second process-wide, which is
    right in production and adds ten seconds to a suite that deliberately drives
    the retry ladder. The limiter itself is unit-tested; here it only gets in
    the way.
    """
    from agent.llm.gateway import RateLimiter

    monkeypatch.setattr("agent.llm.gateway._LIMITER", RateLimiter(rate=10_000, concurrency=16))


# --- P2: evidence -------------------------------------------------------
#
# Built on P1's `project` fixture rather than a second one of my own — the
# evidence tests want an id, and the runs tests want the row, but there should
# only ever be one project in a test's workspace.


@pytest_asyncio.fixture
async def project_id(project: Any) -> uuid.UUID:
    """P1's project, as the id the evidence layer takes."""
    return project.id


@pytest_asyncio.fixture
async def second_project_id(
    db: AsyncSession, admin_user: Any, workspace_id: uuid.UUID
) -> uuid.UUID:
    """A second project in the same workspace, for cross-project dedupe tests."""
    from agent.db.models import Project

    row = Project(
        workspace_id=workspace_id,
        created_by=admin_user.id,
        name="Second Product",
        domain="second.example",
    )
    db.add(row)
    await db.commit()
    return row.id


@pytest_asyncio.fixture
async def workspace_id(db: AsyncSession, workspace: Workspace) -> uuid.UUID:
    """The bootstrapped workspace's id.

    Read from `workspace` rather than from the admin: the account stopped
    carrying a workspace when membership took over, and a fixture that went
    looking for one on it would be asking the wrong table.
    """
    from agent.db.models import Workspace as WorkspaceRow

    result = await db.execute(sa.select(WorkspaceRow).order_by(WorkspaceRow.created_at).limit(1))
    return result.scalar_one().id


# ---------------------------------------------------------------------------
# plan runs
# ---------------------------------------------------------------------------
#
# These two live here rather than in `plan_gates.py` beside the helpers they go
# with: a fixture imported into a test module collides with the parameter that
# requests it, so a shared fixture has to be somewhere pytest reads by itself.


@pytest.fixture
def fake_openrouter() -> Any:
    from tests.openrouter_fake import FakeOpenRouter

    return FakeOpenRouter()


@pytest_asyncio.fixture
async def accepted(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """A finished research run, accepted, with the CRM and account history."""
    import json

    from agent.db.models import Report, Run, RunStage, RunStatus, RunTrigger
    from agent.export.contract import ResearchReport
    from tests.integration.plan_gates import LOST, WON, seed_account
    from tests.integration.runs_support import seed_crm
    from tests.report_support import golden_payload

    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])

    run = Run(
        workspace_id=workspace_id,
        project_id=project.id,
        triggered_by=user_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.RESEARCH,
    )
    db.add(run)
    await db.flush()

    payload = golden_payload()
    payload["project_id"] = str(project.id)
    payload["run_id"] = str(run.id)
    payload["launch_readiness"] = "go"
    parsed = ResearchReport.model_validate(payload)
    db.add(
        Report(
            run_id=run.id,
            schema_version="1.0",
            payload=json.loads(parsed.model_dump_json()),
            markdown="# report",
        )
    )
    await db.commit()

    await seed_crm(project.id, won=WON, lost=LOST)
    await seed_account(project.id)
    response = await admin.post(f"/runs/{run.id}/accept", json={})
    assert response.status_code == 201, response.text
    return {
        "project_id": project.id,
        "research_run_id": run.id,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }
