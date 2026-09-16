"""CSV upload and column mapping (PRD §9.5, §14).

Two endpoints, because mapping columns is a two-step conversation: the file is
posted once to see what is in it, and again with a confirmed mapping. The
preview step deliberately stores nothing — a user who uploads the wrong export
should be able to walk away without having written a row.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Literal

import structlog
from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.schemas_evidence import (
    CsvColumnSample,
    CsvIngestResponse,
    CsvPreviewResponse,
    CsvRowError,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.connectors.base import ConnectorContext, ConnectorError
from agent.connectors.csv_ingest import CANONICAL_FIELDS, CsvIngestConnector, preview
from agent.db.session import get_session
from agent.evidence.store import EvidenceScopeError, EvidenceStore

log = structlog.get_logger(__name__)

router = APIRouter(tags=["sources"])

Db = Annotated[AsyncSession, Depends(get_session)]
ProjectWriter = Annotated[Principal, Depends(require(Permission.PROJECT_WRITE))]

#: How many row errors travel back in one response. The full count is always
#: reported; the list is capped so a catastrophically bad file cannot return a
#: 40MB problem document.
MAX_REPORTED_ERRORS = 200


async def _read_upload(file: UploadFile) -> bytes:
    settings = get_settings()
    content = await file.read()
    if not content:
        raise problems.Problem(
            status_code=status.HTTP_400_BAD_REQUEST,
            title="Empty file",
            detail="The uploaded file contains no data.",
            type_=problems.TYPE_VALIDATION,
        )
    if len(content) > settings.csv_max_bytes:
        raise problems.Problem(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            title="File too large",
            detail=f"The file is {len(content)} bytes; the limit is {settings.csv_max_bytes}.",
            type_=problems.TYPE_VALIDATION,
        )
    return content


def _bad_request(detail: str) -> problems.Problem:
    return problems.Problem(
        status_code=status.HTTP_400_BAD_REQUEST,
        title="Invalid CSV",
        detail=detail,
        type_=problems.TYPE_VALIDATION,
    )


@router.post(
    "/projects/{project_id}/sources/csv/preview",
    response_model=CsvPreviewResponse,
    summary="Inspect a CSV and propose a column mapping",
)
async def preview_csv(
    me: ProjectWriter,
    db: Db,
    project_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="The CRM export")],
) -> CsvPreviewResponse:
    """Headers, sample cells and a proposed mapping. Writes nothing."""
    store = EvidenceStore(db, me.workspace_id)
    try:
        await store.assert_project(project_id)
    except EvidenceScopeError as exc:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="Project not found",
            detail=str(exc),
        ) from exc

    content = await _read_upload(file)
    try:
        inspected = preview(content)
    except ConnectorError as exc:
        raise _bad_request(str(exc)) from exc

    columns = []
    for header in inspected.headers:
        values = [row[header] for row in inspected.sample_rows if row.get(header)][:3]
        columns.append(
            CsvColumnSample(
                header=header,
                values=values,
                suggested_field=inspected.suggested_mapping.get(header),
            )
        )
    return CsvPreviewResponse(
        filename=file.filename or "upload.csv",
        row_count=inspected.row_count,
        columns=columns,
        canonical_fields=[item.name for item in CANONICAL_FIELDS],
        required_fields=[item.name for item in CANONICAL_FIELDS if item.required],
    )


@router.post(
    "/projects/{project_id}/sources/csv",
    response_model=CsvIngestResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Import a CRM export as evidence",
)
async def upload_csv(
    me: ProjectWriter,
    db: Db,
    project_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="The CRM export")],
    outcome: Annotated[Literal["won", "lost"], Form(description="closed-won or closed-lost")],
    mapping: Annotated[str, Form(description='JSON object: {"Source Column": "canonical_field"}')],
) -> CsvIngestResponse:
    """Parse, validate, dedupe and store. Partial success is success.

    The mapping arrives as a JSON string rather than as nested form fields
    because multipart has no object type and the alternative — one form field
    per column — makes the client build the request differently for every file.
    """
    import json

    store = EvidenceStore(db, me.workspace_id)
    try:
        await store.assert_project(project_id)
    except EvidenceScopeError as exc:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="Project not found",
            detail=str(exc),
        ) from exc

    try:
        parsed_mapping = json.loads(mapping)
    except json.JSONDecodeError as exc:
        raise _bad_request(f"`mapping` is not valid JSON: {exc}") from exc
    if not isinstance(parsed_mapping, dict) or not parsed_mapping:
        raise _bad_request("`mapping` must be a non-empty JSON object of column → field")

    content = await _read_upload(file)
    connector = CsvIngestConnector(ConnectorContext(project_id=str(project_id)))
    try:
        result = connector.ingest(
            content,
            mapping={str(k): str(v) for k, v in parsed_mapping.items()},
            outcome=outcome,
            filename=file.filename,
        )
    except (ConnectorError, ValidationError) as exc:
        raise _bad_request(str(exc)) from exc

    written = await store.write(result.drafts, project_id=project_id)

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CSV_UPLOADED,
        target_type=AuditTarget.PROJECT,
        target_id=project_id,
        meta={
            "filename": file.filename,
            "outcome": outcome,
            "rows_accepted": result.accepted,
            "rows_skipped": result.skipped,
            "evidence_written": written.inserted,
            "duplicates": written.duplicates,
            "row_errors": len(result.errors),
        },
    )
    await db.commit()

    return CsvIngestResponse(
        project_id=project_id,
        outcome=outcome,
        rows_accepted=result.accepted,
        rows_skipped=result.skipped,
        evidence_written=written.inserted,
        duplicates=written.duplicates,
        errors=[
            CsvRowError(row=e.row, column=e.column, value=e.value, problem=e.problem)
            for e in result.errors[:MAX_REPORTED_ERRORS]
        ],
        evidence_ids=written.evidence_ids,
    )
