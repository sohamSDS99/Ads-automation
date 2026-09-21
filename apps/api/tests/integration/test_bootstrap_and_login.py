"""Bootstrap, sign-in, and the lockout. The first four acceptance lines of PRD §19.1."""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog, Membership, User, UserRole, UserStatus, Workspace
from tests.integration.conftest import ADMIN_PASSWORD, ApiClient
from tests.integration.conftest import Workspace as Ws


async def actions(db: AsyncSession) -> list[str]:
    result = await db.execute(sa.select(AuditLog.action).order_by(AuditLog.created_at))
    return list(result.scalars().all())


async def test_startup_bootstrap_creates_the_workspace_and_one_admin(
    db: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agent.auth.bootstrap import bootstrap_from_environment
    from agent.config import Settings

    settings = Settings(
        bootstrap_admin_email="first@example.com",
        bootstrap_admin_password=ADMIN_PASSWORD,  # type: ignore[arg-type]
    )
    admin = await bootstrap_from_environment(db, settings)
    assert admin is not None
    assert admin.status is UserStatus.ACTIVE
    # The first account administers the whole installation, not just the
    # workspace it was handed: without this nobody could create the second one.
    assert admin.is_superadmin is True

    assert (await db.execute(sa.select(sa.func.count()).select_from(Workspace))).scalar_one() == 1
    membership = (
        await db.execute(sa.select(Membership).where(Membership.user_id == admin.id))
    ).scalar_one()
    assert membership.role is UserRole.ADMIN
    assert membership.status is UserStatus.ACTIVE
    assert "workspace.bootstrap" in await actions(db)


async def test_startup_bootstrap_is_a_no_op_once_a_user_exists(
    db: AsyncSession, workspace: Ws
) -> None:
    from agent.auth.bootstrap import bootstrap_from_environment
    from agent.config import Settings

    settings = Settings(
        bootstrap_admin_email="second@example.com",
        bootstrap_admin_password=ADMIN_PASSWORD,  # type: ignore[arg-type]
    )
    assert await bootstrap_from_environment(db, settings) is None
    count = (await db.execute(sa.select(sa.func.count()).select_from(User))).scalar_one()
    assert count == 1


async def test_bootstrap_route_returns_409_once_a_user_exists(
    client: ApiClient, workspace: Ws, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ACCEPTANCE: a second call to POST /auth/bootstrap returns 409."""
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAIL", "another@example.com")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_PASSWORD", ADMIN_PASSWORD)
    from agent.config import get_settings

    get_settings.cache_clear()

    response = await client.post("/auth/bootstrap", json={})
    assert response.status_code == 409
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["title"] == "Already bootstrapped"


async def test_a_weak_bootstrap_password_leaves_an_empty_database(db: AsyncSession) -> None:
    from agent.auth.bootstrap import bootstrap_from_environment
    from agent.config import Settings

    settings = Settings(
        bootstrap_admin_email="first@example.com",
        bootstrap_admin_password="password1234",  # type: ignore[arg-type]
    )
    assert await bootstrap_from_environment(db, settings) is None
    assert (await db.execute(sa.select(sa.func.count()).select_from(User))).scalar_one() == 0


async def test_admin_can_sign_in(client: ApiClient, workspace: Ws) -> None:
    """ACCEPTANCE: fresh DB + BOOTSTRAP_ADMIN_* → the admin can log in."""
    response = await client.login(workspace.admin_email, ADMIN_PASSWORD)
    assert response.status_code == 200
    body = response.json()
    assert body["email"] == workspace.admin_email
    assert body["role"] == "admin"
    assert body["is_superadmin"] is True
    assert set(body["permissions"]) == {
        "read",
        "project_write",
        "run_execute",
        "credential_write",
        "settings_write",
        "approval_decide",
        "user_manage",
        "audit_read",
        # Stage 02 (S2-P0). An admin holds both; the split between them is
        # `test_rbac`'s grid.
        "plan_execute",
        "plan_freeze",
        # The bootstrap account is the system administrator. A workspace admin
        # invited later holds the ten above and not this one — see
        # `test_authz_matrix`, which runs its `admin` row through an invited
        # account for exactly that reason.
        "platform_admin",
    }
    assert [w["name"] for w in body["workspaces"]] == [body["workspace_name"]]


async def test_the_session_cookie_is_httponly_and_the_csrf_cookie_is_not(
    client: ApiClient, workspace: Ws
) -> None:
    response = await client.login(workspace.admin_email, ADMIN_PASSWORD)
    cookies = response.headers.get_list("set-cookie")
    session_cookie = next(c for c in cookies if c.startswith("ara_session="))
    csrf_cookie = next(c for c in cookies if c.startswith("csrf="))

    assert "httponly" in session_cookie.lower()
    assert "samesite=lax" in session_cookie.lower()
    # The browser has to read this one to echo it back in X-CSRF-Token.
    assert "httponly" not in csrf_cookie.lower()


async def test_no_response_ever_carries_a_password_hash(client: ApiClient, workspace: Ws) -> None:
    await client.login(workspace.admin_email, ADMIN_PASSWORD)
    for path in ("/auth/me", "/users", "/workspace", "/auth/sessions"):
        body = (await client.get(path)).text
        assert "password_hash" not in body
        assert "$argon2" not in body
        assert "token_hash" not in body


@pytest.mark.parametrize(
    ("email", "password"),
    [
        ("admin@example.com", "wrong-password-entirely"),
        ("nobody@example.com", ADMIN_PASSWORD),
    ],
)
async def test_a_wrong_password_and_an_unknown_email_look_identical(
    client: ApiClient, workspace: Ws, email: str, password: str
) -> None:
    """PRD §6.1.5: the response must not say whether the account exists."""
    response = await client.login(email, password)
    assert response.status_code == 401
    assert response.json()["detail"] == "That email and password don't match an active account."


async def test_a_member_disabled_in_their_only_workspace_signs_in_to_nothing(
    admin: ApiClient, second_client: ApiClient, db: AsyncSession
) -> None:
    """403, not 401 — and no session either way.

    Disabling a *membership* is now a statement about one workspace, so the
    password is still correct and pretending otherwise would send someone who
    was simply moved off a team round the password-reset loop for an account
    that works. What they must not get is a session: the assertion that
    matters is the missing cookie, not the status code.
    """
    from tests.integration.conftest import make_member

    email, password = await make_member(admin, "viewer")
    target = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
    assert (
        await admin.patch(f"/users/{target.id}", json={"status": "disabled"})
    ).status_code == 200

    response = await second_client.login(email, password)
    assert response.status_code == 403
    assert response.json()["title"] == "No workspace"
    assert "ara_session" not in response.headers.get("set-cookie", "")
    assert (await second_client.get("/auth/me")).status_code == 401


async def test_a_disabled_account_cannot_sign_in_at_all(
    admin: ApiClient, second_client: ApiClient, db: AsyncSession
) -> None:
    """The platform-wide lock still looks exactly like a wrong password."""
    from agent.db.models import UserStatus as Status
    from tests.integration.conftest import make_member

    email, password = await make_member(admin, "viewer")
    target = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
    target.status = Status.DISABLED
    await db.commit()

    response = await second_client.login(email, password)
    assert response.status_code == 401
    assert response.json()["detail"] == "That email and password don't match an active account."


async def test_six_failed_logins_lock_the_account_out(
    client: ApiClient, workspace: Ws, db: AsyncSession
) -> None:
    """ACCEPTANCE: 6 failed logins in 15 min returns a lockout problem+json + an AuditLog row."""
    # Five attempts are allowed. Each is answered as a failed sign-in.
    for attempt in range(5):
        response = await client.login(workspace.admin_email, "definitely-not-it")
        assert response.status_code == 401, f"attempt {attempt + 1} should be a plain rejection"

    logged = await actions(db)
    assert logged.count("user.login_failed") == 5
    assert "user.login_locked" not in logged

    # The sixth is refused before the password is even looked at.
    sixth = await client.login(workspace.admin_email, "definitely-not-it")
    assert sixth.status_code == 429
    assert sixth.headers["content-type"].startswith("application/problem+json")
    body = sixth.json()
    assert body["type"] == "/problems/rate-limited"
    assert int(sixth.headers["Retry-After"]) > 0

    logged = await actions(db)
    assert logged.count("user.login_failed") == 5, "a locked-out attempt is not a new failure"
    assert logged.count("user.login_locked") == 1

    # And the correct password does not get you past the lock.
    assert (await client.login(workspace.admin_email, ADMIN_PASSWORD)).status_code == 429


async def test_a_successful_login_clears_the_counter(client: ApiClient, workspace: Ws) -> None:
    for _ in range(4):
        await client.login(workspace.admin_email, "wrong")
    assert (await client.login(workspace.admin_email, ADMIN_PASSWORD)).status_code == 200

    await client.logout()
    for _ in range(4):
        assert (await client.login(workspace.admin_email, "wrong")).status_code == 401


async def test_logout_ends_the_session_immediately(client: ApiClient, workspace: Ws) -> None:
    await client.login(workspace.admin_email, ADMIN_PASSWORD)
    assert (await client.get("/auth/me")).status_code == 200

    logout = await client.post("/auth/logout")
    assert logout.status_code == 204
    assert (await client.get("/auth/me")).status_code == 401
