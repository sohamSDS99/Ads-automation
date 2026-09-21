"""Re-roling, disabling, the last-admin rule, and what a change does to live sessions."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog, Membership, User, UserRole, UserStatus
from tests.integration.conftest import ApiClient, build_client, make_member


async def user_id(db: AsyncSession, email: str) -> object:
    return (await db.execute(sa.select(User.id).where(User.email == email))).scalar_one()


async def test_an_admin_can_change_a_role(admin: ApiClient, db: AsyncSession) -> None:
    email, _ = await make_member(admin, "viewer")
    target = await user_id(db, email)

    response = await admin.patch(f"/users/{target}", json={"role": "operator"})
    assert response.status_code == 200
    assert response.json()["role"] == "operator"

    db.expire_all()
    row = (
        await db.execute(
            sa.select(Membership)
            .join(User, User.id == Membership.user_id)
            .where(User.email == email)
        )
    ).scalar_one()
    assert row.role is UserRole.OPERATOR


async def test_disabling_a_user_invalidates_their_session_on_the_next_request(
    admin: ApiClient, db: AsyncSession
) -> None:
    """ACCEPTANCE: disabling a user invalidates their session on their next request."""
    email, password = await make_member(admin, "operator")

    victim = build_client()
    async with victim.raw:
        assert (await victim.login(email, password)).status_code == 200
        assert (await victim.get("/auth/me")).status_code == 200

        target = await user_id(db, email)
        assert (
            await admin.patch(f"/users/{target}", json={"status": "disabled"})
        ).status_code == 200

        # No sleep, no expiry wait: the sessions were revoked inside the request.
        assert (await victim.get("/auth/me")).status_code == 401


async def test_a_role_change_also_ends_the_targets_sessions(
    admin: ApiClient, db: AsyncSession
) -> None:
    """A live session must never keep the permissions its owner just lost."""
    email, password = await make_member(admin, "operator")
    victim = build_client()
    async with victim.raw:
        await victim.login(email, password)
        target = await user_id(db, email)
        await admin.patch(f"/users/{target}", json={"role": "viewer"})
        assert (await victim.get("/auth/me")).status_code == 401

        await victim.login(email, password)
        assert (await victim.get("/auth/me")).json()["permissions"] == ["read"]


async def test_the_last_active_admin_cannot_be_demoted(admin: ApiClient, db: AsyncSession) -> None:
    """ACCEPTANCE: demoting the last active admin returns 409 and leaves the row unchanged."""
    target = await user_id(db, "admin@example.com")

    response = await admin.patch(f"/users/{target}", json={"role": "operator"})
    assert response.status_code == 409
    assert response.json()["title"] == "Last admin"

    db.expire_all()
    row = (await db.execute(sa.select(Membership).where(Membership.user_id == target))).scalar_one()
    assert row.role is UserRole.ADMIN
    assert row.status is UserStatus.ACTIVE


async def test_the_last_active_admin_cannot_be_disabled(admin: ApiClient, db: AsyncSession) -> None:
    target = await user_id(db, "admin@example.com")
    response = await admin.patch(f"/users/{target}", json={"status": "disabled"})
    assert response.status_code == 409

    db.expire_all()
    row = (await db.execute(sa.select(Membership).where(Membership.user_id == target))).scalar_one()
    assert row.status is UserStatus.ACTIVE


async def test_an_admin_can_be_demoted_once_a_second_one_exists(
    admin: ApiClient, db: AsyncSession
) -> None:
    second_email, _ = await make_member(admin, "admin")
    target = await user_id(db, "admin@example.com")

    response = await admin.patch(f"/users/{target}", json={"role": "operator"})
    assert response.status_code == 200
    assert response.json()["role"] == "operator"

    db.expire_all()
    remaining = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Membership)
            .where(Membership.role == UserRole.ADMIN, Membership.status == UserStatus.ACTIVE)
        )
    ).scalar_one()
    assert remaining == 1


async def test_a_disabled_admin_does_not_count_towards_the_last_admin_rule(
    admin: ApiClient, db: AsyncSession
) -> None:
    """Two admins, one disabled, leaves one — so the other still cannot be demoted."""
    second_email, _ = await make_member(admin, "admin")
    second = await user_id(db, second_email)
    assert (await admin.patch(f"/users/{second}", json={"status": "disabled"})).status_code == 200

    first = await user_id(db, "admin@example.com")
    assert (await admin.patch(f"/users/{first}", json={"role": "viewer"})).status_code == 409


async def test_status_invited_cannot_be_assigned(admin: ApiClient, db: AsyncSession) -> None:
    email, _ = await make_member(admin, "viewer")
    target = await user_id(db, email)
    response = await admin.patch(f"/users/{target}", json={"status": "invited"})
    assert response.status_code == 422


async def test_an_empty_patch_is_rejected(admin: ApiClient, db: AsyncSession) -> None:
    email, _ = await make_member(admin, "viewer")
    target = await user_id(db, email)
    assert (await admin.patch(f"/users/{target}", json={})).status_code == 422


async def test_patching_an_unknown_user_is_a_404(admin: ApiClient) -> None:
    import uuid

    assert (await admin.patch(f"/users/{uuid.uuid4()}", json={"role": "viewer"})).status_code == 404


async def test_every_change_writes_an_audit_row_naming_the_actor(
    admin: ApiClient, db: AsyncSession
) -> None:
    email, _ = await make_member(admin, "viewer")
    target = await user_id(db, email)
    await admin.patch(f"/users/{target}", json={"role": "operator", "status": "disabled"})

    rows = (
        (
            await db.execute(
                sa.select(AuditLog).where(
                    AuditLog.action.in_(["user.role_changed", "user.status_changed"])
                )
            )
        )
        .scalars()
        .all()
    )
    assert {r.action for r in rows} == {"user.role_changed", "user.status_changed"}
    admin_id = await user_id(db, "admin@example.com")
    assert all(r.actor_id == admin_id for r in rows)
    assert all(r.target_id == target for r in rows)


async def test_a_no_op_patch_changes_nothing_and_keeps_the_session(
    admin: ApiClient, db: AsyncSession
) -> None:
    """Re-submitting the current values must not log the user out."""
    email, password = await make_member(admin, "operator")
    victim = build_client()
    async with victim.raw:
        await victim.login(email, password)
        target = await user_id(db, email)
        response = await admin.patch(f"/users/{target}", json={"role": "operator"})
        assert response.status_code == 200
        assert (await victim.get("/auth/me")).status_code == 200

    count = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(AuditLog)
            .where(AuditLog.action == "user.role_changed")
        )
    ).scalar_one()
    assert count == 0
