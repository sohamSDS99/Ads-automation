"""The authz matrix: 4 roles x every guarded route.

PRD §19.1 acceptance: "for all 4 roles x every mutating route, an unauthorized
call returns 403 and performs no write."

The route table below is checked against the application's own routers, so a new
guarded route cannot be added without an entry here — the coverage test fails
until it is.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth.rbac import Permission, has_permission
from agent.db.models import AuditLog, Invite, User, UserRole, Workspace
from tests.integration.conftest import ApiClient

ROLES = ("admin", "operator", "approver", "viewer")

#: (method, router path, request path, permission, json body).
#: The router path is what `check_route_guards` sees; the request path is what
#: the test actually calls, with `{target}` filled in with a real user id.
GUARDED_ROUTES: tuple[tuple[str, str, str, Permission, dict[str, object] | None], ...] = (
    ("GET", "/auth/me", "/auth/me", Permission.READ, None),
    ("GET", "/auth/sessions", "/auth/sessions", Permission.READ, None),
    ("GET", "/users", "/users", Permission.READ, None),
    ("GET", "/workspace", "/workspace", Permission.READ, None),
    ("GET", "/audit", "/audit", Permission.AUDIT_READ, None),
    ("POST", "/auth/logout", "/auth/logout", Permission.READ, None),
    (
        "POST",
        "/auth/password",
        "/auth/password",
        Permission.READ,
        {"current_password": "quarry-lantern-98-fog", "new_password": "thistle-marrow-71-kiln"},
    ),
    (
        "DELETE",
        "/auth/sessions/{session_id}",
        "/auth/sessions/deadbeefdeadbeef",
        Permission.READ,
        None,
    ),
    (
        "POST",
        "/users/invite",
        "/users/invite",
        Permission.USER_MANAGE,
        {"email": "matrix-probe@example.com", "name": "Probe", "role": "viewer"},
    ),
    (
        "PATCH",
        "/users/{user_id}",
        "/users/{target}",
        Permission.USER_MANAGE,
        {"role": "viewer"},
    ),
    ("PATCH", "/workspace", "/workspace", Permission.SETTINGS_WRITE, {"name": "Renamed"}),
    (
        "POST",
        "/projects/{project_id}/runs",
        "/projects/{project}/runs",
        Permission.RUN_EXECUTE,
        {},
    ),
    ("GET", "/runs/{run_id}", "/runs/{run}", Permission.READ, None),
    ("GET", "/runs/{run_id}/events", "/runs/{run}/events", Permission.READ, None),
    (
        "GET",
        "/runs/{run_id}/nodes/{node_id}",
        "/runs/{run}/nodes/0.1",
        Permission.READ,
        None,
    ),
    ("POST", "/runs/{run_id}/cancel", "/runs/{run}/cancel", Permission.RUN_EXECUTE, None),
    (
        "POST",
        "/runs/{run_id}/retry-failed",
        "/runs/{run}/retry-failed",
        Permission.RUN_EXECUTE,
        None,
    ),
    # P7. Checking in as a viewer of a run is a POST that grants nothing: it
    # writes a 30-second presence mark and reads the set back, so `read` is the
    # permission a `viewer` watching a console needs to hold.
    ("POST", "/runs/{run_id}/presence", "/runs/{run}/presence", Permission.READ, None),
    # P2. The CSV routes take multipart, not JSON — which is fine here: the
    # permission dependency is resolved before the body is, so a role without
    # `project_write` gets its 403 without the request ever being parsed.
    ("GET", "/evidence", "/evidence", Permission.READ, None),
    ("GET", "/connectors", "/connectors", Permission.READ, None),
    # P7. The creative gallery reads a capture off the worker's Volume.
    (
        "GET",
        "/evidence/{evidence_id}/screenshot",
        "/evidence/{target}/screenshot",
        Permission.READ,
        None,
    ),
    (
        "POST",
        "/projects/{project_id}/sources/csv",
        "/projects/{target}/sources/csv",
        Permission.PROJECT_WRITE,
        None,
    ),
    (
        "POST",
        "/projects/{project_id}/sources/csv/preview",
        "/projects/{target}/sources/csv/preview",
        Permission.PROJECT_WRITE,
        None,
    ),
    # The business-context library. Reading it is READ — a viewer looking at a
    # project should be able to see what the run was told — and writing it is
    # PROJECT_WRITE, the same permission that governs the CSV import above.
    (
        "GET",
        "/projects/{project_id}/documents",
        "/projects/{target}/documents",
        Permission.READ,
        None,
    ),
    (
        "POST",
        "/projects/{project_id}/documents",
        "/projects/{target}/documents",
        Permission.PROJECT_WRITE,
        None,
    ),
    (
        "DELETE",
        "/projects/{project_id}/documents/{document_id}",
        "/projects/{target}/documents/{target}",
        Permission.PROJECT_WRITE,
        None,
    ),
    # P5a. Reports and exports are READ for every role — PRD §4.1 grants both
    # "read reports" and "export PDF/DOCX/CSV/JSON" to all four, so a `viewer`
    # may export. The POST is guarded by READ on purpose: it creates a job, so
    # it cannot be a GET, but it grants nothing a GET would not.
    ("GET", "/reports/{run_id}", "/reports/{run}", Permission.READ, None),
    (
        "POST",
        "/reports/{run_id}/export",
        "/reports/{run}/export?format=md",
        Permission.READ,
        None,
    ),
    ("GET", "/exports/{export_id}", "/exports/{run}", Permission.READ, None),
    (
        "GET",
        "/exports/{export_id}/download",
        "/exports/{run}/download",
        Permission.READ,
        None,
    ),
    # P3. The inbox is readable by every role — PRD §4.1 gives `viewer` the
    # run history, and a gate is part of it — but only `approval_decide`
    # holders may act, which excludes `operator` as well as `viewer`.
    ("GET", "/approvals", "/approvals", Permission.READ, None),
    (
        "POST",
        "/approvals/{approval_id}",
        "/approvals/{run}",
        Permission.APPROVAL_DECIDE,
        {"decision": "approve"},
    ),
    (
        "PATCH",
        "/approvals/{approval_id}/assignee",
        "/approvals/{run}/assignee",
        Permission.APPROVAL_DECIDE,
        {"assignee_id": None},
    ),
    # P6. The credential routes declare `read` and then narrow by scope inside:
    # a shared credential needs `credential_write`, a personal one needs only a
    # session (PRD §13.4 H). The matrix cannot express that — every role holds
    # `read`, so every row below is skipped by the refusal test — so the scope
    # rules are covered directly in `test_credentials_api.py`.
    ("GET", "/projects", "/projects", Permission.READ, None),
    (
        "POST",
        "/projects",
        "/projects",
        Permission.PROJECT_WRITE,
        {"name": "Matrix probe", "domain": "example.com"},
    ),
    ("GET", "/projects/{project_id}", "/projects/{project}", Permission.READ, None),
    (
        "PATCH",
        "/projects/{project_id}",
        "/projects/{project}",
        Permission.PROJECT_WRITE,
        {"name": "Matrix probe"},
    ),
    ("GET", "/projects/{project_id}/runs", "/projects/{project}/runs", Permission.READ, None),
    ("GET", "/credentials", "/credentials", Permission.READ, None),
    (
        "POST",
        "/credentials",
        "/credentials",
        Permission.READ,
        {"kind": "openrouter", "values": {"api_key": "sk-or-matrix-probe"}},
    ),
    (
        "POST",
        "/credentials/{credential_id}/test",
        "/credentials/{target}/test",
        Permission.READ,
        None,
    ),
    ("DELETE", "/credentials/{credential_id}", "/credentials/{target}", Permission.READ, None),
    # The Google Ads consent pair declares `read` and narrows to
    # `credential_write` inside, like every other route that writes a shared
    # credential. `test_google_ads_oauth_api.py` covers that narrowing.
    (
        "POST",
        "/credentials/google-ads/authorize",
        "/credentials/google-ads/authorize",
        Permission.READ,
        {"developer_token": "matrix-probe", "return_to": "/settings"},
    ),
    (
        "GET",
        "/credentials/google-ads/callback",
        "/credentials/google-ads/callback",
        Permission.READ,
        None,
    ),
    ("GET", "/models", "/models", Permission.SETTINGS_WRITE, None),
    # P8. Schedules are written by settings holders rather than by run
    # operators: a schedule is a standing instruction to spend unattended, which
    # is a different act from launching one run you are present for.
    ("GET", "/schedules", "/schedules", Permission.READ, None),
    (
        "POST",
        "/schedules/preview",
        "/schedules/preview",
        Permission.READ,
        {"cron": "0 3 * * *", "timezone": "UTC"},
    ),
    (
        "POST",
        "/schedules",
        "/schedules",
        Permission.SETTINGS_WRITE,
        {"project_id": "00000000-0000-0000-0000-000000000000", "cron": "0 3 * * *"},
    ),
    (
        "PATCH",
        "/schedules/{schedule_id}",
        "/schedules/{run}",
        Permission.SETTINGS_WRITE,
        {"enabled": False},
    ),
    ("DELETE", "/schedules/{schedule_id}", "/schedules/{run}", Permission.SETTINGS_WRITE, None),
    ("GET", "/runs/{run_id}/diff", "/runs/{run}/diff", Permission.READ, None),
    # Admin-only for the same reason `GET /models` is: its only consumer is the
    # admin settings screen.
    ("GET", "/storage", "/storage", Permission.SETTINGS_WRITE, None),
)

MUTATING = tuple(row for row in GUARDED_ROUTES if row[0] != "GET")


async def snapshot(db: AsyncSession) -> tuple[object, ...]:
    """Everything a forbidden call must leave untouched."""
    rows = []
    for model in (User, Invite, Workspace, AuditLog):
        result = await db.execute(sa.select(sa.func.count()).select_from(model))
        rows.append(result.scalar_one())
    users = await db.execute(sa.select(User.id, User.role, User.status).order_by(User.email))
    names = await db.execute(sa.select(Workspace.name))
    return (*rows, tuple(users.all()), tuple(names.all()))


@pytest.mark.parametrize(("method", "router_path", "path", "permission", "body"), MUTATING)
@pytest.mark.parametrize("role", ROLES)
async def test_a_role_without_the_permission_is_refused_and_writes_nothing(
    admin: ApiClient,
    signed_in_as: object,
    db: AsyncSession,
    role: str,
    method: str,
    router_path: str,
    path: str,
    permission: Permission,
    body: dict[str, object] | None,
) -> None:
    caller = admin if role == "admin" else await signed_in_as(role)  # type: ignore[operator]
    target = (await db.execute(sa.select(User.id).where(User.role == UserRole.ADMIN))).scalar_one()
    # The run and project ids are deliberately fictional: `require(Permission)`
    # is a dependency, so a forbidden caller is refused before any lookup. A 403
    # that depended on the row existing would not be proving authorization.
    url = path.format(target=target, project=uuid.uuid4(), run=uuid.uuid4())

    if has_permission(UserRole(role), permission):
        pytest.skip(f"{role} legitimately holds {permission.value}")

    before = await snapshot(db)
    response = await getattr(caller, method.lower())(url, json=body)

    assert response.status_code == 403, f"{role} {method} {url} -> {response.status_code}"
    assert response.headers["content-type"].startswith("application/problem+json")
    problem = response.json()
    assert problem["missing_permission"] == permission.value
    assert permission.value in problem["detail"]

    db.expire_all()
    assert await snapshot(db) == before, f"{role} {method} {url} changed state despite the 403"


@pytest.mark.parametrize(("method", "router_path", "path", "permission", "body"), GUARDED_ROUTES)
async def test_every_guarded_route_rejects_an_anonymous_caller(
    client: ApiClient,
    workspace: object,
    method: str,
    router_path: str,
    path: str,
    permission: Permission,
    body: dict[str, object] | None,
) -> None:
    url = path.format(target=uuid.uuid4(), project=uuid.uuid4(), run=uuid.uuid4())
    response = await getattr(client, method.lower())(url, json=body)
    assert response.status_code == 401
    assert response.json()["type"] == "/problems/unauthenticated"


@pytest.mark.parametrize("role", ROLES)
async def test_a_role_with_the_permission_is_allowed_through(
    admin: ApiClient, signed_in_as: object, db: AsyncSession, role: str
) -> None:
    """The mirror image: READ routes must actually work for every role."""
    caller = admin if role == "admin" else await signed_in_as(role)  # type: ignore[operator]
    for path in ("/auth/me", "/auth/sessions", "/users", "/workspace"):
        response = await caller.get(path)
        assert response.status_code == 200, f"{role} GET {path} -> {response.text}"

    me = (await caller.get("/auth/me")).json()
    assert me["role"] == role
    expected = {p.value for p in Permission if has_permission(UserRole(role), p)}
    assert set(me["permissions"]) == expected


def test_the_matrix_covers_every_guarded_route_in_the_application() -> None:
    """A new guarded route must arrive with its row in GUARDED_ROUTES."""
    import importlib

    from check_route_guards import PUBLIC_ROUTES, declared_permission, route_modules
    from fastapi.routing import APIRoute

    live: set[tuple[str, str]] = set()
    for module_name in route_modules():
        router = importlib.import_module(module_name).router
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            for method in route.methods - {"HEAD", "OPTIONS"}:
                if declared_permission(route) is not None:
                    live.add((method, route.path))

    covered = {(method, router_path) for method, router_path, _, _, _ in GUARDED_ROUTES}
    assert not live - covered, (
        f"guarded routes with no row in GUARDED_ROUTES: {sorted(live - covered)}"
    )
    assert not covered - live, (
        f"GUARDED_ROUTES names routes that do not exist: {sorted(covered - live)}"
    )
    assert not (live & PUBLIC_ROUTES)
