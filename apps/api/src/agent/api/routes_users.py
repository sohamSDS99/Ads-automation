"""Managing who is in the workspace. Invite-only, disable-never-delete."""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_auth import (
    InviteCreatedResponse,
    InviteUserRequest,
    UpdateUserRequest,
    UserListResponse,
    UserSummary,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import invitations
from agent.auth.deps import Principal, get_session_store, require
from agent.auth.rbac import Permission
from agent.auth.sessions import SessionStore
from agent.config import Settings, get_settings
from agent.db.models import UserRole, UserStatus
from agent.db.repos import UserRepo
from agent.db.session import get_session

log = structlog.get_logger(__name__)

router = APIRouter(tags=["users"])

Db = Annotated[AsyncSession, Depends(get_session)]
Store = Annotated[SessionStore, Depends(get_session_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
UserAdmin = Annotated[Principal, Depends(require(Permission.USER_MANAGE))]


@router.get("/users", response_model=UserListResponse, summary="Everyone in the workspace")
async def list_users(me: AnyMember, db: Db) -> UserListResponse:
    """Readable by every role: knowing who can approve a gate is not privileged.

    Scoped to the active workspace by the repository's join, so a member of
    two workspaces appears on two different lists with two different roles and
    neither list mentions the other.
    """
    members = await UserRepo(db, me.workspace_id).all_ordered()
    return UserListResponse(users=[UserSummary.model_validate(m) for m in members])


@router.post(
    "/users/invite",
    response_model=InviteCreatedResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Invite someone",
)
async def invite_user(
    me: UserAdmin,
    body: InviteUserRequest,
    request: Request,
    db: Db,
    settings: SettingsDep,
) -> InviteCreatedResponse:
    """Create the profile, the pending membership, and the single-use link.

    The work is `agent.auth.invitations.invite_member`, shared with the two
    other places an account can be created — the system administrator's
    Accounts screen and the founding admin of a new workspace. This route is
    the one that invites into *the workspace you are in*, and the only thing
    it adds is that it does not have to ask which one that is.
    """
    try:
        invitation = await invitations.invite_member(
            db,
            workspace=me.workspace,
            email=str(body.email),
            role=body.role,
            invited_by=me.user,
            settings=settings,
            name=body.name,
            ip=client_ip(request),
            audit_meta=me.audit_meta(),
        )
    except invitations.InvitationError as exc:
        raise problems.conflict(exc.detail, title=exc.title) from exc

    return _invite_response(invitation)


def _invite_response(invitation: invitations.Invitation) -> InviteCreatedResponse:
    """One shape for every route that creates an invite."""
    return InviteCreatedResponse(
        invite_id=invitation.invite.id,
        email=invitation.invite.email,
        role=invitation.role,
        expires_at=invitation.invite.expires_at,
        link=invitation.link,
        email_delivered=invitation.email_delivered,
        has_account=invitation.had_account,
        workspace_id=invitation.workspace.id,
        workspace_name=invitation.workspace.name,
    )


@router.patch(
    "/users/{user_id}",
    response_model=UserSummary,
    summary="Change someone's role or status",
)
async def update_user(
    me: UserAdmin,
    user_id: uuid.UUID,
    body: UpdateUserRequest,
    request: Request,
    db: Db,
    store: Store,
) -> UserSummary:
    """Re-role or disable a member, refusing to strand the workspace without an admin.

    The last-admin check runs against row-locked admin rows inside this
    transaction, so two admins demoting each other at the same instant cannot
    both succeed (PRD §6.1, Authorization 4).
    """
    if body.role is None and body.status is None:
        raise problems.Problem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            title="Nothing to change",
            detail="Provide a role, a status, or both.",
            type_=problems.TYPE_VALIDATION,
        )
    if body.status is UserStatus.INVITED:
        raise problems.Problem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            title="Status not settable",
            detail="'invited' is set by the invite flow and cannot be assigned here.",
            type_=problems.TYPE_VALIDATION,
        )

    repo = UserRepo(db, me.workspace_id)
    target = await repo.get(user_id)
    if target is None:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="No such user",
            detail="That user is not a member of this workspace.",
        )
    if target.user.status is UserStatus.DISABLED:
        # Their account is locked installation-wide, and re-enabling them here
        # would produce a member who still cannot sign in. Only the system
        # administrator can undo that, and saying so is more use than a row
        # that silently refuses to take effect.
        raise problems.conflict(
            f"{target.email}'s account is disabled across the whole system. "
            "A system administrator has to re-enable it first.",
            title="Account disabled",
        )

    membership = target.membership
    if body.status is not None and membership.status is UserStatus.INVITED:
        # Marking a pending invite "active" would produce a member with no
        # password who cannot sign in, and would spend the invite link they
        # are holding — `accept_invite` only consumes an invited membership.
        # Their role can still be changed while they decide.
        raise problems.conflict(
            f"{target.email} has not accepted their invite yet, so their access "
            "cannot be enabled or disabled. Remove them to withdraw the invite.",
            title="Invite still open",
        )

    new_role = body.role or membership.role
    new_status = body.status or membership.status
    loses_admin = membership.role is UserRole.ADMIN and (
        new_role is not UserRole.ADMIN or new_status is not UserStatus.ACTIVE
    )

    if loses_admin:
        active_admins = await repo.lock_active_admins()
        if not [a for a in active_admins if a.id != target.id]:
            raise problems.conflict(
                "This is the only active admin. Promote someone else first.",
                title="Last admin",
            )

    changes: dict[str, object] = {}
    if body.role is not None and body.role is not membership.role:
        changes["role"] = {"from": membership.role.value, "to": body.role.value}
        membership.role = body.role
    if body.status is not None and body.status is not membership.status:
        changes["status"] = {"from": membership.status.value, "to": body.status.value}
        membership.status = body.status

    if not changes:
        return UserSummary.model_validate(target)

    ip = client_ip(request)
    if "role" in changes:
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.USER_ROLE_CHANGED,
            target_type=AuditTarget.USER,
            target_id=target.id,
            meta=me.audit_meta(email=target.email, role=changes["role"]),
            ip=ip,
        )
    if "status" in changes:
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.USER_STATUS_CHANGED,
            target_type=AuditTarget.USER,
            target_id=target.id,
            meta=me.audit_meta(email=target.email, status=changes["status"]),
            ip=ip,
        )
    await db.commit()

    # Both changes invalidate what the target's live sessions were granted
    # under, so neither waits for a cookie to expire. Scoped to this
    # workspace: a demotion here says nothing about the workspace next door,
    # and signing them out of it would be a second decision nobody made.
    if changes:
        revoked = await store.revoke_for_user_in_workspace(target.id, me.workspace_id)
        log.info(
            "auth.sessions_revoked",
            user_id=str(target.id),
            workspace_id=str(me.workspace_id),
            revoked=revoked,
            reason=",".join(changes),
        )
    return UserSummary.model_validate(target)


@router.delete(
    "/users/{user_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove someone from this workspace",
)
async def remove_member(
    me: UserAdmin,
    user_id: uuid.UUID,
    request: Request,
    db: Db,
    store: Store,
) -> Response:
    """Take away this workspace's access, and nothing else.

    The account survives, along with their name on every project, run and
    approval they touched here — "disabled, never deleted" (PRD §6.1) is about
    the account, and this route does not contradict it. What goes is one
    membership row: they lose this workspace from their switcher and keep
    every other one, which is the whole reason the two are separate tables.

    Removing is the right verb for someone who has moved to another team.
    Disabling the membership is the right one for someone who may come back,
    and `PATCH` still does that.
    """
    repo = UserRepo(db, me.workspace_id)
    target = await repo.get(user_id)
    if target is None:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="No such user",
            detail="That user is not a member of this workspace.",
        )

    if target.membership.role is UserRole.ADMIN and target.status is UserStatus.ACTIVE:
        remaining = [a for a in await repo.lock_active_admins() if a.id != target.id]
        if not remaining:
            raise problems.conflict(
                "This is the only active admin. Promote someone else first.",
                title="Last admin",
            )

    # Read before the delete: after it the row is staged for removal, and
    # the audit entry that outlives it is the only record of what was taken.
    email, role = target.email, target.membership.role.value
    await db.delete(target.membership)
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.MEMBER_REMOVED,
        target_type=AuditTarget.USER,
        target_id=target.id,
        meta=me.audit_meta(email=email, role=role),
        ip=client_ip(request),
    )
    await db.commit()

    revoked = await store.revoke_for_user_in_workspace(target.id, me.workspace_id)
    log.info(
        "auth.member_removed",
        user_id=str(target.id),
        workspace_id=str(me.workspace_id),
        revoked=revoked,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)
