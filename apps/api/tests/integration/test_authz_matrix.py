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
from agent.db.models import AuditLog, Invite, Membership, User, UserRole, Workspace
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
    # The budget what-if is `READ`, not `APPROVAL_DECIDE`, and deliberately so:
    # it advances nothing, calls no model and changes no plan, so a `viewer`
    # may follow the working behind a decision it cannot make. Deciding the
    # gate is the row above and needs the permission that row names.
    (
        "POST",
        "/approvals/{approval_id}/recalc",
        "/approvals/{run}/recalc",
        Permission.READ,
        {
            "allocation": [
                {
                    "campaign_ref": "brand",
                    "market": "GB",
                    "funnel_stage": "bofu",
                    "usd": 1000,
                }
            ]
        },
    ),
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
    (
        "POST",
        "/projects/{project_id}/autofill",
        "/projects/{project}/autofill",
        Permission.PROJECT_WRITE,
        {"fields": ["site_url"]},
    ),
    # Connections. Switching a source on or off is `credential_write` at the
    # decorator now, not a scope rule narrowed inside the handler — nothing is
    # stored per-scope any more, so there is no personal credential for the
    # matrix to be unable to express. Reading the list and testing a source are
    # `read`: neither returns a secret, and the person watching a run skip a
    # source is often not the person who can reconnect it.
    ("GET", "/connections", "/connections", Permission.READ, None),
    (
        "POST",
        "/connections/{kind}/connect",
        "/connections/openrouter/connect",
        Permission.CREDENTIAL_WRITE,
        None,
    ),
    (
        "DELETE",
        "/connections/{kind}",
        "/connections/openrouter",
        Permission.CREDENTIAL_WRITE,
        None,
    ),
    (
        "POST",
        "/connections/{kind}/test",
        "/connections/openrouter/test",
        Permission.READ,
        None,
    ),
    # Signing in to Google is `read`, deliberately. What it hands over is the
    # caller's own Google account, and the developer token it joins is the
    # deployment's — so the alternative is one administrator minting refresh
    # tokens on a laptop for everybody else, which is the arrangement that left
    # this source unconnected. Deleting the workspace's connection still needs
    # `credential_write`, because that one stops everybody's runs.
    (
        "POST",
        "/connections/google/authorize",
        "/connections/google/authorize",
        Permission.READ,
        {"return_to": "/settings/connections"},
    ),
    (
        "GET",
        "/connections/google/callback",
        "/connections/google/callback",
        Permission.READ,
        None,
    ),
    (
        "POST",
        "/connections/{kind}/account",
        "/connections/google_ads/account",
        Permission.READ,
        {"customer_id": "4445556660"},
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
    # Multi-workspace. Removing someone from a workspace is `user_manage`, the
    # same as changing their role; everything that reaches *across* workspaces
    # is `platform_admin`, which no role grants. Those rows are the ones that
    # matter here: they are what proves one company's admin cannot create,
    # rename, archive or enumerate another company's workspace.
    ("DELETE", "/users/{user_id}", "/users/{target}", Permission.USER_MANAGE, None),
    (
        "POST",
        "/users/{user_id}/invite",
        "/users/{target}/invite",
        Permission.USER_MANAGE,
        None,
    ),
    (
        "POST",
        "/platform/accounts/{user_id}/invite",
        "/platform/accounts/{target}/invite",
        Permission.PLATFORM_ADMIN,
        {"workspace_id": "00000000-0000-0000-0000-000000000000"},
    ),
    ("GET", "/workspaces", "/workspaces", Permission.READ, None),
    (
        "POST",
        "/auth/workspace",
        "/auth/workspace",
        Permission.READ,
        {"workspace_id": "00000000-0000-0000-0000-000000000000"},
    ),
    (
        "POST",
        "/workspaces",
        "/workspaces",
        Permission.PLATFORM_ADMIN,
        {"name": "Matrix Probe Workspace"},
    ),
    (
        "PATCH",
        "/workspaces/{workspace_id}",
        "/workspaces/{workspace}",
        Permission.PLATFORM_ADMIN,
        {"name": "Renamed By Probe"},
    ),
    (
        "DELETE",
        "/workspaces/{workspace_id}",
        "/workspaces/{workspace}",
        Permission.PLATFORM_ADMIN,
        None,
    ),
    (
        "POST",
        "/workspaces/{workspace_id}/restore",
        "/workspaces/{workspace}/restore",
        Permission.PLATFORM_ADMIN,
        None,
    ),
    ("GET", "/platform/accounts", "/platform/accounts", Permission.PLATFORM_ADMIN, None),
    (
        "POST",
        "/platform/accounts",
        "/platform/accounts",
        Permission.PLATFORM_ADMIN,
        {
            "email": "matrix-platform-probe@example.com",
            "workspace_id": "00000000-0000-0000-0000-000000000000",
            "role": "viewer",
        },
    ),
    (
        "PATCH",
        "/platform/accounts/{user_id}",
        "/platform/accounts/{target}",
        Permission.PLATFORM_ADMIN,
        {"status": "disabled"},
    ),
    # Stage 02, S2-P0. Accepting research reuses APPROVAL_DECIDE — it is an
    # approval-class act, so the people who decided the research gates are the
    # people who sign off that the report is fit to plan from — while starting
    # a plan run is PLAN_EXECUTE, which `approver` does not hold and `operator`
    # does. That asymmetry is the whole reason the two are separate rows.
    (
        "POST",
        "/runs/{research_run_id}/accept",
        "/runs/{run}/accept",
        Permission.APPROVAL_DECIDE,
        {},
    ),
    (
        "DELETE",
        "/runs/{research_run_id}/accept",
        "/runs/{run}/accept",
        Permission.APPROVAL_DECIDE,
        None,
    ),
    (
        "GET",
        "/projects/{project_id}/plan/eligibility",
        "/projects/{project}/plan/eligibility",
        Permission.READ,
        None,
    ),
    (
        "POST",
        "/projects/{project_id}/plan/runs",
        "/projects/{project}/plan/runs",
        Permission.PLAN_EXECUTE,
        None,
    ),
    # Stage 03. `GUIDELINE_EXECUTE` and not `PLAN_EXECUTE`: an approver may
    # publish a guideline and may never start one, which is the same asymmetry
    # Stage 02 drew between running a plan and freezing it.
    (
        "GET",
        "/projects/{project_id}/guidelines/eligibility",
        "/projects/{project}/guidelines/eligibility",
        Permission.READ,
        None,
    ),
    (
        "POST",
        "/projects/{project_id}/guidelines/runs",
        "/projects/{project}/guidelines/runs",
        Permission.GUIDELINE_EXECUTE,
        {},
    ),
    (
        "GET",
        "/projects/{project_id}/guidelines",
        "/projects/{project}/guidelines",
        Permission.READ,
        None,
    ),
    # Stage 04 (S4-P0). Eligibility is READ — every role may ask whether the
    # project is ready — and starting is CREATIVE_EXECUTE: admin and operator,
    # never approver or viewer (PRD §5.1). The body is a valid text-only
    # scope, so the 403 is the guard's and not the validator's.
    (
        "GET",
        "/projects/{project_id}/creative/eligibility",
        "/projects/{project}/creative/eligibility",
        Permission.READ,
        None,
    ),
    (
        "POST",
        "/projects/{project_id}/creative/runs",
        "/projects/{project}/creative/runs",
        Permission.CREATIVE_EXECUTE,
        {"scope": {"images": False, "video": False, "concepts_per_campaign": 2}},
    ),
    (
        "GET",
        "/projects/{project_id}/creative",
        "/projects/{project}/creative",
        Permission.READ,
        None,
    ),
    (
        "GET",
        "/guidelines/published/creative-context",
        "/guidelines/published/creative-context?project_id=00000000-0000-4000-8000-000000000000",
        Permission.READ,
        None,
    ),
    (
        "GET",
        "/guidelines/{guideline_id}",
        "/guidelines/00000000-0000-0000-0000-000000000000",
        Permission.READ,
        None,
    ),
    # -- S3-P3: the claims register and the non-delegable signature ---------
    #
    # `/auth/reauth` is READ-guarded because every signed-in account may prove
    # its own presence; what it mints is useless without CLAIM_SIGN *and* being
    # the named legal owner, both asserted on the signing route.
    ("POST", "/auth/reauth", "/auth/reauth", Permission.READ, {"password": "x"}),
    (
        "GET",
        "/guidelines/{guideline_id}/claims",
        "/guidelines/00000000-0000-0000-0000-000000000000/claims",
        Permission.READ,
        None,
    ),
    (
        "PATCH",
        "/guidelines/{guideline_id}/claims/{claim_id}",
        "/guidelines/00000000-0000-0000-0000-000000000000/claims/00000000-0000-0000-0000-000000000000",
        Permission.GUIDELINE_EXECUTE,
        {},
    ),
    (
        "POST",
        "/guidelines/{guideline_id}/claims/sign",
        "/guidelines/00000000-0000-0000-0000-000000000000/claims/sign",
        Permission.CLAIM_SIGN,
        {"decisions": [], "statement": "x", "set_hash": "x", "reauth_token": "x"},
    ),
    (
        "POST",
        "/claims/{claim_id}/revoke",
        "/claims/00000000-0000-0000-0000-000000000000/revoke",
        Permission.CLAIM_SIGN,
        {"reason": "x"},
    ),
    (
        "GET",
        "/claims/{claim_id}/signature",
        "/claims/00000000-0000-0000-0000-000000000000/signature",
        Permission.READ,
        None,
    ),
    ("GET", "/projects/{project_id}/plans", "/projects/{project}/plans", Permission.READ, None),
    (
        "GET",
        "/plans/{plan_run_id}/calcs",
        "/plans/{run}/calcs",
        Permission.READ,
        None,
    ),
    ("GET", "/plans/{plan_run_id}", "/plans/{run}", Permission.READ, None),
    (
        "GET",
        "/plans/{plan_run_id}/structure",
        "/plans/{run}/structure",
        Permission.READ,
        None,
    ),
    (
        "GET",
        "/plans/{plan_run_id}/diff",
        # `against` is required, so the row carries one. Without it the route
        # answers 422 before the permission is ever checked, and the matrix
        # would be asserting on FastAPI's validator rather than on the guard.
        "/plans/{run}/diff?against=00000000-0000-4000-8000-0000000d1ff0",
        Permission.READ,
        None,
    ),
    # Every role may export a plan — §14 gives the deliverable to all four, and
    # the write-shaped verb is about where the work happens (a job row and a
    # file on the worker's volume), not about privilege.
    (
        "POST",
        "/plans/{plan_run_id}/export",
        "/plans/{run}/export?format=json",
        Permission.READ,
        None,
    ),
    # Freezing is the one thing an operator may not do. It runs plans; it does
    # not sign them, and that difference is the point of the four gates.
    (
        "POST",
        "/plans/{plan_run_id}/freeze",
        "/plans/{run}/freeze",
        Permission.PLAN_FREEZE,
        {"confirm_version": 1},
    ),
)

MUTATING = tuple(row for row in GUARDED_ROUTES if row[0] != "GET")


async def snapshot(db: AsyncSession) -> tuple[object, ...]:
    """Everything a forbidden call must leave untouched.

    Memberships are in the snapshot as well as accounts: the routes under test
    can now change a role, revoke access, archive a workspace or promote a
    system administrator, and three of those four leave `user` alone.
    """
    rows = []
    for model in (User, Invite, Workspace, AuditLog, Membership):
        result = await db.execute(sa.select(sa.func.count()).select_from(model))
        rows.append(result.scalar_one())
    accounts = await db.execute(
        sa.select(User.id, User.status, User.is_superadmin).order_by(User.email)
    )
    members = await db.execute(
        sa.select(
            Membership.user_id, Membership.workspace_id, Membership.role, Membership.status
        ).order_by(Membership.user_id, Membership.workspace_id)
    )
    spaces = await db.execute(sa.select(Workspace.name, Workspace.archived_at))
    return (*rows, tuple(accounts.all()), tuple(members.all()), tuple(spaces.all()))


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
    # Every role here is an *invited* member, including admin. The bootstrap
    # admin would not do: it is the system administrator, and a matrix that
    # tested `admin` through the one account holding `platform_admin` would
    # report that a workspace admin may reach every other workspace.
    caller = await signed_in_as(role)  # type: ignore[operator]
    target = (
        await db.execute(
            sa.select(User.id)
            .join(Membership, Membership.user_id == User.id)
            .where(Membership.role == UserRole.ADMIN)
            .order_by(User.created_at)
            .limit(1)
        )
    ).scalar_one()
    workspace_row = (
        await db.execute(sa.select(Workspace.id).order_by(Workspace.created_at).limit(1))
    ).scalar_one()
    # The run and project ids are deliberately fictional: `require(Permission)`
    # is a dependency, so a forbidden caller is refused before any lookup. A 403
    # that depended on the row existing would not be proving authorization.
    url = path.format(
        target=target, project=uuid.uuid4(), run=uuid.uuid4(), workspace=workspace_row
    )

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
    url = path.format(
        target=uuid.uuid4(), project=uuid.uuid4(), run=uuid.uuid4(), workspace=uuid.uuid4()
    )
    response = await getattr(client, method.lower())(url, json=body)
    assert response.status_code == 401
    assert response.json()["type"] == "/problems/unauthenticated"


@pytest.mark.parametrize("role", ROLES)
async def test_a_role_with_the_permission_is_allowed_through(
    admin: ApiClient, signed_in_as: object, db: AsyncSession, role: str
) -> None:
    """The mirror image: READ routes must actually work for every role."""
    caller = await signed_in_as(role)  # type: ignore[operator]
    for path in ("/auth/me", "/auth/sessions", "/users", "/workspace", "/workspaces"):
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
