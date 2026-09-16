"""The invite flow, end to end, and each of its three failure screens."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.auth.rbac import Permission, has_permission
from agent.db.models import AuditLog, Invite, User, UserRole, UserStatus
from tests.integration.conftest import ADMIN_PASSWORD, ApiClient, build_client

ROLES = ("admin", "operator", "approver", "viewer")


async def issue(admin: ApiClient, role: str, email: str) -> str:
    response = await admin.post(
        "/users/invite", json={"email": email, "name": role.title(), "role": role}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["link"].rsplit("/", 1)[-1])


@pytest.mark.parametrize("role", ROLES)
async def test_an_invited_member_lands_with_exactly_the_prd_permission_set(
    admin: ApiClient, role: str
) -> None:
    """ACCEPTANCE: admin invites one user of each role; each accepts and lands with §4.1."""
    # Not `{role}@example.com` — the bootstrap admin already owns admin@example.com.
    address = f"invited-{role}@example.com"
    token = await issue(admin, role, address)

    joiner = build_client()
    async with joiner.raw:
        preview = await joiner.get(f"/invites/{token}")
        assert preview.status_code == 200
        assert preview.json()["state"] == "valid"
        assert preview.json()["role"] == role
        assert preview.json()["email"] == address

        accepted = await joiner.post(
            f"/invites/{token}/accept", json={"name": "New Person", "password": ADMIN_PASSWORD}
        )
        assert accepted.status_code == 200, accepted.text
        body = accepted.json()
        assert body["role"] == role
        assert body["name"] == "New Person"
        assert set(body["permissions"]) == {
            p.value for p in Permission if has_permission(UserRole(role), p)
        }

        # Accepting signs them in, so the session is live immediately.
        assert (await joiner.get("/auth/me")).status_code == 200


async def test_the_token_is_never_stored_in_plaintext(admin: ApiClient, db: AsyncSession) -> None:
    token = await issue(admin, "viewer", "viewer@example.com")
    stored = (await db.execute(sa.select(Invite.token_hash))).scalar_one()
    assert token not in stored
    assert len(stored) == 64


async def test_an_invited_user_appears_as_pending_and_cannot_sign_in(
    admin: ApiClient, db: AsyncSession
) -> None:
    await issue(admin, "operator", "pending@example.com")
    listed = (await admin.get("/users")).json()["users"]
    pending = next(u for u in listed if u["email"] == "pending@example.com")
    assert pending["status"] == "invited"

    outsider = build_client()
    async with outsider.raw:
        assert (await outsider.login("pending@example.com", ADMIN_PASSWORD)).status_code == 401


async def test_a_token_can_only_be_used_once(admin: ApiClient) -> None:
    token = await issue(admin, "viewer", "once@example.com")
    first = build_client()
    async with first.raw:
        assert (
            await first.post(
                f"/invites/{token}/accept", json={"name": "A", "password": ADMIN_PASSWORD}
            )
        ).status_code == 200

    second = build_client()
    async with second.raw:
        replay = await second.post(
            f"/invites/{token}/accept", json={"name": "B", "password": ADMIN_PASSWORD}
        )
        assert replay.status_code == 410
        assert replay.json()["state"] == "accepted"
        assert (await second.get(f"/invites/{token}")).json()["state"] == "accepted"


async def test_an_expired_token_reports_expired(admin: ApiClient, db: AsyncSession) -> None:
    token = await issue(admin, "viewer", "stale@example.com")
    await db.execute(sa.update(Invite).values(expires_at=datetime.now(UTC) - timedelta(seconds=1)))
    await db.commit()

    joiner = build_client()
    async with joiner.raw:
        assert (await joiner.get(f"/invites/{token}")).json()["state"] == "expired"
        rejected = await joiner.post(
            f"/invites/{token}/accept", json={"name": "X", "password": ADMIN_PASSWORD}
        )
        assert rejected.status_code == 410
        assert rejected.json()["state"] == "expired"


async def test_an_unknown_token_reports_invalid_and_leaks_nothing(admin: ApiClient) -> None:
    joiner = build_client()
    async with joiner.raw:
        preview = await joiner.get("/invites/not-a-real-token")
        assert preview.status_code == 200
        body = preview.json()
        assert body["state"] == "invalid"
        assert body["email"] is None and body["role"] is None and body["workspace_name"] is None

        rejected = await joiner.post(
            "/invites/not-a-real-token/accept", json={"name": "X", "password": ADMIN_PASSWORD}
        )
        assert rejected.status_code == 404


async def test_a_weak_password_is_rejected_and_the_invite_stays_usable(
    admin: ApiClient, db: AsyncSession
) -> None:
    token = await issue(admin, "viewer", "weak@example.com")
    joiner = build_client()
    async with joiner.raw:
        rejected = await joiner.post(
            f"/invites/{token}/accept", json={"name": "X", "password": "password1234"}
        )
        assert rejected.status_code == 422
        assert "common" in rejected.json()["detail"]

        # Still open, so they can try again with a better one.
        assert (await joiner.get(f"/invites/{token}")).json()["state"] == "valid"
        assert (
            await joiner.post(
                f"/invites/{token}/accept", json={"name": "X", "password": ADMIN_PASSWORD}
            )
        ).status_code == 200


async def test_inviting_the_same_address_twice_is_refused(admin: ApiClient) -> None:
    """And the refusal says "invite open", not "already a member" — nobody accepted yet."""
    await issue(admin, "viewer", "dupe@example.com")
    again = await admin.post(
        "/users/invite", json={"email": "dupe@example.com", "name": "Dupe", "role": "viewer"}
    )
    assert again.status_code == 409
    assert again.json()["title"] == "Invite already open"


async def test_inviting_someone_who_already_accepted_says_they_are_a_member(
    admin: ApiClient,
) -> None:
    token = await issue(admin, "viewer", "joined@example.com")
    joiner = build_client()
    async with joiner.raw:
        await joiner.post(
            f"/invites/{token}/accept", json={"name": "J", "password": ADMIN_PASSWORD}
        )

    again = await admin.post(
        "/users/invite", json={"email": "joined@example.com", "name": "J", "role": "viewer"}
    )
    assert again.status_code == 409
    assert again.json()["title"] == "Already a member"


async def test_inviting_an_existing_member_is_refused(admin: ApiClient) -> None:
    response = await admin.post(
        "/users/invite", json={"email": "admin@example.com", "name": "Again", "role": "viewer"}
    )
    assert response.status_code == 409
    assert response.json()["title"] == "Already a member"


async def test_the_link_is_returned_when_smtp_is_not_configured(admin: ApiClient) -> None:
    """Without SMTP the admin copies the link; the API must hand it over."""
    response = await admin.post(
        "/users/invite", json={"email": "copy@example.com", "name": "Copy", "role": "viewer"}
    )
    body = response.json()
    assert body["email_delivered"] is False
    assert body["link"].startswith("http://localhost:3000/invite/")


async def test_both_halves_of_the_flow_are_audited(admin: ApiClient, db: AsyncSession) -> None:
    token = await issue(admin, "operator", "audited@example.com")
    joiner = build_client()
    async with joiner.raw:
        await joiner.post(
            f"/invites/{token}/accept", json={"name": "A", "password": ADMIN_PASSWORD}
        )

    actions = (await db.execute(sa.select(AuditLog.action))).scalars().all()
    assert "user.invited" in actions
    assert "user.invite_accepted" in actions


async def test_accepting_activates_the_pending_row_rather_than_creating_a_second(
    admin: ApiClient, db: AsyncSession
) -> None:
    token = await issue(admin, "approver", "single@example.com")
    joiner = build_client()
    async with joiner.raw:
        await joiner.post(
            f"/invites/{token}/accept", json={"name": "Solo", "password": ADMIN_PASSWORD}
        )

    rows = (
        (await db.execute(sa.select(User).where(User.email == "single@example.com")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status is UserStatus.ACTIVE
    assert rows[0].name == "Solo"
    assert rows[0].password_hash is not None
