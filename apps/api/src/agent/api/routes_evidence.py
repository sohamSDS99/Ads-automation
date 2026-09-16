"""`GET /evidence` — the read side of PRD Law 1.

Nodes cite `evidence_id`s, which is only meaningful if a human can pull the same
row up and check it. This endpoint is that check, and it is why the response
carries `matched_by` and the two ranks: "why did this surface" is as much a part
of the answer as the row itself.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.schemas_evidence import (
    ConnectorInfo,
    ConnectorListResponse,
    EvidenceItem,
    EvidenceListResponse,
)
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.connectors import CONNECTOR_NAMES, credential_kind_for, source_for
from agent.db.models import EvidenceSource
from agent.db.session import get_session
from agent.evidence.search import EvidenceSearch

router = APIRouter(tags=["evidence"])

Db = Annotated[AsyncSession, Depends(get_session)]
Reader = Annotated[Principal, Depends(require(Permission.READ))]

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200


def _offset(cursor: str | None) -> int:
    """Cursors here are opaque offsets.

    A keyset cursor needs a total order, and a fused relevance score is not one
    — it changes when new evidence lands. Rather than ship two cursor formats
    and have the caller guess which it holds, both modes use the same opaque
    string and the client only ever echoes it back.
    """
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except ValueError as exc:
        raise problems.Problem(
            status_code=status.HTTP_400_BAD_REQUEST,
            title="Invalid cursor",
            detail="That pagination cursor is not one this endpoint issued.",
            type_=problems.TYPE_VALIDATION,
        ) from exc
    if value < 0:
        raise problems.Problem(
            status_code=status.HTTP_400_BAD_REQUEST,
            title="Invalid cursor",
            detail="That pagination cursor is not one this endpoint issued.",
            type_=problems.TYPE_VALIDATION,
        )
    return value


@router.get("/evidence", response_model=EvidenceListResponse, summary="Search evidence")
async def list_evidence(
    me: Reader,
    db: Db,
    project_id: Annotated[uuid.UUID | None, Query(description="Restrict to one project")] = None,
    source: Annotated[EvidenceSource | None, Query(description="Filter by connector")] = None,
    kind: Annotated[str | None, Query(description="Exact kind, e.g. search_term_pnl")] = None,
    run_id: Annotated[uuid.UUID | None, Query(description="Evidence gathered by one run")] = None,
    q: Annotated[str | None, Query(description="Hybrid full-text + vector search")] = None,
    cursor: Annotated[str | None, Query(description="From a previous response")] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_SIZE_MAX)] = PAGE_SIZE_DEFAULT,
) -> EvidenceListResponse:
    page = await EvidenceSearch(db, me.workspace_id).search(
        project_id=project_id,
        source=source,
        kind=kind,
        run_id=run_id,
        query=q,
        limit=limit,
        offset=_offset(cursor),
    )
    return EvidenceListResponse(
        items=[
            EvidenceItem(
                id=hit.id,
                project_id=hit.project_id,
                run_id=hit.run_id,
                source=hit.source,
                kind=hit.kind,
                source_url=hit.source_url,
                content_text=hit.content_text,
                payload=hit.payload,
                fetched_at=hit.fetched_at,
                score=hit.score,
                matched_by=hit.matched_by,
                text_rank=hit.text_rank,
                vector_rank=hit.vector_rank,
            )
            for hit in page.hits
        ],
        next_cursor=page.next_cursor,
        ranked=page.ranked,
    )


@router.get("/connectors", response_model=ConnectorListResponse, summary="Available connectors")
async def list_connectors(me: Reader) -> ConnectorListResponse:
    """The five evidence sources and which of them need a stored secret."""
    return ConnectorListResponse(
        connectors=[
            ConnectorInfo(
                name=name,
                source=source_for(name),
                requires_credential=credential_kind_for(name),
            )
            for name in CONNECTOR_NAMES
        ]
    )
