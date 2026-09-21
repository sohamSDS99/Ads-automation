"""Workspaces: the one you are in, and the ones the installation has.

Two audiences, deliberately two shapes of route.

`/workspace` is *this* workspace — the one the session is in. Any member may
read it, a settings holder may change it, and neither needs to know the id.
Everything the settings screen edits lives here.

`/workspaces` is the collection, and every write on it requires
`PLATFORM_ADMIN`. That is the asymmetry the whole feature rests on: an admin
of one company's workspace administers that workspace completely and cannot
create, rename, archive or even enumerate another's. Only the system
administrator sees across the boundary, and the audit log of the workspace
they touched records that they did.
"""

from __future__ import annotations

import uuid
from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

import httpx
import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.routes_users import default_name, invite_link
from agent.api.schemas_auth import (
    ArchiveWorkspaceResponse,
    CreateWorkspaceRequest,
    RenameWorkspaceRequest,
    StorageUsageResponse,
    UpdateWorkspaceRequest,
    WorkspaceListResponse,
    WorkspaceResponse,
    WorkspaceSettings,
    WorkspaceSummary,
)
from agent.api.worker_files import WorkerClient
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import invites, workspaces
from agent.auth.deps import Principal, get_session_store, require
from agent.auth.rbac import Permission
from agent.auth.sessions import SessionStore
from agent.config import Settings, get_settings
from agent.db.models import User, UserRole, UserStatus, Workspace
from agent.db.repos import account_by_email, utcnow
from agent.db.session import get_session
from agent.llm.router import SETTINGS_KEY, ModelRoutingError, validate_overrides
from agent.notify.email import send_invite

log = structlog.get_logger(__name__)

router = APIRouter(tags=["workspace"])

Db = Annotated[AsyncSession, Depends(get_session)]
Store = Annotated[SessionStore, Depends(get_session_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
SettingsWriter = Annotated[Principal, Depends(require(Permission.SETTINGS_WRITE))]
#: The system administrator. `PLATFORM_ADMIN` is held by no role, so this is
#: `user.is_superadmin` and nothing else — see `agent.auth.rbac`.
PlatformAdmin = Annotated[Principal, Depends(require(Permission.PLATFORM_ADMIN))]


@router.get("/workspace", response_model=WorkspaceResponse, summary="This workspace")
async def get_workspace(me: AnyMember) -> WorkspaceResponse:
    """The workspace this session is in. Resolved by the session, not by a query."""
    return _response(me.workspace)


@router.patch("/workspace", response_model=WorkspaceResponse, summary="Rename this workspace")
async def update_workspace(
    me: SettingsWriter,
    body: UpdateWorkspaceRequest,
    request: Request,
    db: Db,
) -> WorkspaceResponse:
    workspace = me.workspace

    changed: dict[str, Any] = {}
    if body.name != workspace.name:
        if await _name_taken(db, body.name, exclude=workspace.id):
            raise problems.conflict(
                f"Another workspace is already called “{body.name}”.",
                title="Name already used",
            )
        changed["name"] = {"from": workspace.name, "to": body.name}
        workspace.name = body.name

    if body.models is not None or body.max_run_cost_usd is not None:
        # Reassigned rather than mutated: SQLAlchemy does not track in-place
        # edits of a JSONB dict, so a mutated `settings` would never be written.
        settings = dict(workspace.settings)
        if body.models is not None:
            try:
                validate_overrides(body.models, source="workspace.settings")
            except ModelRoutingError as exc:
                raise problems.unprocessable(str(exc)) from exc
            settings[SETTINGS_KEY] = body.models
            changed["models"] = body.models
        if body.max_run_cost_usd is not None:
            settings["max_run_cost_usd"] = str(body.max_run_cost_usd)
            changed["max_run_cost_usd"] = str(body.max_run_cost_usd)
        workspace.settings = settings

    if changed:
        write_audit(
            db,
            workspace_id=workspace.id,
            actor_id=me.user.id,
            action=AuditAction.WORKSPACE_UPDATED,
            target_type=AuditTarget.WORKSPACE,
            target_id=workspace.id,
            meta=me.audit_meta(**changed),
            ip=client_ip(request),
        )
    try:
        await db.commit()
    except IntegrityError as exc:
        # `uq_workspace_name` is checked by the database, so the pre-flight
        # above is a better error message and this is the actual guarantee.
        await db.rollback()
        raise problems.conflict(
            f"Another workspace is already called “{body.name}”.", title="Name already used"
        ) from exc
    return _response(workspace)


async def _name_taken(db: Db, name: str, *, exclude: uuid.UUID | None = None) -> bool:
    existing = await workspaces.by_name(db, name)
    return existing is not None and existing.id != exclude


# --- the collection ---------------------------------------------------------


@router.get("/workspaces", response_model=WorkspaceListResponse, summary="Workspaces you can open")
async def list_workspaces(
    me: AnyMember,
    db: Db,
    include_archived: bool = False,
) -> WorkspaceListResponse:
    """What belongs in the switcher, and — for the system administrator — the
    whole installation with a member count against each.

    One route rather than two because it answers one question ("which
    workspaces may I open?") whose answer happens to be wider for one account.
    Splitting it would mean the shell had to know which caller it was before
    it knew which URL to call.
    """
    entries = await workspaces.list_for_user(
        db, me.user, include_archived=include_archived and me.is_superadmin
    )
    counts = await workspaces.member_counts(db) if me.is_superadmin else {}
    return WorkspaceListResponse(
        workspaces=[
            WorkspaceSummary(
                id=entry.workspace.id,
                name=entry.workspace.name,
                created_at=entry.workspace.created_at,
                archived_at=entry.workspace.archived_at,
                member_count=counts.get(entry.workspace.id, 0) if me.is_superadmin else None,
                role=entry.role if entry.is_member else None,
                is_member=entry.is_member,
                current=entry.workspace.id == me.workspace_id,
            )
            for entry in entries
        ]
    )


@router.post(
    "/workspaces",
    response_model=WorkspaceSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workspace",
)
async def create_workspace(
    me: PlatformAdmin,
    body: CreateWorkspaceRequest,
    request: Request,
    db: Db,
    settings_dep: SettingsDep,
) -> WorkspaceSummary:
    """Open a new company, or a new business function inside one.

    The creator does *not* become a member. A system administrator setting up
    a workspace for the finance team is not on the finance team, and enrolling
    them silently would put their name in a member list they never asked to be
    in — they can already reach it, and `via_superadmin` says how. Passing
    `admin_email` names who should actually run it, and that person is invited
    exactly as any other member is: a link, and a password they choose.
    """
    name = body.name.strip()
    if await _name_taken(db, name):
        raise problems.conflict(
            f"A workspace called “{name}” already exists.", title="Name already used"
        )

    workspace = Workspace(name=name, created_by=me.user.id)
    db.add(workspace)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise problems.conflict(
            f"A workspace called “{name}” already exists.", title="Name already used"
        ) from exc

    write_audit(
        db,
        workspace_id=workspace.id,
        actor_id=me.user.id,
        action=AuditAction.WORKSPACE_CREATED,
        target_type=AuditTarget.WORKSPACE,
        target_id=workspace.id,
        meta={"name": name, "admin_email": str(body.admin_email) if body.admin_email else None},
        ip=client_ip(request),
    )
    await db.commit()
    log.info("workspace.created", workspace_id=str(workspace.id), name=name)

    invited: bool | None = None
    if body.admin_email is not None:
        # The workspace is already committed. Letting this raise would leave a
        # workspace that exists, a caller who saw a 500, and a retry that
        # collides on the name — so the failure is reported in the response
        # instead, and the invite can be sent again from the Team screen.
        try:
            await _invite_first_admin(
                db,
                settings=settings_dep,
                workspace=workspace,
                email=str(body.admin_email),
                invited_by=me.user,
                ip=client_ip(request),
            )
            invited = True
        except Exception as exc:  # noqa: BLE001 — see above
            await db.rollback()
            invited = False
            log.error(
                "workspace.founding_invite_failed",
                workspace_id=str(workspace.id),
                error=str(exc),
            )

    return WorkspaceSummary(
        id=workspace.id,
        name=workspace.name,
        created_at=workspace.created_at,
        archived_at=None,
        member_count=1 if invited else 0,
        role=None,
        is_member=False,
        current=False,
        admin_invited=invited,
    )


async def _invite_first_admin(
    db: Db,
    *,
    settings: Settings,
    workspace: Workspace,
    email: str,
    invited_by: User,
    ip: str | None,
) -> None:
    """Give the new workspace somebody to run it.

    Reuses the ordinary invite machinery rather than creating an active
    account: the person still sets their own password, and an admin created
    this way is indistinguishable from one invited on the Team page a week
    later. A failure here does not undo the workspace — it exists, and the
    invitation can be sent again from inside it.
    """
    account = await account_by_email(db, email)
    if account is None:
        account = User(
            email=email,
            name=default_name(email),
            password_hash=None,
            status=UserStatus.INVITED,
        )
        db.add(account)
        await db.flush()

    token = invites.new_token()
    workspaces.add_member(
        db,
        workspace_id=workspace.id,
        user_id=account.id,
        role=UserRole.ADMIN,
        status=UserStatus.INVITED,
        invited_by=invited_by.id,
    )
    db.add(
        invites.build(
            workspace_id=workspace.id,
            email=email,
            role=UserRole.ADMIN,
            invited_by=invited_by.id,
            token=token,
        )
    )
    write_audit(
        db,
        workspace_id=workspace.id,
        actor_id=invited_by.id,
        action=AuditAction.USER_INVITED,
        target_type=AuditTarget.WORKSPACE,
        target_id=workspace.id,
        meta={"email": email, "role": UserRole.ADMIN.value, "founding_admin": True},
        ip=ip,
    )
    await db.commit()

    await send_invite(
        settings,
        to=email,
        link=invite_link(settings, token),
        workspace_name=workspace.name,
        inviter=invited_by.name,
    )


@router.patch(
    "/workspaces/{workspace_id}",
    response_model=WorkspaceSummary,
    summary="Rename a workspace",
)
async def rename_workspace(
    me: PlatformAdmin,
    workspace_id: uuid.UUID,
    body: RenameWorkspaceRequest,
    request: Request,
    db: Db,
) -> WorkspaceSummary:
    """Rename any workspace on the installation.

    Renaming the one you are *in* is `PATCH /workspace`, which a workspace
    admin may do. This route reaches the others, so it is the system
    administrator's alone.
    """
    workspace = await workspaces.get(db, workspace_id)
    if workspace is None:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="No such workspace",
            detail="That workspace does not exist.",
        )
    name = body.name.strip()
    if await _name_taken(db, name, exclude=workspace.id):
        raise problems.conflict(
            f"A workspace called “{name}” already exists.", title="Name already used"
        )

    before = workspace.name
    workspace.name = name
    write_audit(
        db,
        workspace_id=workspace.id,
        actor_id=me.user.id,
        action=AuditAction.WORKSPACE_UPDATED,
        target_type=AuditTarget.WORKSPACE,
        target_id=workspace.id,
        meta={"name": {"from": before, "to": name}, "via_superadmin": True},
        ip=client_ip(request),
    )
    await db.commit()
    return await _summary(db, workspace, me)


@router.delete(
    "/workspaces/{workspace_id}",
    response_model=ArchiveWorkspaceResponse,
    summary="Archive a workspace",
)
async def archive_workspace(
    me: PlatformAdmin,
    workspace_id: uuid.UUID,
    request: Request,
    db: Db,
    store: Store,
) -> ArchiveWorkspaceResponse:
    """Close a workspace without destroying anything inside it.

    Archiving is the strongest thing this API will do to a workspace, and it
    is still reversible. Every project, run, report and audit row stays
    exactly where it is; what changes is that nobody — including the account
    running this route — can open it again until it is restored. The sessions
    already inside it are ended here rather than left to expire, because
    "archived" that still serves live pages for another twelve hours is not
    archived.

    There is no hard delete. `workspace` cascades to eight tables, and one
    mis-click should not be able to erase the record of everything a company
    ever decided. Deleting the row remains possible with a hand on the
    database, which is the correct amount of friction for that decision.
    """
    workspace = await workspaces.get(db, workspace_id)
    if workspace is None:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="No such workspace",
            detail="That workspace does not exist.",
        )
    if workspace.is_archived:
        raise problems.conflict(f"{workspace.name} is already archived.", title="Already archived")
    remaining = await workspaces.list_all(db)
    if len([w for w in remaining if w.id != workspace.id]) == 0:
        raise problems.conflict(
            "This is the only workspace left. Create another one before archiving it.",
            title="Last workspace",
        )

    archived_at = utcnow()
    workspace.archived_at = archived_at
    write_audit(
        db,
        workspace_id=workspace.id,
        actor_id=me.user.id,
        action=AuditAction.WORKSPACE_ARCHIVED,
        target_type=AuditTarget.WORKSPACE,
        target_id=workspace.id,
        meta={"name": workspace.name},
        ip=client_ip(request),
    )
    await db.commit()

    ended = 0
    for user_id in await workspaces.member_ids(db, workspace.id):
        ended += await store.revoke_for_user_in_workspace(user_id, workspace.id)
    # The acting administrator may be signed in here too, and is not
    # necessarily a member.
    ended += await store.revoke_for_user_in_workspace(me.user.id, workspace.id)
    log.info("workspace.archived", workspace_id=str(workspace.id), sessions_ended=ended)
    return ArchiveWorkspaceResponse(
        id=workspace.id, name=workspace.name, archived_at=archived_at, sessions_ended=ended
    )


@router.post(
    "/workspaces/{workspace_id}/restore",
    response_model=WorkspaceSummary,
    summary="Restore an archived workspace",
)
async def restore_workspace(
    me: PlatformAdmin,
    workspace_id: uuid.UUID,
    request: Request,
    db: Db,
) -> WorkspaceSummary:
    """Undo an archive. Everyone who was a member is a member again."""
    workspace = await workspaces.get(db, workspace_id)
    if workspace is None:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="No such workspace",
            detail="That workspace does not exist.",
        )
    if not workspace.is_archived:
        raise problems.conflict(f"{workspace.name} is not archived.", title="Not archived")

    workspace.archived_at = None
    write_audit(
        db,
        workspace_id=workspace.id,
        actor_id=me.user.id,
        action=AuditAction.WORKSPACE_RESTORED,
        target_type=AuditTarget.WORKSPACE,
        target_id=workspace.id,
        meta={"name": workspace.name},
        ip=client_ip(request),
    )
    await db.commit()
    return await _summary(db, workspace, me)


async def _summary(db: Db, workspace: Workspace, me: Principal) -> WorkspaceSummary:
    counts = await workspaces.member_counts(db)
    membership = await workspaces.membership_for(db, workspace_id=workspace.id, user_id=me.user.id)
    return WorkspaceSummary(
        id=workspace.id,
        name=workspace.name,
        created_at=workspace.created_at,
        archived_at=workspace.archived_at,
        member_count=counts.get(workspace.id, 0),
        role=membership.role if membership is not None else None,
        is_member=membership is not None,
        current=workspace.id == me.workspace_id,
    )


def _response(workspace: Workspace) -> WorkspaceResponse:
    settings = get_settings()
    stored = workspace.settings or {}
    raw_models = stored.get(SETTINGS_KEY)
    raw_cap = stored.get("max_run_cost_usd")
    return WorkspaceResponse(
        id=workspace.id,
        name=workspace.name,
        created_at=workspace.created_at,
        settings=WorkspaceSettings(
            models={
                str(key): str(value)
                for key, value in (raw_models or {}).items()
                if isinstance(raw_models, dict)
            },
            max_run_cost_usd=_decimal(raw_cap),
        ),
        smtp_configured=settings.smtp_configured,
        default_max_run_cost_usd=settings.max_run_cost_usd,
    )


def _decimal(value: Any) -> Decimal | None:
    """A stored cap, or None when it was never set or is unreadable."""
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):  # pragma: no cover — tolerate a bad row
        return None


@router.get("/storage", response_model=StorageUsageResponse, summary="Volume usage")
async def get_storage_usage(me: SettingsWriter, client: WorkerClient) -> StorageUsageResponse:
    """What is on the worker's Volume, and the windows the retention job applies.

    `SETTINGS_WRITE` rather than `READ` because its only consumer is the admin
    settings screen — the same call P6 made for `GET /models`.

    A worker that cannot be reached returns `reachable=false` rather than a 502.
    The storage panel is one card on a page full of other cards, and taking the
    whole settings screen down because a figure is unavailable would be a worse
    answer than saying the figure is unavailable.
    """
    settings = get_settings()
    retention = {
        "screenshots": settings.screenshot_retention_days,
        "debug": settings.debug_retention_days,
        "exports": settings.export_retention_days,
        "backups": settings.backup_retention_days,
    }
    url = f"{settings.worker_internal_url.rstrip('/')}/usage"
    try:
        response = await client.get(url, timeout=5.0)
        response.raise_for_status()
        payload = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        log.warning("storage.usage_unreachable", error=str(exc))
        return StorageUsageResponse(
            warn_above=settings.storage_warn_fraction, reachable=False, retention=retention
        )

    fraction = payload.get("used_fraction")
    return StorageUsageResponse(
        objects=int(payload.get("objects") or 0),
        bytes=int(payload.get("bytes") or 0),
        capacity_bytes=payload.get("capacity_bytes"),
        used_fraction=fraction,
        warn_above=settings.storage_warn_fraction,
        at_capacity=bool(fraction is not None and fraction >= settings.storage_warn_fraction),
        retention=retention,
    )
