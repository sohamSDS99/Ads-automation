"""The audit log, the session list, and changing your own password."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog, User
from tests.integration.conftest import ADMIN_PASSWORD, ApiClient, build_client, make_member

NEW_PASSWORD = "thistle-marrow-71-kiln"  # noqa: S105 — a test fixture's password


# --- audit -------------------------------------------------------------------


async def test_the_audit_log_names_the_actor_and_is_newest_first(
    admin: ApiClient, db: AsyncSession
) -> None:
    await make_member(admin, "viewer")
    response = await admin.get("/audit")
    assert response.status_code == 200
    entries = response.json()["entries"]
    assert entries

    timestamps = [e["created_at"] for e in entries]
    assert timestamps == sorted(timestamps, reverse=True)
    invited = next(e for e in entries if e["action"] == "user.invited")
    assert invited["actor_email"] == "admin@example.com"
    assert invited["target_type"] == "invite"


async def test_the_audit_log_filters_by_action_and_actor(
    admin: ApiClient, db: AsyncSession
) -> None:
    await make_member(admin, "operator")
    filtered = (await admin.get("/audit?action=user.invited")).json()["entries"]
    assert filtered
    assert {e["action"] for e in filtered} == {"user.invited"}

    admin_id = (
        await db.execute(sa.select(User.id).where(User.email == "admin@example.com"))
    ).scalar_one()
    by_actor = (await admin.get(f"/audit?actor={admin_id}")).json()["entries"]
    assert by_actor
    assert all(e["actor_id"] == str(admin_id) for e in by_actor)


async def test_the_audit_log_paginates_without_repeats_or_gaps(admin: ApiClient) -> None:
    """Rows written in one transaction share a timestamp, so the cursor carries the id too."""
    for index in range(6):
        await admin.post(
            "/users/invite",
            json={"email": f"page{index}@example.com", "name": "P", "role": "viewer"},
        )

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):
        query = f"/audit?limit=3{f'&cursor={cursor}' if cursor else ''}"
        body = (await admin.get(query)).json()
        seen.extend(e["id"] for e in body["entries"])
        cursor = body["next_cursor"]
        if not cursor:
            break

    assert len(seen) == len(set(seen)), "a row was returned on two pages"
    everything = (await admin.get("/audit?limit=200")).json()["entries"]
    assert set(seen) == {e["id"] for e in everything}


async def test_a_forged_cursor_is_rejected(admin: ApiClient) -> None:
    assert (await admin.get("/audit?cursor=not-base64-at-all")).status_code == 400


async def test_a_failed_login_is_audited_with_no_actor(
    client: ApiClient, workspace: object, admin: ApiClient, db: AsyncSession
) -> None:
    await client.login("ghost@example.com", "some-wrong-password")
    row = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == "user.login_failed"))
    ).scalar_one()
    assert row.actor_id is None
    assert row.meta["email"] == "ghost@example.com"


# --- sessions ----------------------------------------------------------------


async def test_the_session_list_never_contains_a_session_id(admin: ApiClient) -> None:
    """The cookie value must not be readable back out of the API."""
    cookie = admin.raw.cookies.get("ara_session")
    assert cookie

    body = (await admin.get("/auth/sessions")).json()
    assert len(body["sessions"]) == 1
    assert body["sessions"][0]["current"] is True
    assert cookie not in (await admin.get("/auth/sessions")).text
    assert len(body["sessions"][0]["id"]) == 16


async def test_signing_in_twice_shows_two_sessions(admin: ApiClient, workspace: object) -> None:
    other = build_client()
    async with other.raw:
        await other.login("admin@example.com", ADMIN_PASSWORD)
        sessions = (await admin.get("/auth/sessions")).json()["sessions"]
        assert len(sessions) == 2
        assert [s["current"] for s in sessions].count(True) == 1


async def test_revoking_another_session_ends_it(admin: ApiClient, workspace: object) -> None:
    other = build_client()
    async with other.raw:
        await other.login("admin@example.com", ADMIN_PASSWORD)
        sessions = (await admin.get("/auth/sessions")).json()["sessions"]
        victim = next(s for s in sessions if not s["current"])

        assert (await admin.delete(f"/auth/sessions/{victim['id']}")).status_code == 204
        assert (await other.get("/auth/me")).status_code == 401
        assert (await admin.get("/auth/me")).status_code == 200


async def test_you_cannot_revoke_someone_elses_session(admin: ApiClient, db: AsyncSession) -> None:
    email, password = await make_member(admin, "operator")
    victim = build_client()
    async with victim.raw:
        await victim.login(email, password)
        their_handle = (await victim.get("/auth/sessions")).json()["sessions"][0]["id"]

        # The admin's own list does not contain it, so the handle resolves to nothing.
        assert (await admin.delete(f"/auth/sessions/{their_handle}")).status_code == 404
        assert (await victim.get("/auth/me")).status_code == 200


async def test_an_unknown_session_handle_is_a_404(admin: ApiClient) -> None:
    assert (await admin.delete("/auth/sessions/0123456789abcdef")).status_code == 404


# --- password ----------------------------------------------------------------


async def test_changing_your_password_keeps_you_signed_in_and_ends_every_other_session(
    admin: ApiClient, workspace: object
) -> None:
    other = build_client()
    async with other.raw:
        await other.login("admin@example.com", ADMIN_PASSWORD)

        response = await admin.post(
            "/auth/password",
            json={"current_password": ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
        )
        assert response.status_code == 204

        assert (await admin.get("/auth/me")).status_code == 200
        assert (await other.get("/auth/me")).status_code == 401

    fresh = build_client()
    async with fresh.raw:
        assert (await fresh.login("admin@example.com", NEW_PASSWORD)).status_code == 200
        assert (await fresh.login("admin@example.com", ADMIN_PASSWORD)).status_code == 401


async def test_the_wrong_current_password_is_refused(admin: ApiClient) -> None:
    response = await admin.post(
        "/auth/password",
        json={"current_password": "not-the-current-one", "new_password": NEW_PASSWORD},
    )
    assert response.status_code == 403
    assert response.json()["title"] == "Current password incorrect"


async def test_a_weak_new_password_is_refused(admin: ApiClient) -> None:
    response = await admin.post(
        "/auth/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": "password1234"},
    )
    assert response.status_code == 422
    assert "common" in response.json()["detail"]


async def test_the_change_is_audited(admin: ApiClient, db: AsyncSession) -> None:
    await admin.post(
        "/auth/password",
        json={"current_password": ADMIN_PASSWORD, "new_password": NEW_PASSWORD},
    )
    row = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == "user.password_changed"))
    ).scalar_one()
    assert row.actor_id is not None
    assert NEW_PASSWORD not in str(row.meta)
