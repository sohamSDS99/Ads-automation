"""Many workspaces: isolation, switching, the system administrator, and joining.

The tests that matter most here are the negative ones. A multi-tenant feature
is only as good as the things it refuses, so the file is organised around four
refusals — one workspace cannot see another's rows, a workspace admin cannot
administer the fleet, an invite link cannot change a password it does not own,
and an archived workspace cannot be entered by anybody.
"""

from __future__ import annotations

import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Membership, Project, User, UserRole, UserStatus, Workspace
from tests.integration.conftest import ADMIN_PASSWORD, ApiClient, build_client, make_member

SECOND_PASSWORD = "tundra-beacon-42-vellum"  # noqa: S105 — a test fixture's password


async def accept(token: str, *, password: str, name: str | None = None) -> tuple[int, dict]:
    """Accept an invite in a fresh browser. Returns (status, body)."""
    joiner = build_client()
    async with joiner.raw:
        body: dict[str, object] = {"password": password}
        if name is not None:
            body["name"] = name
        response = await joiner.post(f"/invites/{token}/accept", json=body)
        return response.status_code, response.json()


async def invite_token(admin: ApiClient, email: str, role: str = "viewer") -> str:
    created = await admin.post("/users/invite", json={"email": email, "role": role})
    assert created.status_code == 201, created.text
    return str(created.json()["link"]).rsplit("/", 1)[-1]


async def make_workspace(admin: ApiClient, name: str, *, admin_email: str | None = None) -> dict:
    body: dict[str, object] = {"name": name}
    if admin_email is not None:
        body["admin_email"] = admin_email
    response = await admin.post("/workspaces", json=body)
    assert response.status_code == 201, response.text
    return dict(response.json())


# ---------------------------------------------------------------------------
# creating and listing
# ---------------------------------------------------------------------------


async def test_the_installation_is_no_longer_limited_to_one_workspace(
    admin: ApiClient, db: AsyncSession
) -> None:
    """The singleton index is gone, and the API is what proves it."""
    await make_workspace(admin, "Paid Search")
    await make_workspace(admin, "Brand")

    count = (await db.execute(sa.select(sa.func.count()).select_from(Workspace))).scalar_one()
    assert count == 3


async def test_two_workspaces_cannot_share_a_name(admin: ApiClient) -> None:
    """Case-insensitively: the switcher cannot tell “Brand” from “brand”."""
    await make_workspace(admin, "Brand")
    clash = await admin.post("/workspaces", json={"name": "  brand  "})
    assert clash.status_code == 409
    assert clash.json()["title"] == "Name already used"


async def test_a_workspace_admin_cannot_create_a_workspace(
    admin: ApiClient, signed_in_as: object
) -> None:
    """The asymmetry the whole feature rests on."""
    other_admin: ApiClient = await signed_in_as("admin")  # type: ignore[operator]
    refused = await other_admin.post("/workspaces", json={"name": "Shadow Company"})
    assert refused.status_code == 403
    assert refused.json()["missing_permission"] == "platform_admin"


async def test_the_creator_is_not_silently_made_a_member(
    admin: ApiClient, db: AsyncSession
) -> None:
    """A system administrator setting up finance's workspace is not in finance."""
    created = await make_workspace(admin, "Finance")
    members = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Membership)
            .where(Membership.workspace_id == uuid.UUID(created["id"]))
        )
    ).scalar_one()
    assert members == 0
    assert created["is_member"] is False
    assert created["member_count"] == 0


async def test_a_new_workspace_can_be_handed_to_an_admin_who_sets_their_own_password(
    admin: ApiClient, db: AsyncSession
) -> None:
    """The founding admin arrives through the ordinary invite, not as a ready account."""
    created = await make_workspace(admin, "Finance", admin_email="cfo@example.com")

    account = (
        await db.execute(sa.select(User).where(User.email == "cfo@example.com"))
    ).scalar_one()
    assert account.password_hash is None, "an invited account must hold no password yet"
    membership = (
        await db.execute(sa.select(Membership).where(Membership.user_id == account.id))
    ).scalar_one()
    assert membership.role is UserRole.ADMIN
    assert membership.status is UserStatus.INVITED
    assert membership.workspace_id == uuid.UUID(created["id"])


async def test_the_switcher_shows_only_what_a_member_can_open(
    admin: ApiClient, signed_in_as: object
) -> None:
    await make_workspace(admin, "Paid Search")
    viewer: ApiClient = await signed_in_as("viewer")  # type: ignore[operator]

    mine = (await viewer.get("/workspaces")).json()["workspaces"]
    assert [w["name"] for w in mine] == ["Research Workspace"]
    # A member sees no member counts: that is an administration figure about
    # workspaces they may not even know exist.
    assert mine[0]["member_count"] is None

    everything = (await admin.get("/workspaces")).json()["workspaces"]
    assert {w["name"] for w in everything} == {"Research Workspace", "Paid Search"}
    assert all(w["member_count"] is not None for w in everything)


# ---------------------------------------------------------------------------
# isolation
# ---------------------------------------------------------------------------


async def test_a_project_in_one_workspace_is_invisible_from_another(
    admin: ApiClient, db: AsyncSession
) -> None:
    """The central promise. Same account, same session cookie, different workspace."""
    first = (await admin.get("/workspace")).json()
    made = await admin.post("/projects", json={"name": "Home Turf", "domain": "home.example"})
    assert made.status_code == 201, made.text

    second = await make_workspace(admin, "Paid Search")
    switched = await admin.post("/auth/workspace", json={"workspace_id": second["id"]})
    assert switched.status_code == 200, switched.text
    assert switched.json()["workspace_name"] == "Paid Search"

    assert (await admin.get("/projects")).json()["projects"] == []

    back = await admin.post("/auth/workspace", json={"workspace_id": first["id"]})
    assert back.status_code == 200
    assert [p["name"] for p in (await admin.get("/projects")).json()["projects"]] == ["Home Turf"]


async def test_a_member_of_one_workspace_cannot_switch_into_another(
    admin: ApiClient, signed_in_as: object
) -> None:
    other = await make_workspace(admin, "Paid Search")
    viewer: ApiClient = await signed_in_as("viewer")  # type: ignore[operator]

    refused = await viewer.post("/auth/workspace", json={"workspace_id": other["id"]})
    assert refused.status_code == 403
    assert "do not have access" in refused.json()["detail"]


async def test_the_team_list_is_per_workspace(admin: ApiClient, db: AsyncSession) -> None:
    """The same account in two workspaces appears on two lists, with two roles."""
    await make_member(admin, "operator", email="both@example.com")
    second = await make_workspace(admin, "Paid Search")

    await admin.post("/auth/workspace", json={"workspace_id": second["id"]})
    assert (await admin.get("/users")).json()["users"] == []

    token = await invite_token(admin, "both@example.com", role="admin")
    status_code, body = await accept(token, password=ADMIN_PASSWORD)
    assert status_code == 200, body
    assert body["role"] == "admin"
    assert body["workspace_name"] == "Paid Search"

    here = {u["email"]: u["role"] for u in (await admin.get("/users")).json()["users"]}
    assert here == {"both@example.com": "admin"}

    accounts = (
        await db.execute(
            sa.select(sa.func.count()).select_from(User).where(User.email == "both@example.com")
        )
    ).scalar_one()
    assert accounts == 1, "joining a second workspace must not fork the account"


# ---------------------------------------------------------------------------
# joining an existing account
# ---------------------------------------------------------------------------


async def test_an_invite_link_cannot_overwrite_an_existing_password(
    admin: ApiClient, db: AsyncSession
) -> None:
    """The one that would be a real vulnerability.

    The link grants a workspace. If it also accepted a new password for an
    account that already has one, anyone holding a forwarded invite could take
    the account over — including the admin who issued it.
    """
    email, password = await make_member(admin, "viewer", email="incumbent@example.com")
    before = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
    original_hash = before.password_hash

    second = await make_workspace(admin, "Paid Search")
    await admin.post("/auth/workspace", json={"workspace_id": second["id"]})
    token = await invite_token(admin, email, role="operator")

    status_code, body = await accept(token, password="a-completely-different-one")
    assert status_code == 403
    assert body["title"] == "Password incorrect"

    db.expire_all()
    after = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
    assert after.password_hash == original_hash
    membership = (
        await db.execute(
            sa.select(Membership).where(
                Membership.user_id == after.id,
                Membership.workspace_id == uuid.UUID(second["id"]),
            )
        )
    ).scalar_one()
    assert membership.status is UserStatus.INVITED, "a refused accept must not grant access"

    # And the right password does work, on the same link.
    ok_status, ok_body = await accept(token, password=password)
    assert ok_status == 200, ok_body
    assert ok_body["workspace_name"] == "Paid Search"


async def test_the_invite_page_says_whether_the_address_already_has_an_account(
    admin: ApiClient, client: ApiClient
) -> None:
    await make_member(admin, "viewer", email="incumbent@example.com")
    second = await make_workspace(admin, "Paid Search")
    await admin.post("/auth/workspace", json={"workspace_id": second["id"]})

    known = await invite_token(admin, "incumbent@example.com")
    stranger = await invite_token(admin, "stranger@example.com")

    assert (await client.get(f"/invites/{known}")).json()["has_account"] is True
    assert (await client.get(f"/invites/{stranger}")).json()["has_account"] is False


async def test_an_admin_need_not_invent_a_name_for_the_person_they_invite(
    admin: ApiClient,
) -> None:
    """Email is the only thing required; the placeholder is replaced on accept."""
    token = await invite_token(admin, "jo.patel@example.com")
    listed = {u["email"]: u["name"] for u in (await admin.get("/users")).json()["users"]}
    assert listed["jo.patel@example.com"] == "Jo Patel"

    status_code, body = await accept(token, password=SECOND_PASSWORD, name="Jo Patel-Okonkwo")
    assert status_code == 200, body
    assert body["name"] == "Jo Patel-Okonkwo"


# ---------------------------------------------------------------------------
# the system administrator
# ---------------------------------------------------------------------------


async def test_the_system_administrator_reaches_a_workspace_they_never_joined(
    admin: ApiClient, db: AsyncSession
) -> None:
    created = await make_workspace(admin, "Finance")
    switched = await admin.post("/auth/workspace", json={"workspace_id": created["id"]})
    assert switched.status_code == 200, switched.text

    body = switched.json()
    assert body["via_superadmin"] is True
    assert body["role"] == "admin"
    assert {w["name"] for w in body["workspaces"]} >= {"Finance", "Research Workspace"}

    # And the visit is on the record, in the log of the workspace visited.
    entries = (await admin.get("/audit")).json()["entries"]
    entered = [e for e in entries if e["action"] == "workspace.entered"]
    assert entered and entered[0]["meta"]["via_superadmin"] is True


async def test_what_a_visiting_administrator_does_is_marked_as_such(
    admin: ApiClient,
) -> None:
    """Otherwise they are indistinguishable from the workspace's own admin."""
    created = await make_workspace(admin, "Finance")
    await admin.post("/auth/workspace", json={"workspace_id": created["id"]})
    await admin.post("/users/invite", json={"email": "someone@example.com", "role": "viewer"})

    entries = (await admin.get("/audit")).json()["entries"]
    invited = next(e for e in entries if e["action"] == "user.invited")
    assert invited["meta"]["via_superadmin"] is True


async def test_the_last_system_administrator_cannot_step_down(
    admin: ApiClient, db: AsyncSession
) -> None:
    me = (await admin.get("/auth/me")).json()
    refused = await admin.patch(f"/platform/accounts/{me['id']}", json={"is_superadmin": False})
    assert refused.status_code == 409
    assert refused.json()["title"] == "Last system administrator"

    db.expire_all()
    row = (await db.execute(sa.select(User).where(User.id == uuid.UUID(me["id"])))).scalar_one()
    assert row.is_superadmin is True


async def test_a_second_system_administrator_frees_the_first(
    admin: ApiClient, db: AsyncSession
) -> None:
    email, _ = await make_member(admin, "operator", email="deputy@example.com")
    deputy = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
    promoted = await admin.patch(f"/platform/accounts/{deputy.id}", json={"is_superadmin": True})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["is_superadmin"] is True

    me = (await admin.get("/auth/me")).json()
    stepped_down = await admin.patch(
        f"/platform/accounts/{me['id']}", json={"is_superadmin": False}
    )
    assert stepped_down.status_code == 200, stepped_down.text


async def test_disabling_an_account_locks_it_out_of_every_workspace(
    admin: ApiClient, second_client: ApiClient, db: AsyncSession
) -> None:
    email, password = await make_member(admin, "operator", email="leaver@example.com")
    assert (await second_client.login(email, password)).status_code == 200
    assert (await second_client.get("/auth/me")).status_code == 200

    leaver = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
    disabled = await admin.patch(f"/platform/accounts/{leaver.id}", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text

    # The live session dies on its next request, not when the cookie expires.
    assert (await second_client.get("/auth/me")).status_code == 401
    assert (await second_client.login(email, password)).status_code == 401


# ---------------------------------------------------------------------------
# membership lifecycle
# ---------------------------------------------------------------------------


async def test_removing_someone_ends_their_session_here_and_nowhere_else(
    admin: ApiClient, db: AsyncSession
) -> None:
    """Two workspaces, two browsers, one removal. Only one session should die."""
    email, password = await make_member(admin, "operator", email="dual@example.com")
    second = await make_workspace(admin, "Paid Search")
    await admin.post("/auth/workspace", json={"workspace_id": second["id"]})
    token = await invite_token(admin, email, role="operator")
    assert (await accept(token, password=password))[0] == 200

    here, there = build_client(), build_client()
    async with here.raw, there.raw:
        assert (await here.login(email, password)).status_code == 200
        assert (await there.login(email, password)).status_code == 200
        # Park one browser in each workspace.
        first = (await admin.get("/workspaces")).json()["workspaces"]
        home = next(w for w in first if w["name"] == "Research Workspace")
        assert (
            await here.post("/auth/workspace", json={"workspace_id": home["id"]})
        ).status_code == 200
        assert (
            await there.post("/auth/workspace", json={"workspace_id": second["id"]})
        ).status_code == 200

        target = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
        removed = await admin.delete(f"/users/{target.id}")
        assert removed.status_code == 204, removed.text

        assert (await there.get("/auth/me")).status_code == 401, "removed here, session should end"
        assert (await here.get("/auth/me")).status_code == 200, "untouched workspace, live session"


async def test_removing_someone_keeps_the_account_and_their_history(
    admin: ApiClient, db: AsyncSession
) -> None:
    email, _ = await make_member(admin, "operator", email="mover@example.com")
    # Read the id out before expiring the session: an expired instance would
    # reload lazily, and a lazy load inside an async test is an error rather
    # than a query.
    target_id = (await db.execute(sa.select(User.id).where(User.email == email))).scalar_one()

    assert (await admin.delete(f"/users/{target_id}")).status_code == 204

    db.expire_all()
    still_there = (
        await db.execute(sa.select(sa.func.count()).select_from(User).where(User.id == target_id))
    ).scalar_one()
    assert still_there == 1
    assert [u["email"] for u in (await admin.get("/users")).json()["users"]] == [
        "admin@example.com"
    ]
    assert (await admin.delete(f"/users/{target_id}")).status_code == 404


async def test_the_last_admin_of_a_workspace_cannot_be_removed(
    admin: ApiClient, db: AsyncSession
) -> None:
    me = (await admin.get("/auth/me")).json()
    refused = await admin.delete(f"/users/{me['id']}")
    assert refused.status_code == 409
    assert refused.json()["title"] == "Last admin"


async def test_a_demotion_here_does_not_sign_someone_out_over_there(
    admin: ApiClient, db: AsyncSession
) -> None:
    email, password = await make_member(admin, "operator", email="dual2@example.com")
    second = await make_workspace(admin, "Paid Search")
    await admin.post("/auth/workspace", json={"workspace_id": second["id"]})
    token = await invite_token(admin, email, role="operator")
    assert (await accept(token, password=password))[0] == 200

    browser = build_client()
    async with browser.raw:
        assert (await browser.login(email, password)).status_code == 200
        first = (await admin.get("/workspaces")).json()["workspaces"]
        home = next(w for w in first if w["name"] == "Research Workspace")
        await browser.post("/auth/workspace", json={"workspace_id": home["id"]})

        target = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
        # Demote them in Paid Search, where the admin currently is.
        assert (
            await admin.patch(f"/users/{target.id}", json={"role": "viewer"})
        ).status_code == 200
        assert (await browser.get("/auth/me")).status_code == 200


# ---------------------------------------------------------------------------
# archiving
# ---------------------------------------------------------------------------


async def test_archiving_closes_a_workspace_without_destroying_it(
    admin: ApiClient, db: AsyncSession
) -> None:
    second = await make_workspace(admin, "Paid Search")
    await admin.post("/auth/workspace", json={"workspace_id": second["id"]})
    made = await admin.post("/projects", json={"name": "Doomed", "domain": "doomed.example"})
    assert made.status_code == 201, made.text

    first = next(
        w
        for w in (await admin.get("/workspaces")).json()["workspaces"]
        if w["name"] == "Research Workspace"
    )
    await admin.post("/auth/workspace", json={"workspace_id": first["id"]})

    archived = await admin.delete(f"/workspaces/{second['id']}")
    assert archived.status_code == 200, archived.text
    assert archived.json()["name"] == "Paid Search"

    # Gone from the switcher, and it cannot be entered.
    names = {w["name"] for w in (await admin.get("/workspaces")).json()["workspaces"]}
    assert "Paid Search" not in names
    refused = await admin.post("/auth/workspace", json={"workspace_id": second["id"]})
    assert refused.status_code == 403
    assert "archived" in refused.json()["detail"]

    # The rows are all still there.
    projects = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Project)
            .where(Project.workspace_id == uuid.UUID(second["id"]))
        )
    ).scalar_one()
    assert projects == 1


async def test_an_archived_workspace_can_be_restored(admin: ApiClient) -> None:
    second = await make_workspace(admin, "Paid Search")
    assert (await admin.delete(f"/workspaces/{second['id']}")).status_code == 200

    hidden = {w["name"] for w in (await admin.get("/workspaces")).json()["workspaces"]}
    assert "Paid Search" not in hidden
    shown = {
        w["name"]
        for w in (await admin.get("/workspaces?include_archived=true")).json()["workspaces"]
    }
    assert "Paid Search" in shown

    restored = await admin.post(f"/workspaces/{second['id']}/restore")
    assert restored.status_code == 200, restored.text
    assert (
        await admin.post("/auth/workspace", json={"workspace_id": second["id"]})
    ).status_code == 200


async def test_the_only_workspace_cannot_be_archived(admin: ApiClient) -> None:
    """An installation with nowhere to sign in to is not a state worth reaching."""
    only = (await admin.get("/workspace")).json()
    refused = await admin.delete(f"/workspaces/{only['id']}")
    assert refused.status_code == 409
    assert refused.json()["title"] == "Last workspace"


async def test_archiving_ends_the_sessions_inside_it(admin: ApiClient, db: AsyncSession) -> None:
    email, password = await make_member(admin, "operator", email="inside@example.com")
    second = await make_workspace(admin, "Paid Search")
    await admin.post("/auth/workspace", json={"workspace_id": second["id"]})
    token = await invite_token(admin, email, role="operator")
    assert (await accept(token, password=password))[0] == 200

    browser = build_client()
    async with browser.raw:
        assert (await browser.login(email, password)).status_code == 200
        assert (
            await browser.post("/auth/workspace", json={"workspace_id": second["id"]})
        ).status_code == 200

        first = next(
            w
            for w in (await admin.get("/workspaces")).json()["workspaces"]
            if w["name"] == "Research Workspace"
        )
        await admin.post("/auth/workspace", json={"workspace_id": first["id"]})
        result = await admin.delete(f"/workspaces/{second['id']}")
        assert result.status_code == 200, result.text
        assert result.json()["sessions_ended"] >= 1

        assert (await browser.get("/auth/me")).status_code == 401
        # They still have their other workspace, so signing back in works.
        assert (await browser.login(email, password)).status_code == 200


# ---------------------------------------------------------------------------
# the session is a hint, never a grant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("revocation", ["disable", "remove"])
async def test_access_ends_on_the_next_request_not_when_the_cookie_does(
    admin: ApiClient, second_client: ApiClient, db: AsyncSession, revocation: str
) -> None:
    email, password = await make_member(admin, "operator", email="live@example.com")
    assert (await second_client.login(email, password)).status_code == 200
    assert (await second_client.get("/projects")).status_code == 200

    target = (await db.execute(sa.select(User).where(User.email == email))).scalar_one()
    if revocation == "disable":
        assert (
            await admin.patch(f"/users/{target.id}", json={"status": "disabled"})
        ).status_code == 200
    else:
        assert (await admin.delete(f"/users/{target.id}")).status_code == 204

    assert (await second_client.get("/projects")).status_code == 401


async def test_a_pending_invite_cannot_be_enabled_out_from_under_itself(
    admin: ApiClient, db: AsyncSession
) -> None:
    """Marking an unaccepted member "active" would spend the link they hold.

    The account has no password yet, so an "active" membership produces
    somebody who cannot sign in *and* whose invite has stopped working. The
    role is still changeable while they decide.
    """
    await invite_token(admin, "pending@example.com", role="viewer")
    pending = (
        await db.execute(sa.select(User.id).where(User.email == "pending@example.com"))
    ).scalar_one()

    refused = await admin.patch(f"/users/{pending}", json={"status": "active"})
    assert refused.status_code == 409
    assert refused.json()["title"] == "Invite still open"

    promoted = await admin.patch(f"/users/{pending}", json={"role": "operator"})
    assert promoted.status_code == 200, promoted.text
    assert promoted.json()["role"] == "operator"
    assert promoted.json()["status"] == "invited"


async def test_a_project_still_names_the_person_who_left(
    admin: ApiClient, db: AsyncSession
) -> None:
    """Membership is revocable, and run history is not.

    A project list that says "Unknown" against half its rows because three
    people changed teams is worse than one that can still name them.
    """
    email, password = await make_member(admin, "operator", email="author@example.com")

    author = build_client()
    async with author.raw:
        assert (await author.login(email, password)).status_code == 200
        made = await author.post("/projects", json={"name": "Theirs", "domain": "theirs.example"})
        assert made.status_code == 201, made.text

    target = (await db.execute(sa.select(User.id).where(User.email == email))).scalar_one()
    assert (await admin.delete(f"/users/{target}")).status_code == 204

    listed = (await admin.get("/projects")).json()["projects"]
    theirs = next(p for p in listed if p["name"] == "Theirs")
    assert theirs["created_by_name"] == "Operator"
