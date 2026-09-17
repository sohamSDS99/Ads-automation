"""Uploading business context over HTTP: the file, the evidence, the delete.

Per PRD Law 8 every mutating route ships with a four-role authz test; the two
new ones are covered by the matrix in `test_authz_matrix.py`. What this module
proves is the part a status code cannot: that the passages a node will cite
actually exist after an upload, and actually stop existing after a delete.
"""

from __future__ import annotations

import io
import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog, Evidence, EvidenceSource, ProjectDocument
from agent.documents import BRAND_DOC

from .conftest import ApiClient

BRAND_NOTE = (
    b"# What we sell\n"
    b"SDS Manager is safety data sheet software for EHS teams at mid-sized "
    b"manufacturers. It replaces the paper binder with a searchable library.\n\n"
    b"# What it costs\n"
    b"From 49 EUR per site per month, with a free tier below fifty sheets. "
    b"Enterprise sites negotiate annually and are invoiced per location.\n"
)


def upload(content: bytes = BRAND_NOTE, filename: str = "brand.md") -> dict:
    return {"files": {"file": (filename, content, "text/markdown")}}


def docx(paragraphs: list[tuple[str, str]]) -> bytes:
    import docx as python_docx

    document = python_docx.Document()
    for style, text in paragraphs:
        document.add_paragraph(text, style=style or None)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


# --- the happy path ---------------------------------------------------------


async def test_an_upload_becomes_passages_a_node_can_cite(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    response = await admin.post(f"/projects/{project_id}/documents", **upload())
    assert response.status_code == 201, response.text

    body = response.json()
    document = body["documents"][0]
    assert document["filename"] == "brand.md"
    assert document["char_count"] > 200
    assert document["passage_count"] >= 1
    assert body["passages_written"] == document["passage_count"]
    assert document["preview"].startswith("# What we sell")
    assert document["warnings"] == []

    rows = (
        (
            await db.execute(
                sa.select(Evidence).where(
                    Evidence.project_id == project_id, Evidence.kind == BRAND_DOC
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == document["passage_count"]
    assert {row.source for row in rows} == {EvidenceSource.UPLOAD}
    assert all(row.payload["document_id"] == document["id"] for row in rows)
    # The passages have to be retrievable as well as stored: a row with no
    # embedding is invisible to the evidence search the explorer runs.
    assert all(row.embedding is not None for row in rows)
    assert all("49 EUR" in row.content_text for row in rows if "costs" in row.payload["section"])


async def test_the_library_reports_itself_with_the_limits_the_uploader_needs(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    await admin.post(f"/projects/{project_id}/documents", **upload())
    listed = await admin.get(f"/projects/{project_id}/documents")
    assert listed.status_code == 200

    body = listed.json()
    assert len(body["documents"]) == 1
    assert body["total_chars"] == body["documents"][0]["char_count"]
    assert body["max_documents"] > 0
    assert ".pdf" in body["accepted_extensions"]
    assert ".doc" not in body["accepted_extensions"]
    assert body["documents"][0]["uploaded_by_name"] == "Admin"


async def test_a_word_file_arrives_as_text(admin: ApiClient, project_id: uuid.UUID) -> None:
    content = docx(
        [
            ("Heading 1", "Objection handling"),
            ("", "They say the migration is too slow. It takes two weeks with our importer."),
        ]
    )
    response = await admin.post(
        f"/projects/{project_id}/documents",
        files={"file": ("objections.docx", content, "application/octet-stream")},
    )
    assert response.status_code == 201, response.text
    assert "two weeks with our importer" in response.json()["documents"][0]["preview"]


async def test_deleting_a_document_takes_its_passages_with_it(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    created = await admin.post(f"/projects/{project_id}/documents", **upload())
    document_id = created.json()["documents"][0]["id"]

    removed = await admin.delete(f"/projects/{project_id}/documents/{document_id}")
    assert removed.status_code == 204

    left = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Evidence)
        .where(Evidence.project_id == project_id, Evidence.kind == BRAND_DOC)
    )
    assert left == 0
    assert (await admin.get(f"/projects/{project_id}/documents")).json()["documents"] == []


async def test_both_actions_are_audited(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    created = await admin.post(f"/projects/{project_id}/documents", **upload())
    await admin.delete(f"/projects/{project_id}/documents/{created.json()['documents'][0]['id']}")

    actions = (
        (
            await db.execute(
                sa.select(AuditLog.action, AuditLog.meta)
                .where(AuditLog.target_id == project_id)
                .order_by(AuditLog.created_at)
            )
        )
        .tuples()
        .all()
    )
    recorded = {action for action, _ in actions}
    assert "project.document_uploaded" in recorded
    assert "project.document_deleted" in recorded
    uploaded_meta = next(meta for action, meta in actions if action == "project.document_uploaded")
    assert uploaded_meta["filename"] == "brand.md"
    assert uploaded_meta["evidence_written"] >= 1
    deleted_meta = next(meta for action, meta in actions if action == "project.document_deleted")
    assert deleted_meta["evidence_removed"] >= 1


# --- the refusals -----------------------------------------------------------


async def test_the_same_file_twice_is_refused_rather_than_doubled(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    first = await admin.post(f"/projects/{project_id}/documents", **upload())
    assert first.status_code == 201

    again = await admin.post(f"/projects/{project_id}/documents", **upload(filename="copy.md"))
    assert again.status_code == 409
    assert again.json()["document_id"] == first.json()["documents"][0]["id"]

    held = await db.scalar(
        sa.select(sa.func.count())
        .select_from(ProjectDocument)
        .where(ProjectDocument.project_id == project_id)
    )
    assert held == 1


async def test_a_file_we_cannot_read_is_a_422_with_a_sentence_to_act_on(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    response = await admin.post(
        f"/projects/{project_id}/documents",
        files={"file": ("brochure.doc", b"\xd0\xcf\x11\xe0 legacy word", "application/msword")},
    )
    assert response.status_code == 422
    assert "save as .docx" in response.json()["detail"]


async def test_an_empty_file_is_refused(admin: ApiClient, project_id: uuid.UUID) -> None:
    response = await admin.post(
        f"/projects/{project_id}/documents", files={"file": ("empty.txt", b"", "text/plain")}
    )
    assert response.status_code == 422


async def test_a_project_in_another_workspace_is_a_404_not_a_403(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    """Whether a project id exists is itself workspace information."""
    stranger = uuid.uuid4()
    assert (await admin.post(f"/projects/{stranger}/documents", **upload())).status_code == 404
    assert (await admin.get(f"/projects/{stranger}/documents")).status_code == 404
    assert (
        await admin.delete(f"/projects/{project_id}/documents/{uuid.uuid4()}")
    ).status_code == 404


async def test_a_viewer_cannot_upload_and_writes_nothing(
    admin: ApiClient, signed_in_as: object, db: AsyncSession, project_id: uuid.UUID
) -> None:
    viewer: ApiClient = await signed_in_as("viewer")  # type: ignore[operator]
    response = await viewer.post(f"/projects/{project_id}/documents", **upload())

    assert response.status_code == 403
    held = await db.scalar(
        sa.select(sa.func.count())
        .select_from(ProjectDocument)
        .where(ProjectDocument.project_id == project_id)
    )
    assert held == 0
    # …but they may read the library, which is a READ route.
    assert (await viewer.get(f"/projects/{project_id}/documents")).status_code == 200


async def test_an_operator_can_upload(
    admin: ApiClient, signed_in_as: object, project_id: uuid.UUID
) -> None:
    operator: ApiClient = await signed_in_as("operator")  # type: ignore[operator]
    response = await operator.post(f"/projects/{project_id}/documents", **upload())
    assert response.status_code == 201, response.text
