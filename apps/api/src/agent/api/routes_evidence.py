"""`GET /evidence` — the read side of PRD Law 1.

Nodes cite `evidence_id`s, which is only meaningful if a human can pull the same
row up and check it. This endpoint is that check, and it is why the response
carries `matched_by` and the two ranks: "why did this surface" is as much a part
of the answer as the row itself.

Two shapes of that check exist. The Evidence Explorer browses and searches; the
Report Viewer resolves a specific set of citations, which is what `?ids=` is
for — `ResearchReport.evidence_ids()` hands over every id cited anywhere in a
report, and one request for the set beats one request per superscript.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.responses import Response, StreamingResponse

from agent.api import problems
from agent.api.schemas_evidence import (
    ConnectorInfo,
    ConnectorListResponse,
    EvidenceItem,
    EvidenceListResponse,
)
from agent.api.worker_files import WorkerClient, open_upstream, signed_url
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import Settings, get_settings
from agent.connectors import CONNECTOR_NAMES, credential_kind_for, source_for
from agent.db.models import Evidence, EvidenceSource, Project
from agent.db.session import get_session
from agent.evidence.search import EvidenceSearch

log = structlog.get_logger(__name__)

router = APIRouter(tags=["evidence"])

Db = Annotated[AsyncSession, Depends(get_session)]
Reader = Annotated[Principal, Depends(require(Permission.READ))]

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200

#: The payload key a connector writes a stored capture under.
SCREENSHOT_KEY = "screenshot_path"

#: What `GET /evidence/{id}/screenshot` will serve, by extension. A capture is a
#: PNG today; the map exists so a connector switching to WebP is a one-line
#: change rather than a mislabelled response.
IMAGE_MEDIA_TYPES = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


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
    ids: Annotated[
        list[uuid.UUID] | None,
        Query(description="Exactly these rows, e.g. every citation in one report"),
    ] = None,
    fetched_from: Annotated[
        datetime | None, Query(description="Only evidence fetched at or after this moment")
    ] = None,
    fetched_to: Annotated[
        datetime | None, Query(description="Only evidence fetched at or before this moment")
    ] = None,
    q: Annotated[str | None, Query(description="Hybrid full-text + vector search")] = None,
    cursor: Annotated[str | None, Query(description="From a previous response")] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_SIZE_MAX)] = PAGE_SIZE_DEFAULT,
) -> EvidenceListResponse:
    if ids is not None and len(ids) > PAGE_SIZE_MAX:
        raise problems.unprocessable(
            f"Ask for at most {PAGE_SIZE_MAX} ids at a time; this asked for {len(ids)}.",
        )
    page = await EvidenceSearch(db, me.workspace_id).search(
        project_id=project_id,
        source=source,
        kind=kind,
        run_id=run_id,
        ids=ids,
        fetched_from=fetched_from,
        fetched_to=fetched_to,
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
                has_screenshot=bool(hit.payload.get(SCREENSHOT_KEY)),
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


@router.get(
    "/evidence/{evidence_id}/screenshot",
    summary="The creative capture stored for one evidence row",
    response_class=StreamingResponse,
)
async def get_screenshot(
    evidence_id: uuid.UUID,
    me: Reader,
    db: Db,
    client: WorkerClient,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    """Stream a competitor creative off the worker's Volume (PRD §13.4 D).

    The caller names a row, never a path. The storage key comes out of the
    payload we wrote ourselves, so no request can ask this endpoint for an
    arbitrary object on the Volume — which is exactly what accepting a key would
    allow, signature or no signature.
    """
    row = (
        await db.execute(
            sa.select(Evidence)
            .join(Project, Project.id == Evidence.project_id)
            .where(Evidence.id == evidence_id, Project.workspace_id == me.workspace_id)
        )
    ).scalar_one_or_none()
    if row is None:
        raise problems.not_found(f"No evidence {evidence_id}.")

    key = row.payload.get(SCREENSHOT_KEY) if isinstance(row.payload, dict) else None
    if not isinstance(key, str) or not key:
        raise problems.not_found(f"Evidence {evidence_id} has no stored screenshot.")

    upstream = await open_upstream(
        client,
        signed_url(key, settings=settings),
        subject="screenshot",
        evidence_id=str(evidence_id),
    )
    suffix = key[key.rfind(".") :].lower() if "." in key else ""
    return StreamingResponse(
        upstream.aiter_bytes(),
        media_type=IMAGE_MEDIA_TYPES.get(suffix, "application/octet-stream"),
        headers={
            # The key carries a random component and the object behind it is
            # never rewritten, so a gallery scrolled twice fetches once.
            "Cache-Control": "private, max-age=3600, immutable",
            "Content-Disposition": "inline",
        },
        # The upstream response outlives this function, so closing it is the
        # response's job, not a context manager's.
        background=BackgroundTask(upstream.aclose),
    )
