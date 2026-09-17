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
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.schemas_documents import (
    PREVIEW_CHARS,
    DocumentListResponse,
    DocumentSummary,
    DocumentUploadResponse,
    SkippedEntry,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import Settings, get_settings
from agent.db.models import Evidence, ProjectDocument, User
from agent.db.session import get_session
from agent.documents import BRAND_DOC, DocumentError, extract, passages, to_drafts
from agent.documents.archive import ARCHIVE_SUFFIX, ArchiveError, is_archive, read_archive
from agent.documents.chunk import MAX_PASSAGES
from agent.documents.extract import supported_extensions
from agent.evidence.store import EvidenceScopeError, EvidenceStore, StoreResult

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
        # The archive is offered alongside the types it may contain, because
        # the file picker is the only place a person learns a zip is allowed.
        accepted_extensions=[*supported_extensions(), ARCHIVE_SUFFIX],
    )


@router.post(
    "/projects/{project_id}/documents",
    response_model=DocumentUploadResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a document, or a zip of them, as business context",
)
async def upload_document(
    me: ProjectWriter,
    db: Db,
    response: Response,
    project_id: uuid.UUID,
    file: Annotated[
        UploadFile, File(description="PDF, Word (.docx), CSV, text — or a .zip of them")
    ],
) -> DocumentUploadResponse:
    """Read the file, store its text, and write its passages as evidence.

    A `.zip` is the same job repeated. It is worth saying why it is one endpoint
    rather than two: what a person has is a folder of context, and whether they
    happened to compress it before dragging it in is not a distinction the
    product should hold an opinion about. What differs is only the failure
    shape — one bad file in an archive is that file's problem and the rest still
    land, where one bad file on its own is the whole request.
    """
    settings = get_settings()
    store = EvidenceStore(db, me.workspace_id)
    try:
        await store.assert_project(project_id)
    except EvidenceScopeError as exc:
        raise problems.not_found(str(exc)) from exc

    content = await file.read()
    filename = file.filename or "document"
    if is_archive(filename):
        unpacked = await _ingest_archive(
            db, me, project_id, filename, content, settings=settings, store=store
        )
        if not unpacked.documents:
            # Every member was refused. The reasons are the answer, and 201
            # Created would be a lie about what happened.
            response.status_code = status.HTTP_200_OK
        return unpacked

    if len(content) > settings.document_max_bytes:
        raise problems.Problem(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            title="File too large",
            detail=(
                f"That file is {_megabytes(len(content))}; the limit is "
                f"{_megabytes(settings.document_max_bytes)}."
            ),
            type_=problems.TYPE_VALIDATION,
        )
    await _assert_room(db, project_id, settings, taking=1)

    try:
        document, written = await _ingest(
            db, me, project_id, filename, content, settings=settings, store=store
        )
    except _Rejected as exc:
        raise exc.as_problem() from exc

    await db.commit()
    await db.refresh(document)
    return DocumentUploadResponse(
        documents=[_summary(document, me.user.name)],
        passages_written=written.inserted,
        duplicates=written.duplicates,
    )


async def _ingest_archive(
    db: Db,
    me: Principal,
    project_id: uuid.UUID,
    filename: str,
    content: bytes,
    *,
    settings: Settings,
    store: EvidenceStore,
) -> DocumentUploadResponse:
    """Every readable document in the archive, and the name of everything else."""
    try:
        contents = read_archive(
            content,
            supported=set(supported_extensions()),
            max_entries=settings.archive_max_entries,
            max_entry_bytes=settings.document_max_bytes,
            max_total_bytes=settings.archive_max_total_bytes,
            max_ratio=settings.archive_max_ratio,
        )
    except ArchiveError as exc:
        raise problems.unprocessable(str(exc), title="Could not read that archive") from exc

    room = await _room_left(db, project_id, settings)
    stored: list[DocumentSummary] = []
    skipped = [
        SkippedEntry(filename=item.filename, reason=item.reason) for item in contents.skipped
    ]
    passages_written = 0
    duplicates = 0

    for entry in contents.entries:
        if len(stored) >= room:
            skipped.append(
                SkippedEntry(
                    filename=entry.filename,
                    reason=(
                        f"this project already holds the most documents it can "
                        f"({settings.document_max_per_project})"
                    ),
                )
            )
            continue
        try:
            document, written = await _ingest(
                db, me, project_id, entry.filename, entry.content, settings=settings, store=store
            )
        except _Rejected as exc:
            # One member failing is that member's problem. The alternative —
            # failing the upload — throws away nine good files because the tenth
            # was a password-protected spreadsheet.
            skipped.append(SkippedEntry(filename=entry.filename, reason=exc.reason))
            continue
        await db.flush()
        stored.append(_summary(document, me.user.name))
        passages_written += written.inserted
        duplicates += written.duplicates

    await db.commit()
    log.info(
        "document.archive_uploaded",
        project_id=str(project_id),
        filename=filename,
        stored=len(stored),
        skipped=len(skipped),
    )
    return DocumentUploadResponse(
        documents=stored,
        passages_written=passages_written,
        duplicates=duplicates,
        skipped=skipped,
    )


class _Rejected(Exception):
    """This one file cannot be stored.

    Carries the HTTP shape a direct upload needs and the sentence an archive
    member needs, so the two paths cannot drift into describing the same
    refusal differently.
    """

    def __init__(
        self,
        reason: str,
        *,
        status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT,
        title: str = "Could not read that file",
        existing: ProjectDocument | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code
        self.title = title
        self.existing = existing

    def as_problem(self) -> problems.Problem:
        if self.existing is not None:
            return _already_uploaded(self.existing)
        if self.status_code == status.HTTP_413_CONTENT_TOO_LARGE:
            return problems.Problem(
                status_code=self.status_code,
                title=self.title,
                detail=self.reason,
                type_=problems.TYPE_VALIDATION,
            )
        return problems.unprocessable(self.reason, title=self.title)


async def _room_left(db: Db, project_id: uuid.UUID, settings: Settings) -> int:
    held = await db.scalar(
        sa.select(sa.func.count())
        .select_from(ProjectDocument)
        .where(ProjectDocument.project_id == project_id)
    )
    return max(0, settings.document_max_per_project - (held or 0))


async def _assert_room(db: Db, project_id: uuid.UUID, settings: Settings, *, taking: int) -> None:
    if await _room_left(db, project_id, settings) >= taking:
        return
    raise problems.Problem(
        status_code=status.HTTP_409_CONFLICT,
        title="Too many documents",
        detail=(
            f"This project already holds {settings.document_max_per_project} documents, "
            "which is the limit. Remove one before adding another."
        ),
        type_=problems.TYPE_CONFLICT,
    )


async def _ingest(
    db: Db,
    me: Principal,
    project_id: uuid.UUID,
    filename: str,
    content: bytes,
    *,
    settings: Settings,
    store: EvidenceStore,
) -> tuple[ProjectDocument, StoreResult]:
    """Store one file's text and passages. The single path both uploads take."""
    if len(content) > settings.document_max_bytes:
        raise _Rejected(
            f"it is {_megabytes(len(content))}, over the "
            f"{_megabytes(settings.document_max_bytes)} limit",
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            title="File too large",
        )

    digest = hashlib.sha256(content).hexdigest()
    existing = await _same_contents(db, project_id, digest)
    if existing is not None:
        raise _Rejected(f"the same file is already here as {existing.filename}", existing=existing)

    try:
        extracted = extract(filename, content, max_chars=settings.document_max_chars)
    except DocumentError as exc:
        raise _Rejected(str(exc)) from exc

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
    try:
        await db.flush()
    except IntegrityError as exc:
        # `uq_project_document_sha`, from a second upload of the same file that
        # got past the check above while this one was still reading. A
        # double-clicked button is the ordinary way that happens, and the
        # person who did it should see the same 409 the slower path gives them
        # rather than a 500.
        await db.rollback()
        duplicate = await _same_contents(db, project_id, digest)
        if duplicate is None:
            raise
        raise _Rejected(
            f"the same file is already here as {duplicate.filename}", existing=duplicate
        ) from exc

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
    return document, written


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


async def _same_contents(
    db: AsyncSession, project_id: uuid.UUID, digest: str
) -> ProjectDocument | None:
    """The document in this project with these exact bytes, if there is one."""
    found: ProjectDocument | None = await db.scalar(
        sa.select(ProjectDocument).where(
            ProjectDocument.project_id == project_id, ProjectDocument.sha256 == digest
        )
    )
    return found


def _already_uploaded(existing: ProjectDocument) -> problems.Problem:
    return problems.Problem(
        status_code=status.HTTP_409_CONFLICT,
        title="Already uploaded",
        detail=f"{existing.filename} has the same contents and is already in this project.",
        type_=problems.TYPE_CONFLICT,
        document_id=str(existing.id),
    )


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
