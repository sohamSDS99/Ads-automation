"""The workspace singleton: readable by everyone, writable by settings holders."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_auth import UpdateWorkspaceRequest, WorkspaceResponse
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import bootstrap
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.db.session import get_session

router = APIRouter(tags=["workspace"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
SettingsWriter = Annotated[Principal, Depends(require(Permission.SETTINGS_WRITE))]


@router.get("/workspace", response_model=WorkspaceResponse, summary="This workspace")
async def get_workspace(me: AnyMember, db: Db) -> WorkspaceResponse:
    workspace = await bootstrap.get_workspace(db)
    if workspace is None:  # pragma: no cover — a session implies a workspace
        raise problems.conflict("This installation has not been bootstrapped.")
    return WorkspaceResponse.model_validate(workspace)


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

    previous = workspace.name
    workspace.name = body.name
    write_audit(
        db,
        workspace_id=workspace.id,
        actor_id=me.user.id,
        action=AuditAction.WORKSPACE_UPDATED,
        target_type=AuditTarget.WORKSPACE,
        target_id=workspace.id,
        meta={"name": {"from": previous, "to": body.name}},
        ip=client_ip(request),
    )
    await db.commit()
    return WorkspaceResponse.model_validate(workspace)
