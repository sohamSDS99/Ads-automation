"""Response models for the evidence and source endpoints.

Exported to `packages/contracts` as JSON Schema, so the Evidence Explorer's zod
types come from these definitions rather than from a second hand-written copy.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.db.models import EvidenceSource


class EvidenceItem(BaseModel):
    """One stored fact, as the Evidence Explorer renders it."""

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID | None = Field(
        default=None, description="Null when the evidence arrived outside a run, e.g. a CSV upload"
    )
    source: EvidenceSource
    kind: str = Field(description="Sub-type within the source, e.g. search_term_pnl")
    source_url: str | None = None
    content_text: str | None = Field(default=None, description="What was embedded and indexed")
    payload: dict[str, Any] = Field(default_factory=dict)
    fetched_at: datetime
    has_screenshot: bool = Field(
        default=False,
        description=(
            "A creative capture is stored for this row and can be fetched from "
            "`GET /evidence/{id}/screenshot`. Derived, so the gallery does not have to "
            "know which payload key holds a storage path."
        ),
    )

    score: float = Field(default=0.0, description="Fused relevance; 0 when not a ranked query")
    matched_by: Literal["vector", "text", "both", "filter"] = Field(
        default="filter", description="Which retriever surfaced this row"
    )
    text_rank: int | None = Field(default=None, description="Rank within full-text results")
    vector_rank: int | None = Field(default=None, description="Rank within vector results")


class EvidenceListResponse(BaseModel):
    """A page of evidence. `ranked` distinguishes a search from a browse."""

    items: list[EvidenceItem]
    next_cursor: str | None = None
    ranked: bool = Field(
        default=False, description="True when `q` was supplied and results are relevance-ordered"
    )


class CsvColumnSample(BaseModel):
    header: str
    values: list[str] = Field(default_factory=list, description="First few non-empty cells")
    suggested_field: str | None = Field(
        default=None, description="Proposed canonical field; a proposal, never auto-applied"
    )


class CsvPreviewResponse(BaseModel):
    """What the column-mapping UI needs before a human confirms a mapping."""

    filename: str
    row_count: int
    columns: list[CsvColumnSample]
    canonical_fields: list[str] = Field(description="Every field a column may be mapped to")
    required_fields: list[str]


class CsvRowError(BaseModel):
    row: int = Field(description="1-based line number in the uploaded file, header included")
    column: str
    value: str
    problem: str


class CsvIngestResponse(BaseModel):
    """The outcome of an upload: what landed, what did not, and why."""

    project_id: uuid.UUID
    outcome: Literal["won", "lost"]
    rows_accepted: int
    rows_skipped: int
    evidence_written: int = Field(description="New rows; excludes duplicates already held")
    duplicates: int
    errors: list[CsvRowError] = Field(default_factory=list)
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class ConnectorInfo(BaseModel):
    name: str
    source: EvidenceSource
    requires_credential: str | None = Field(
        default=None, description="CredentialKind this connector needs, or null if none"
    )


class ConnectorListResponse(BaseModel):
    connectors: list[ConnectorInfo]
