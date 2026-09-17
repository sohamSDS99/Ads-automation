"""Shapes for the business-context document library (PRD §14, step 1).

The list response carries the limits as well as the rows. The file picker has
to know what it may accept and how large a file may be, and a client that hard
codes those numbers is a client that disagrees with the server the first time
either changes.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

#: Characters of extracted text echoed back with the row. Enough for a person to
#: recognise their own document and see that the text came out right side up,
#: which is the one thing a filename cannot tell them.
PREVIEW_CHARS = 320


class DocumentSummary(BaseModel):
    """One uploaded document, as the wizard lists it."""

    id: uuid.UUID
    project_id: uuid.UUID
    filename: str
    media_type: str
    bytes: int
    char_count: int = Field(description="Characters of text extracted and stored")
    passage_count: int = Field(description="Citable evidence rows this document became")
    unit: str = Field(default="", description="What `unit_count` counts: page, row, paragraph")
    unit_count: int = 0
    warnings: list[str] = Field(
        default_factory=list,
        description="What the extractor could not read. Empty is the normal case.",
    )
    preview: str = Field(default="", description="The first few lines of the extracted text")
    created_at: datetime
    uploaded_by: uuid.UUID | None = None
    uploaded_by_name: str | None = None


class DocumentListResponse(BaseModel):
    """The library, plus the rules the uploader has to obey."""

    documents: list[DocumentSummary]
    total_chars: int = 0
    max_documents: int
    max_bytes: int
    accepted_extensions: list[str]


class SkippedEntry(BaseModel):
    """One file in an archive that was not taken, and why.

    Named rather than dropped: a zip of twelve files that quietly becomes three
    documents is worse than an error, because nothing on the screen says the
    other nine are missing.
    """

    filename: str
    reason: str


class DocumentUploadResponse(BaseModel):
    """What one upload produced — one document, or the contents of an archive."""

    documents: list[DocumentSummary] = Field(
        description="Everything stored by this upload. One entry for a plain file."
    )
    passages_written: int = Field(
        description="New evidence rows. Fewer than `passage_count` means some already existed."
    )
    duplicates: int = 0
    skipped: list[SkippedEntry] = Field(
        default_factory=list, description="Archive members that were not stored, and why"
    )
