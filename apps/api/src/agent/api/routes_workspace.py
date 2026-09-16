"""The workspace singleton: readable by everyone, writable by settings holders."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_auth import (
    UpdateWorkspaceRequest,
    WorkspaceResponse,
    WorkspaceSettings,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import bootstrap
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.db.models import Workspace
from agent.db.session import get_session
from agent.llm.router import SETTINGS_KEY, ModelRoutingError, validate_overrides

router = APIRouter(tags=["workspace"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
SettingsWriter = Annotated[Principal, Depends(require(Permission.SETTINGS_WRITE))]


@router.get("/workspace", response_model=WorkspaceResponse, summary="This workspace")
async def get_workspace(me: AnyMember, db: Db) -> WorkspaceResponse:
    workspace = await bootstrap.get_workspace(db)
    if workspace is None:  # pragma: no cover — a session implies a workspace
        raise problems.conflict("This installation has not been bootstrapped.")
    return _response(workspace)


@router.patch("/workspace", response_model=WorkspaceResponse, summary="Rename this workspace")
async def update_workspace(
    me: SettingsWriter,
    body: UpdateWorkspaceRequest,
    request: Request,
    db: Db,
) -> WorkspaceResponse:
    workspace = await bootstrap.get_workspace(db)
    if workspace is None:  # pragma: no cover — a session implies a workspace
        raise problems.conflict("This installation has not been bootstrapped.")

    changed: dict[str, Any] = {}
    if body.name != workspace.name:
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
            meta=changed,
            ip=client_ip(request),
        )
    await db.commit()
    return _response(workspace)


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
