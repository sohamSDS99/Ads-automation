"""Business-context documents: upload, list, delete (PRD §14, wizard step 1).

Five text boxes cannot hold what a person knows about their own brand. This is
the other channel: the pricing sheet, the positioning one-pager, the objection
handling doc. A file posted here is read on the spot, split into passages, and
written as ordinary evidence — which is the only way it can reach a model at
all, because PRD §18 law 1 gives a node exactly one source of facts and it is
the evidence table.

Two decisions worth knowing before changing anything here.

**Extraction is synchronous.** A person who uploads a file expects to be told
whether it worked, and "queued" is not an answer to "did you read my PDF?".
The cost is a request that can take a few seconds while the passages are
embedded; the alternative is a green checkmark next to a scanned brochure the
run cannot read.

**Deleting a document deletes its passages.** Anything else leaves a project
citing a file nobody can see, in a report that outlives the upload.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, File, Response, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.schemas_documents import (
    PREVIEW_CHARS,
    DocumentListResponse,
    DocumentSummary,
    DocumentUploadResponse,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.db.models import Evidence, ProjectDocument, User
from agent.db.session import get_session
from agent.documents import BRAND_DOC, DocumentError, extract, passages, to_drafts
from agent.documents.chunk import MAX_PASSAGES
from agent.documents.extract import supported_extensions
from agent.evidence.store import EvidenceScopeError, EvidenceStore

log = structlog.get_logger(__name__)

router = APIRouter(tags=["documents"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
ProjectWriter = Annotated[Principal, Depends(require(Permission.PROJECT_WRITE))]


@router.get(
    "/projects/{project_id}/documents",
    response_model=DocumentListResponse,
    summary="The business-context documents this project reads",
)
async def list_documents(me: AnyMember, db: Db, project_id: uuid.UUID) -> DocumentListResponse:
    settings = get_settings()
    await _assert_project(db, me, project_id)

    rows = (
        (
            await db.execute(
                sa.select(ProjectDocument, User.name)
                .join(User, User.id == ProjectDocument.uploaded_by, isouter=True)
                .where(ProjectDocument.project_id == project_id)
                .order_by(ProjectDocument.created_at.asc())
            )
        )
        .tuples()
        .all()
    )
    return DocumentListResponse(
        documents=[_summary(document, name) for document, name in rows],
        total_chars=sum(document.char_count for document, _ in rows),
        max_documents=settings.document_max_per_project,
        max_bytes=settings.document_max_bytes,
        accepted_extensions=supported_extensions(),
    )


@router.post(
    "/projects/{project_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document as business context",
)
async def upload_document(
    me: ProjectWriter,
    db: Db,
    project_id: uuid.UUID,
    file: Annotated[UploadFile, File(description="PDF, Word (.docx), CSV or text")],
) -> DocumentUploadResponse:
    """Read the file, store its text, and write its passages as evidence."""
    settings = get_settings()
    store = EvidenceStore(db, me.workspace_id)
    try:
        await store.assert_project(project_id)
    except EvidenceScopeError as exc:
        raise problems.not_found(str(exc)) from exc

    content = await file.read()
    if len(content) > settings.document_max_bytes:
        raise problems.Problem(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            title="File too large",
            detail=(
                f"That file is {_megabytes(len(content))}; the limit is "
                f"{_megabytes(settings.document_max_bytes)}."
            ),
            type_=problems.TYPE_VALIDATION,
        )

    held = await db.scalar(
        sa.select(sa.func.count())
        .select_from(ProjectDocument)
        .where(ProjectDocument.project_id == project_id)
    )
    if (held or 0) >= settings.document_max_per_project:
        raise problems.Problem(
            status_code=status.HTTP_409_CONFLICT,
            title="Too many documents",
            detail=(
                f"This project already holds {held} documents, which is the limit. "
                "Remove one before adding another."
            ),
            type_=problems.TYPE_CONFLICT,
        )

    digest = hashlib.sha256(content).hexdigest()
    existing = await db.scalar(
        sa.select(ProjectDocument).where(
            ProjectDocument.project_id == project_id, ProjectDocument.sha256 == digest
        )
    )
    if existing is not None:
        raise problems.Problem(
            status_code=status.HTTP_409_CONFLICT,
            title="Already uploaded",
            detail=f"{existing.filename} has the same contents and is already in this project.",
            type_=problems.TYPE_CONFLICT,
            document_id=str(existing.id),
        )

    try:
        extracted = extract(
            file.filename or "document", content, max_chars=settings.document_max_chars
        )
    except DocumentError as exc:
        raise problems.unprocessable(str(exc), title="Could not read that file") from exc

    document = ProjectDocument(
        project_id=project_id,
        uploaded_by=me.user.id,
        filename=extracted.filename,
        media_type=extracted.media_type,
        byte_size=len(content),
        sha256=digest,
        text=extracted.text,
        char_count=extracted.char_count,
        unit=extracted.unit,
        unit_count=extracted.unit_count,
        warnings=list(extracted.warnings),
    )
    db.add(document)
    # The passages carry this id, so the row has to have one before they are
    # built. `flush` assigns it without ending the transaction the audit entry
    # and the evidence write still have to join.
    await db.flush()

    found = passages(extracted)
    document.passage_count = len(found)
    written = await store.write(
        to_drafts(extracted, document_id=str(document.id), found=found),
        project_id=project_id,
    )
    if len(found) >= MAX_PASSAGES:
        document.warnings = [
            *document.warnings,
            f"Only the first {len(found)} passages were indexed; the rest of the file "
            "is stored but will not be cited.",
        ]

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.DOCUMENT_UPLOADED,
        target_type=AuditTarget.PROJECT,
        target_id=project_id,
        meta={
            "document_id": str(document.id),
            "filename": document.filename,
            "bytes": document.byte_size,
            "chars": document.char_count,
            "passages": document.passage_count,
            "evidence_written": written.inserted,
            "warnings": document.warnings,
        },
    )
    await db.commit()
    await db.refresh(document)

    return DocumentUploadResponse(
        document=_summary(document, me.user.name),
        passages_written=written.inserted,
        duplicates=written.duplicates,
    )


@router.delete(
    "/projects/{project_id}/documents/{document_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Remove a document and everything it taught the project",
)
async def delete_document(
    me: ProjectWriter,
    db: Db,
    project_id: uuid.UUID,
    document_id: uuid.UUID,
) -> Response:
    await _assert_project(db, me, project_id)
    document = await db.scalar(
        sa.select(ProjectDocument).where(
            ProjectDocument.id == document_id, ProjectDocument.project_id == project_id
        )
    )
    if document is None:
        raise problems.not_found("That document is not in this project.")

    # `returning(id)` rather than `rowcount`: the async result type does not
    # carry a row count, and the number is worth having — it is what the audit
    # row uses to say how much of the project's evidence this delete took.
    removed = len(
        (
            await db.execute(
                sa.delete(Evidence)
                .where(
                    Evidence.project_id == project_id,
                    Evidence.kind == BRAND_DOC,
                    Evidence.payload["document_id"].astext == str(document_id),
                )
                .returning(Evidence.id)
            )
        )
        .scalars()
        .all()
    )
    await db.delete(document)

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.DOCUMENT_DELETED,
        target_type=AuditTarget.PROJECT,
        target_id=project_id,
        meta={
            "document_id": str(document_id),
            "filename": document.filename,
            "evidence_removed": removed,
        },
    )
    await db.commit()
    log.info(
        "document.deleted",
        project_id=str(project_id),
        document_id=str(document_id),
        evidence_removed=removed,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# --- helpers ----------------------------------------------------------------


async def _assert_project(db: AsyncSession, me: Principal, project_id: uuid.UUID) -> None:
    """Refuse a project in someone else's workspace, as a 404 rather than a 403.

    Same reasoning as the evidence routes: whether a project id exists is
    itself workspace information.
    """
    try:
        await EvidenceStore(db, me.workspace_id).assert_project(project_id)
    except EvidenceScopeError as exc:
        raise problems.not_found(str(exc)) from exc


def _summary(document: ProjectDocument, uploader: str | None) -> DocumentSummary:
    return DocumentSummary(
        id=document.id,
        project_id=document.project_id,
        filename=document.filename,
        media_type=document.media_type,
        bytes=document.byte_size,
        char_count=document.char_count,
        passage_count=document.passage_count,
        unit=document.unit,
        unit_count=document.unit_count,
        warnings=list(document.warnings or []),
        preview=_preview(document.text),
        created_at=document.created_at,
        uploaded_by=document.uploaded_by,
        uploaded_by_name=uploader,
    )


def _preview(text: str) -> str:
    head = " ".join(text.split())[: PREVIEW_CHARS + 1]
    if len(head) > PREVIEW_CHARS:
        return head[:PREVIEW_CHARS].rstrip() + "…"
    return head


def _megabytes(size: int) -> str:
    return f"{size / (1024 * 1024):.1f}MB"
