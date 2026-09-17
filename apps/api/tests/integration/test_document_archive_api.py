"""Uploading a zip of business context, through the API.

The claim under test is that an archive is the same job repeated: what lands in
the library, what becomes citable evidence, and what the person is told about
the members that did not make it. One bad file inside an archive is that file's
problem — the rest still land — which is the one behaviour that differs from a
direct upload, and the one most likely to be got wrong by failing the request.
"""

from __future__ import annotations

import zipfile
from io import BytesIO
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Evidence
from tests.integration.conftest import ApiClient

PRICING = b"Pricing\n\nFrom EUR 49 per month per site. Free under 50 sheets.\n" * 6
POSITIONING = b"# Positioning\n\nSDS Manager replaces binders with a searchable library.\n" * 6


def archive(*files: tuple[str, bytes]) -> bytes:
    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zipped:
        for name, content in files:
            zipped.writestr(name, content)
    return buffer.getvalue()


def upload(content: bytes, filename: str = "context.zip") -> dict[str, Any]:
    return {"files": {"file": (filename, content, "application/zip")}}


async def test_an_archive_becomes_one_document_per_readable_file(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    response = await admin.post(
        f"/projects/{project.id}/documents",
        **upload(archive(("pricing.txt", PRICING), ("docs/positioning.md", POSITIONING))),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert [document["filename"] for document in body["documents"]] == [
        "pricing.txt",
        "positioning.md",
    ]
    assert body["passages_written"] >= 2
    assert body["skipped"] == []

    # The point of a document is that a node can cite it, so the evidence rows
    # are what proves the upload did anything.
    rows = await db.scalar(
        sa.select(sa.func.count())
        .select_from(Evidence)
        .where(Evidence.project_id == project.id, Evidence.kind == "brand_doc")
    )
    assert (rows or 0) >= 2


async def test_the_library_lists_everything_the_archive_added(
    admin: ApiClient, project: Any
) -> None:
    await admin.post(
        f"/projects/{project.id}/documents",
        **upload(archive(("pricing.txt", PRICING), ("positioning.md", POSITIONING))),
    )

    listing = await admin.get(f"/projects/{project.id}/documents")

    assert listing.status_code == 200
    names = {document["filename"] for document in listing.json()["documents"]}
    assert names == {"pricing.txt", "positioning.md"}
    assert ".zip" in listing.json()["accepted_extensions"], "the picker has to offer it"


async def test_one_unreadable_member_does_not_cost_the_others(
    admin: ApiClient, project: Any
) -> None:
    """The alternative — failing the upload — throws away nine good files
    because the tenth was a spreadsheet."""
    response = await admin.post(
        f"/projects/{project.id}/documents",
        **upload(
            archive(
                ("pricing.txt", PRICING),
                ("forecast.xlsx", b"PK\x03\x04 not really"),
                ("logo.png", b"\x89PNG\r\n"),
            )
        ),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert [document["filename"] for document in body["documents"]] == ["pricing.txt"]
    assert {item["filename"] for item in body["skipped"]} == {"forecast.xlsx", "logo.png"}


async def test_an_archive_that_stored_nothing_does_not_claim_it_created_something(
    admin: ApiClient, project: Any
) -> None:
    response = await admin.post(
        f"/projects/{project.id}/documents",
        **upload(archive(("logo.png", b"\x89PNG\r\n"), ("sheet.xlsx", b"PK"))),
    )

    assert response.status_code == 200, "201 Created would be a lie"
    body = response.json()
    assert body["documents"] == []
    assert len(body["skipped"]) == 2


async def test_the_same_file_twice_inside_one_archive_is_stored_once(
    admin: ApiClient, project: Any
) -> None:
    """Content-addressed, so a folder holding the same pricing sheet in two
    places does not become two documents saying the same thing twice."""
    response = await admin.post(
        f"/projects/{project.id}/documents",
        **upload(archive(("a/pricing.txt", PRICING), ("b/pricing.txt", PRICING))),
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["documents"]) == 1
    assert "already here" in body["skipped"][0]["reason"]


async def test_a_zip_bomb_is_refused_with_a_sentence(admin: ApiClient, project: Any) -> None:
    response = await admin.post(
        f"/projects/{project.id}/documents", **upload(archive(("bomb.txt", b"\x00" * 4_000_000)))
    )

    assert response.status_code == 422, response.text
    assert "expands" in response.json()["detail"]


async def test_something_that_is_not_a_zip_is_refused_as_an_archive(
    admin: ApiClient, project: Any
) -> None:
    response = await admin.post(
        f"/projects/{project.id}/documents", **upload(b"%PDF-1.4 not a zip at all")
    )

    assert response.status_code == 422, response.text
    assert "not a readable" in response.json()["detail"]


async def test_a_plain_file_still_uploads_the_way_it_always_did(
    admin: ApiClient, project: Any
) -> None:
    """The archive path must not have changed what a single file does."""
    response = await admin.post(
        f"/projects/{project.id}/documents",
        files={"file": ("pricing.txt", PRICING, "text/plain")},
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert len(body["documents"]) == 1
    assert body["documents"][0]["filename"] == "pricing.txt"
    assert body["skipped"] == []
