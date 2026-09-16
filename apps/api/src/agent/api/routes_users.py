"""Managing who is in the workspace. Invite-only, disable-never-delete."""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.exc import IntegrityError
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
from agent.auth import bootstrap, invites
from agent.auth.deps import Principal, get_session_store, require
from agent.auth.rbac import Permission
from agent.auth.sessions import SessionStore
from agent.config import Settings, get_settings
from agent.db.models import User, UserRole, UserStatus
from agent.db.repos import InviteRepo, UserRepo
from agent.db.session import get_session
from agent.notify.email import send_invite

log = structlog.get_logger(__name__)

router = APIRouter(tags=["users"])

Db = Annotated[AsyncSession, Depends(get_session)]
Store = Annotated[SessionStore, Depends(get_session_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
UserAdmin = Annotated[Principal, Depends(require(Permission.USER_MANAGE))]


def invite_link(settings: Settings, token: str) -> str:
    return f"{settings.app_base_url}/invite/{token}"


@router.get("/users", response_model=UserListResponse, summary="Everyone in the workspace")
async def list_users(me: AnyMember, db: Db) -> UserListResponse:
    """Readable by every role: knowing who can approve a gate is not privileged."""
    users = await UserRepo(db, me.workspace_id).all_ordered()
    return UserListResponse(users=[UserSummary.model_validate(u) for u in users])


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
    """Create the pending account and its single-use link.

    A `user` row is written now, with `status='invited'` and no password hash, so
    the admin sees the pending person in the member list straight away and the
    email's global uniqueness constraint — not application logic — is what stops
    a second invite to the same address.
    """
    email = str(body.email)
    user_repo = UserRepo(db, me.workspace_id)
    invite_repo = InviteRepo(db, me.workspace_id)

    # The pending row and the open invite are written together, so the `user`
    # row is hit first. Reporting it as "already a member" would be wrong for
    # someone who has not accepted anything yet.
    existing = await user_repo.by_email(email)
    if existing is not None and existing.status is UserStatus.INVITED:
        raise problems.conflict(
            f"{email} already has an unaccepted invite.", title="Invite already open"
        )
    if existing is not None:
        raise problems.conflict(
            f"{email} is already a member of this workspace.", title="Already a member"
        )
    if await invite_repo.open_for_email(email) is not None:
        raise problems.conflict(
            f"{email} already has an unaccepted invite.", title="Invite already open"
        )

    token = invites.new_token()
    pending = User(
        email=email,
        name=body.name,
        password_hash=None,
        role=body.role,
        status=UserStatus.INVITED,
    )
    user_repo.add(pending)
    invite_repo.add(
        invites.build(
            workspace_id=me.workspace_id,
            email=email,
            role=body.role,
            invited_by=me.user.id,
            token=token,
        )
    )

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise problems.conflict(
            f"{email} already has an account or an open invite.", title="Already invited"
        ) from exc

    invite = await invite_repo.open_for_email(email)
    if invite is None:  # pragma: no cover — just flushed
        raise problems.conflict("The invite could not be created.", title="Invite failed")

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.USER_INVITED,
        target_type=AuditTarget.INVITE,
        target_id=invite.id,
        meta={"email": email, "role": body.role.value},
        ip=client_ip(request),
    )
    await db.commit()

    workspace = await bootstrap.get_workspace(db)
    link = invite_link(settings, token)
    # Delivery happens after the commit on purpose: the invite exists whether or
    # not the mail server does, and `send_invite` never raises.
    delivery = await send_invite(
        settings,
        to=email,
        link=link,
        workspace_name=workspace.name if workspace else "the workspace",
        inviter=me.user.name,
    )
    return InviteCreatedResponse(
        invite_id=invite.id,
        email=email,
        role=body.role,
        expires_at=invite.expires_at,
        link=link,
        email_delivered=delivery.delivered,
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

    new_role = body.role or target.role
    new_status = body.status or target.status
    loses_admin = target.role is UserRole.ADMIN and (
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
    if body.role is not None and body.role is not target.role:
        changes["role"] = {"from": target.role.value, "to": body.role.value}
        target.role = body.role
    if body.status is not None and body.status is not target.status:
        changes["status"] = {"from": target.status.value, "to": body.status.value}
        target.status = body.status

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
            meta={"email": target.email, "role": changes["role"]},
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
            meta={"email": target.email, "status": changes["status"]},
            ip=ip,
        )
    await db.commit()

    # Both changes invalidate what the target's live sessions were granted
    # under, so neither waits for a cookie to expire.
    if changes:
        revoked = await store.revoke_all_for_user(target.id)
        log.info(
            "auth.sessions_revoked",
            user_id=str(target.id),
            revoked=revoked,
            reason=",".join(changes),
        )
    return UserSummary.model_validate(target)
