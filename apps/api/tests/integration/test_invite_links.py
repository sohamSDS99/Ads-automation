"""Invitations delivered as links, which on this deployment is all of them.

There is no mail server in production, so the copyable link is not a
degradation of the email path — it *is* the path. That changes which failures
matter: a link the admin did not copy is a person who cannot be onboarded, and
before this there was no second chance, because the token is stored only as a
SHA-256 and cannot be looked up.

Two of the tests below are regressions for dead ends that existed and were
reachable in about thirty seconds of ordinary use.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog, Invite, Membership, User, UserStatus
from tests.integration.conftest import ADMIN_PASSWORD, ApiClient, make_member
from tests.integration.test_workspaces import SECOND_PASSWORD, accept, invite_token, make_workspace


async def pending_id(admin: ApiClient, email: str) -> str:
    users = (await admin.get("/users")).json()["users"]
    return str(next(u for u in users if u["email"] == email)["id"])


# ---------------------------------------------------------------------------
# reissuing a link
# ---------------------------------------------------------------------------


async def test_a_lost_link_can_be_reissued_and_the_old_one_dies(admin: ApiClient) -> None:
    """The property that makes reissuing safe rather than just convenient.

    Two live links to one account is one more than anybody intended, so the
    superseded token must stop working the instant the new one exists.
    """
    first = await admin.post("/users/invite", json={"email": "lost@example.com", "role": "viewer"})
    assert first.status_code == 201, first.text
    old_token = str(first.json()["link"]).rsplit("/", 1)[-1]

    again = await admin.post(f"/users/{await pending_id(admin, 'lost@example.com')}/invite")
    assert again.status_code == 200, again.text
    new_token = str(again.json()["link"]).rsplit("/", 1)[-1]
    assert new_token != old_token

    # The old link is not merely expired — it names nothing at all.
    assert (await admin.get(f"/invites/{old_token}")).json()["state"] == "invalid"
    assert (await admin.get(f"/invites/{new_token}")).json()["state"] == "valid"

    status_code, me = await accept(new_token, password=SECOND_PASSWORD, name="Found")
    assert status_code == 200, me
    assert me["email"] == "lost@example.com"


async def test_the_superseded_link_cannot_be_accepted(admin: ApiClient) -> None:
    first = await admin.post("/users/invite", json={"email": "race@example.com", "role": "viewer"})
    old_token = str(first.json()["link"]).rsplit("/", 1)[-1]
    await admin.post(f"/users/{await pending_id(admin, 'race@example.com')}/invite")

    status_code, body = await accept(old_token, password=SECOND_PASSWORD, name="Too Late")
    assert status_code == 404, body
    assert body["state"] == "invalid"


async def test_reissuing_is_refused_once_they_have_accepted(admin: ApiClient) -> None:
    """There is no link to reissue — they sign in with the password they set."""
    email, _ = await make_member(admin, "operator", email="settled@example.com")
    target = await pending_id(admin, email)

    refused = await admin.post(f"/users/{target}/invite")
    assert refused.status_code == 409
    assert refused.json()["title"] == "Already accepted"


async def test_reissuing_leaves_exactly_one_open_invite(admin: ApiClient, db: AsyncSession) -> None:
    """`uq_invite_open_email` enforces it, and the reissue must not trip on it."""
    await admin.post("/users/invite", json={"email": "tidy@example.com", "role": "viewer"})
    target = await pending_id(admin, "tidy@example.com")
    for _ in range(3):
        assert (await admin.post(f"/users/{target}/invite")).status_code == 200

    open_invites = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Invite)
            .where(Invite.email == "tidy@example.com", Invite.accepted_at.is_(None))
        )
    ).scalar_one()
    assert open_invites == 1


async def test_reissuing_is_recorded_as_its_own_action(admin: ApiClient, db: AsyncSession) -> None:
    """A credential event, not a repeat of the invitation."""
    await admin.post("/users/invite", json={"email": "logged@example.com", "role": "viewer"})
    await admin.post(f"/users/{await pending_id(admin, 'logged@example.com')}/invite")

    actions = list(
        (await db.execute(sa.select(AuditLog.action).order_by(AuditLog.created_at))).scalars().all()
    )
    assert "user.invite_reissued" in actions
    entry = (
        await db.execute(sa.select(AuditLog).where(AuditLog.action == "user.invite_reissued"))
    ).scalar_one()
    assert entry.meta["email"] == "logged@example.com"
    assert entry.meta["superseded"], "the log should name the link that was killed"


# ---------------------------------------------------------------------------
# the address that used to be burned forever
# ---------------------------------------------------------------------------


async def test_removing_a_pending_person_frees_their_address(
    admin: ApiClient, db: AsyncSession
) -> None:
    """Regression. `uq_invite_open_email` is unique on `(workspace, email)`
    where `accepted_at IS NULL`, so an invite left behind by a removal made
    every later invite to that address answer 409 forever, against a
    membership that no longer existed and no screen could show."""
    await admin.post("/users/invite", json={"email": "second@example.com", "role": "viewer"})
    target = await pending_id(admin, "second@example.com")
    assert (await admin.delete(f"/users/{target}")).status_code == 204

    orphans = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Invite)
            .where(Invite.email == "second@example.com", Invite.accepted_at.is_(None))
        )
    ).scalar_one()
    assert orphans == 0, "the removal must take the open invite with it"

    again = await admin.post(
        "/users/invite", json={"email": "second@example.com", "role": "operator"}
    )
    assert again.status_code == 201, again.text
    status_code, _ = await accept(
        str(again.json()["link"]).rsplit("/", 1)[-1], password=SECOND_PASSWORD, name="Second Go"
    )
    assert status_code == 200


# ---------------------------------------------------------------------------
# across workspaces
# ---------------------------------------------------------------------------


async def test_the_administrator_can_reissue_in_a_workspace_they_are_not_in(
    admin: ApiClient, db: AsyncSession
) -> None:
    finance = await make_workspace(admin, "Finance")
    created = await admin.post(
        "/platform/accounts",
        json={"email": "cfo@example.com", "workspace_id": finance["id"], "role": "admin"},
    )
    assert created.status_code == 201, created.text
    old_token = str(created.json()["link"]).rsplit("/", 1)[-1]
    account = (
        await db.execute(sa.select(User.id).where(User.email == "cfo@example.com"))
    ).scalar_one()

    again = await admin.post(
        f"/platform/accounts/{account}/invite", json={"workspace_id": finance["id"]}
    )
    assert again.status_code == 200, again.text
    assert again.json()["workspace_name"] == "Finance"
    assert (await admin.get(f"/invites/{old_token}")).json()["state"] == "invalid"

    status_code, me = await accept(
        str(again.json()["link"]).rsplit("/", 1)[-1], password=SECOND_PASSWORD, name="Fin Chief"
    )
    assert status_code == 200, me
    assert me["workspace_name"] == "Finance"


async def test_reissuing_for_a_non_member_is_refused(admin: ApiClient, db: AsyncSession) -> None:
    finance = await make_workspace(admin, "Finance")
    email, _ = await make_member(admin, "viewer", email="elsewhere@example.com")
    account = (await db.execute(sa.select(User.id).where(User.email == email))).scalar_one()

    refused = await admin.post(
        f"/platform/accounts/{account}/invite", json={"workspace_id": finance["id"]}
    )
    assert refused.status_code == 409
    assert refused.json()["title"] == "Not a member"


async def test_a_workspace_admin_cannot_reissue_into_another_workspace(
    admin: ApiClient, signed_in_as: object, db: AsyncSession
) -> None:
    finance = await make_workspace(admin, "Finance")
    await admin.post(
        "/platform/accounts",
        json={"email": "theirs@example.com", "workspace_id": finance["id"], "role": "viewer"},
    )
    account = (
        await db.execute(sa.select(User.id).where(User.email == "theirs@example.com"))
    ).scalar_one()

    other_admin: ApiClient = await signed_in_as("admin")  # type: ignore[operator]
    # The cross-workspace route is refused outright…
    refused = await other_admin.post(
        f"/platform/accounts/{account}/invite", json={"workspace_id": finance["id"]}
    )
    assert refused.status_code == 403
    # …and the workspace-scoped one cannot see somebody who is not in theirs.
    assert (await other_admin.post(f"/users/{account}/invite")).status_code == 404


# ---------------------------------------------------------------------------
# what the screen can show
# ---------------------------------------------------------------------------


async def test_an_unaccepted_invite_is_listed_apart_from_real_access(
    admin: ApiClient,
) -> None:
    """The Accounts screen said "invited" without saying invited to *what*.

    `pending` is separate from `workspaces` on purpose: an unaccepted invite
    is not access, and folding it in would show somebody as a member of a
    workspace they have never opened.
    """
    finance = await make_workspace(admin, "Finance")
    await admin.post(
        "/platform/accounts",
        json={"email": "waiting@example.com", "workspace_id": finance["id"], "role": "operator"},
    )

    row = next(
        a
        for a in (await admin.get("/platform/accounts")).json()["accounts"]
        if a["email"] == "waiting@example.com"
    )
    assert row["status"] == "invited"
    assert row["workspaces"] == []
    assert [(w["name"], w["role"], w["is_member"]) for w in row["pending"]] == [
        ("Finance", "operator", False)
    ]

    # Once accepted it moves across, and nothing is left pending.
    token = await invite_token(admin, "moved@example.com")
    assert (await accept(token, password=ADMIN_PASSWORD, name="Moved"))[0] == 200
    moved = next(
        a
        for a in (await admin.get("/platform/accounts")).json()["accounts"]
        if a["email"] == "moved@example.com"
    )
    assert moved["pending"] == []
    assert [w["name"] for w in moved["workspaces"]] == ["Research Workspace"]


async def test_the_membership_stays_pending_until_the_link_is_used(
    admin: ApiClient, db: AsyncSession
) -> None:
    await admin.post("/users/invite", json={"email": "unused@example.com", "role": "viewer"})
    await admin.post(f"/users/{await pending_id(admin, 'unused@example.com')}/invite")

    membership = (
        await db.execute(
            sa.select(Membership)
            .join(User, User.id == Membership.user_id)
            .where(User.email == "unused@example.com")
        )
    ).scalar_one()
    assert membership.status is UserStatus.INVITED
