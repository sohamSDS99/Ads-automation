"""Accounts, across every workspace. The system administrator's screen.

Everything here is guarded by `PLATFORM_ADMIN`, which no role grants — it
comes from `user.is_superadmin` alone. That is what keeps these routes out of
reach of a workspace admin, who administers their own workspace completely and
must not be able to see, disable or promote people in anybody else's.

Two things live here and nowhere else, because they are properties of the
account rather than of a membership:

* **Disabling an account** locks someone out of every workspace at once. A
  workspace admin can remove a person from their own workspace; only this can
  end their access to the installation.
* **Promoting a system administrator.** The last usable one cannot demote or
  disable themselves, for the same reason a workspace cannot lose its last
  admin: an installation with no administrator has no way back.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_auth import (
    AccountListResponse,
    AccountSummary,
    UpdateAccountRequest,
    WorkspaceMembershipSummary,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import workspaces
from agent.auth.bootstrap import first_workspace
from agent.auth.deps import Principal, get_session_store, require
from agent.auth.rbac import Permission
from agent.auth.sessions import SessionStore
from agent.db.models import Membership, User, UserStatus, Workspace
from agent.db.session import get_session

log = structlog.get_logger(__name__)

router = APIRouter(tags=["platform"])

Db = Annotated[AsyncSession, Depends(get_session)]
Store = Annotated[SessionStore, Depends(get_session_store)]
PlatformAdmin = Annotated[Principal, Depends(require(Permission.PLATFORM_ADMIN))]


@router.get(
    "/platform/accounts",
    response_model=AccountListResponse,
    summary="Every account on the installation",
)
async def list_accounts(me: PlatformAdmin, db: Db) -> AccountListResponse:
    """Everyone, with the workspaces each of them can actually reach.

    Two queries, not one per row: the membership rows for the whole page are
    fetched once and stitched in memory. An installation with forty people in
    six workspaces would otherwise issue forty-one queries to draw one table.
    """
    accounts = list(
        (await db.execute(sa.select(User).order_by(User.created_at.asc()))).scalars().all()
    )
    rows = await db.execute(
        sa.select(Membership, Workspace)
        .join(Workspace, Workspace.id == Membership.workspace_id)
        .where(Membership.status == UserStatus.ACTIVE, Workspace.archived_at.is_(None))
        .order_by(sa.func.lower(Workspace.name))
    )
    by_user: dict[uuid.UUID, list[WorkspaceMembershipSummary]] = {}
    for membership, workspace in rows.all():
        by_user.setdefault(membership.user_id, []).append(
            WorkspaceMembershipSummary(
                id=workspace.id, name=workspace.name, role=membership.role, is_member=True
            )
        )

    return AccountListResponse(
        accounts=[
            AccountSummary(
                id=account.id,
                email=account.email,
                name=account.name,
                status=account.status,
                is_superadmin=account.is_superadmin,
                last_login_at=account.last_login_at,
                created_at=account.created_at,
                workspaces=by_user.get(account.id, []),
            )
            for account in accounts
        ]
    )


@router.patch(
    "/platform/accounts/{user_id}",
    response_model=AccountSummary,
    summary="Promote, demote, disable or restore an account",
)
async def update_account(
    me: PlatformAdmin,
    user_id: uuid.UUID,
    body: UpdateAccountRequest,
    request: Request,
    db: Db,
    store: Store,
) -> AccountSummary:
    """Change what an account is, installation-wide.

    The two guards below are the same guard twice: you may not remove the last
    way in. Demoting the final system administrator, or disabling them, would
    leave an installation nobody can create a workspace in and nobody can
    promote anybody from — recoverable only by editing the database by hand.
    """
    if body.is_superadmin is None and body.status is None:
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

    target = await db.get(User, user_id)
    if target is None:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="No such account",
            detail="That account does not exist.",
        )

    losing_superadmin = target.is_superadmin and (
        body.is_superadmin is False or body.status is UserStatus.DISABLED
    )
    if losing_superadmin and await workspaces.count_superadmins(db, exclude=target.id) == 0:
        raise problems.conflict(
            "This is the only system administrator. Promote someone else first.",
            title="Last system administrator",
        )

    ip = client_ip(request)
    # The audit log is per workspace and this change is not, so it is written
    # against the installation's oldest workspace — the same place a failed
    # sign-in goes. Anything narrower would file a platform-wide act under one
    # tenant's history and hide it from the rest.
    home = await first_workspace(db)
    changes: dict[str, object] = {}

    if body.is_superadmin is not None and body.is_superadmin != target.is_superadmin:
        target.is_superadmin = body.is_superadmin
        changes["is_superadmin"] = body.is_superadmin
        if home is not None:
            write_audit(
                db,
                workspace_id=home.id,
                actor_id=me.user.id,
                action=AuditAction.SUPERADMIN_GRANTED
                if body.is_superadmin
                else AuditAction.SUPERADMIN_REVOKED,
                target_type=AuditTarget.USER,
                target_id=target.id,
                meta={"email": target.email},
                ip=ip,
            )
    if body.status is not None and body.status is not target.status:
        changes["status"] = {"from": target.status.value, "to": body.status.value}
        target.status = body.status
        if home is not None:
            write_audit(
                db,
                workspace_id=home.id,
                actor_id=me.user.id,
                action=AuditAction.ACCOUNT_STATUS_CHANGED,
                target_type=AuditTarget.USER,
                target_id=target.id,
                meta={"email": target.email, "status": changes["status"]},
                ip=ip,
            )

    if not changes:
        return await _account(db, target)

    await db.commit()

    # Either change alters what their live cookies are worth, everywhere. This
    # is the one place the wide revocation is right: the decision was about
    # the account, not about one workspace.
    revoked = await store.revoke_all_for_user(target.id)
    log.info(
        "platform.account_updated",
        user_id=str(target.id),
        changes=",".join(changes),
        sessions_revoked=revoked,
    )
    return await _account(db, target)


async def _account(db: AsyncSession, account: User) -> AccountSummary:
    rows = await db.execute(
        sa.select(Membership, Workspace)
        .join(Workspace, Workspace.id == Membership.workspace_id)
        .where(
            Membership.user_id == account.id,
            Membership.status == UserStatus.ACTIVE,
            Workspace.archived_at.is_(None),
        )
        .order_by(sa.func.lower(Workspace.name))
    )
    return AccountSummary(
        id=account.id,
        email=account.email,
        name=account.name,
        status=account.status,
        is_superadmin=account.is_superadmin,
        last_login_at=account.last_login_at,
        created_at=account.created_at,
        workspaces=[
            WorkspaceMembershipSummary(
                id=workspace.id, name=workspace.name, role=membership.role, is_member=True
            )
            for membership, workspace in rows.all()
        ],
    )
