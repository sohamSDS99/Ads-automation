"""The audit log, admin-only and cursor-paginated."""

from __future__ import annotations

import base64
import binascii
import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.schemas_auth import AuditEntry, AuditListResponse
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.db.repos import AuditRepo
from agent.db.session import get_session

router = APIRouter(tags=["audit"])

Db = Annotated[AsyncSession, Depends(get_session)]
AuditReader = Annotated[Principal, Depends(require(Permission.AUDIT_READ))]

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200


def encode_cursor(created_at: datetime, entry_id: uuid.UUID) -> str:
    """Both halves of the sort key, because rows written in one transaction share a timestamp."""
    return base64.urlsafe_b64encode(f"{created_at.isoformat()}|{entry_id}".encode()).decode()


def decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        created_at, entry_id = raw.split("|", 1)
        return datetime.fromisoformat(created_at), uuid.UUID(entry_id)
    except (ValueError, binascii.Error) as exc:
        raise problems.Problem(
            status_code=status.HTTP_400_BAD_REQUEST,
            title="Invalid cursor",
            detail="That pagination cursor is not one this endpoint issued.",
            type_=problems.TYPE_VALIDATION,
        ) from exc


@router.get("/audit", response_model=AuditListResponse, summary="Audit log")
async def list_audit(
    me: AuditReader,
    db: Db,
    actor: Annotated[uuid.UUID | None, Query(description="Filter to one actor")] = None,
    action: Annotated[str | None, Query(description="Exact action name")] = None,
    from_: Annotated[
        datetime | None, Query(alias="from", description="Inclusive lower bound")
    ] = None,
    to: Annotated[datetime | None, Query(description="Inclusive upper bound")] = None,
    cursor: Annotated[str | None, Query(description="From a previous response")] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_SIZE_MAX)] = PAGE_SIZE_DEFAULT,
) -> AuditListResponse:
    rows = await AuditRepo(db, me.workspace_id).page(
        actor_id=actor,
        action=action,
        since=from_,
        until=to,
        before=decode_cursor(cursor) if cursor else None,
        limit=limit,
    )

    entries = [
        AuditEntry(
            id=row.id,
            actor_id=row.actor_id,
            actor_email=actor_email,
            action=row.action,
            target_type=row.target_type,
            target_id=row.target_id,
            meta=dict(row.meta),
            ip=row.ip,
            created_at=row.created_at,
        )
        for row, actor_email in rows
    ]
    # A short page is the last page; only a full one can have more behind it.
    next_cursor = (
        encode_cursor(entries[-1].created_at, entries[-1].id) if len(entries) == limit else None
    )
    return AuditListResponse(entries=entries, next_cursor=next_cursor)
