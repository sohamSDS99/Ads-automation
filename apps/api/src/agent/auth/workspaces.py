"""Workspaces and memberships: who can reach what, and which one they are in.

Every question this module answers has the same shape — *is this person
allowed in this workspace right now?* — and every caller must ask it rather
than trust a session, a cookie or a previous answer. Three rules hold
throughout:

1. **A membership is the grant.** `session.workspace_id` is where the browser
   thinks it is, never proof that it may be there. `resolve_access` re-reads
   the membership on every request, so revoking access ends it on the caller's
   next call.
2. **The system administrator needs no membership.** `user.is_superadmin`
   reaches every workspace that is not archived. That is the point of the role
   and the reason each use of it lands in the audit log of the workspace it
   touched.
3. **An archived workspace admits nobody**, administrator included. Archiving
   is how a workspace stops being used without its history being destroyed; a
   back door for one account would make it a suggestion instead.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Membership, User, UserRole, UserStatus, Workspace


class WorkspaceAccessError(RuntimeError):
    """The caller may not enter that workspace. Carries why, for the API layer."""

    def __init__(self, reason: str, *, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


@dataclass(frozen=True, slots=True)
class Access:
    """A resolved (user, workspace) pair and the role it carries.

    `membership` is None only for a system administrator entering a workspace
    they are not a member of. `role` is then `admin`, which is what they
    effectively hold — but `is_member` stays False so the interface can say
    plainly that they are visiting rather than belonging.
    """

    workspace: Workspace
    membership: Membership | None
    role: UserRole
    via_superadmin: bool

    @property
    def is_member(self) -> bool:
        return self.membership is not None


@dataclass(frozen=True, slots=True)
class WorkspaceMembership:
    """One row of "the workspaces you can switch to"."""

    workspace: Workspace
    role: UserRole
    status: UserStatus
    #: False when the caller reaches it as the system administrator only.
    is_member: bool


async def get(db: AsyncSession, workspace_id: uuid.UUID) -> Workspace | None:
    return await db.get(Workspace, workspace_id)


async def by_name(db: AsyncSession, name: str) -> Workspace | None:
    """Case-insensitive, matching the unique index the database enforces."""
    result = await db.execute(
        sa.select(Workspace).where(sa.func.lower(Workspace.name) == name.strip().lower())
    )
    return result.scalar_one_or_none()


async def membership_for(
    db: AsyncSession, *, workspace_id: uuid.UUID, user_id: uuid.UUID
) -> Membership | None:
    result = await db.execute(
        sa.select(Membership).where(
            Membership.workspace_id == workspace_id, Membership.user_id == user_id
        )
    )
    return result.scalar_one_or_none()


async def list_all(db: AsyncSession, *, include_archived: bool = False) -> list[Workspace]:
    """Every workspace on the installation. For the system administrator only."""
    stmt = sa.select(Workspace).order_by(sa.func.lower(Workspace.name))
    if not include_archived:
        stmt = stmt.where(Workspace.archived_at.is_(None))
    return list((await db.execute(stmt)).scalars().all())


async def list_for_user(
    db: AsyncSession, user: User, *, include_archived: bool = False
) -> list[WorkspaceMembership]:
    """What goes in this person's switcher, in the order it is displayed.

    A system administrator sees every workspace, their own memberships marked
    as such, so the switcher never hides a workspace from the one account whose
    job is to look after all of them.
    """
    stmt = (
        sa.select(Workspace, Membership)
        .join(
            Membership,
            sa.and_(
                Membership.workspace_id == Workspace.id,
                Membership.user_id == user.id,
            ),
            isouter=user.is_superadmin,
        )
        .order_by(sa.func.lower(Workspace.name))
    )
    if not include_archived:
        stmt = stmt.where(Workspace.archived_at.is_(None))
    if not user.is_superadmin:
        # A revoked membership is not a workspace you can switch to. Pending
        # invitations are not either: the link is what accepts them.
        stmt = stmt.where(Membership.status == UserStatus.ACTIVE)

    rows: list[WorkspaceMembership] = []
    for workspace, membership in (await db.execute(stmt)).all():
        active = membership is not None and membership.status is UserStatus.ACTIVE
        rows.append(
            WorkspaceMembership(
                workspace=workspace,
                role=membership.role if active else UserRole.ADMIN,
                status=membership.status if membership is not None else UserStatus.ACTIVE,
                is_member=active,
            )
        )
    return rows


async def resolve_access(db: AsyncSession, *, user: User, workspace_id: uuid.UUID) -> Access:
    """The authorization decision, made the same way on every single request.

    Raises `WorkspaceAccessError` rather than returning None so that no caller
    can accidentally treat "no access" as "no opinion" — the reason string is
    what the API turns into a problem document.
    """
    workspace = await get(db, workspace_id)
    if workspace is None:
        raise WorkspaceAccessError("missing", detail="That workspace no longer exists.")
    if workspace.is_archived:
        raise WorkspaceAccessError(
            "archived",
            detail=f"{workspace.name} has been archived and cannot be opened.",
        )

    membership = await membership_for(db, workspace_id=workspace_id, user_id=user.id)
    if membership is not None and membership.status is UserStatus.ACTIVE:
        return Access(
            workspace=workspace, membership=membership, role=membership.role, via_superadmin=False
        )

    if user.is_superadmin:
        return Access(
            workspace=workspace, membership=None, role=UserRole.ADMIN, via_superadmin=True
        )

    if membership is not None and membership.status is UserStatus.INVITED:
        raise WorkspaceAccessError(
            "invited",
            detail="Your invitation to that workspace has not been accepted yet.",
        )
    raise WorkspaceAccessError("forbidden", detail="You do not have access to that workspace.")


async def default_workspace_id(db: AsyncSession, user: User) -> uuid.UUID | None:
    """Where to land this person at sign-in.

    Their last workspace when they can still reach it, otherwise the first one
    their switcher would show. None means they have nowhere to go, which is a
    real state — an account whose only membership was revoked — and the sign-in
    route reports it rather than opening an empty session.
    """
    if user.last_workspace_id is not None:
        try:
            await resolve_access(db, user=user, workspace_id=user.last_workspace_id)
        except WorkspaceAccessError:
            pass
        else:
            return user.last_workspace_id

    reachable = await list_for_user(db, user)
    return reachable[0].workspace.id if reachable else None


async def member_counts(db: AsyncSession) -> dict[uuid.UUID, int]:
    """Active members per workspace, for the administration screen.

    One grouped query rather than one per row: the screen lists every
    workspace on the installation, and a count per card is how it earns the
    word "overview".
    """
    result = await db.execute(
        sa.select(Membership.workspace_id, sa.func.count())
        .where(Membership.status == UserStatus.ACTIVE)
        .group_by(Membership.workspace_id)
    )
    return {workspace_id: int(count) for workspace_id, count in result.all()}


def add_member(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    role: UserRole,
    status: UserStatus,
    invited_by: uuid.UUID | None = None,
) -> Membership:
    """Stage a membership. The caller commits, as everywhere else in this codebase."""
    membership = Membership(
        workspace_id=workspace_id,
        user_id=user_id,
        role=role,
        status=status,
        invited_by=invited_by,
    )
    db.add(membership)
    return membership


async def active_admin_ids(
    db: AsyncSession, workspace_id: uuid.UUID, *, for_update: bool = False
) -> list[uuid.UUID]:
    """Who still administers this workspace.

    `for_update` row-locks them, which is what stops two admins demoting each
    other in the same instant and leaving the workspace with none.
    """
    stmt = sa.select(Membership.user_id).where(
        Membership.workspace_id == workspace_id,
        Membership.role == UserRole.ADMIN,
        Membership.status == UserStatus.ACTIVE,
    )
    if for_update:
        stmt = stmt.with_for_update()
    return list((await db.execute(stmt)).scalars().all())


async def count_superadmins(db: AsyncSession, *, exclude: uuid.UUID | None = None) -> int:
    """How many usable system administrators exist besides `exclude`.

    Usable means the account itself is active: a disabled superadmin cannot
    sign in, so counting them would let the last real one demote themselves.
    """
    stmt = (
        sa.select(sa.func.count())
        .select_from(User)
        .where(User.is_superadmin.is_(True), User.status == UserStatus.ACTIVE)
    )
    if exclude is not None:
        stmt = stmt.where(User.id != exclude)
    return int((await db.execute(stmt)).scalar_one())


async def member_ids(db: AsyncSession, workspace_id: uuid.UUID) -> list[uuid.UUID]:
    """Everyone with a membership row here, whatever its status.

    Whatever its status on purpose: this is used to end sessions when a
    workspace is archived, and someone whose membership was disabled an hour
    ago may still be holding a live cookie.
    """
    result = await db.execute(
        sa.select(Membership.user_id).where(Membership.workspace_id == workspace_id)
    )
    return list(result.scalars().all())
