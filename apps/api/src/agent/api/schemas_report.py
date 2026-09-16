"""Request and response models for the report and export endpoints (PRD §14).

These are the envelopes. The document itself is `export.contract.ResearchReport`
and is nested whole inside `ReportResponse.payload` — the Report Viewer gets one
schema for the report and one for the wrapper, rather than a flattened copy that
would have to be kept in step by hand.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from agent.db.models import ExportFormat, ExportStatus
from agent.export.contract import ResearchReport


class ReportResponse(BaseModel):
    """`GET /reports/{run_id}`.

    `markdown` is the stored rendering, not one produced on read: PRD §11 makes
    the template deterministic so the bytes a viewer reads are the bytes the run
    produced, even if the template changes later.
    """

    id: uuid.UUID
    run_id: uuid.UUID
    project_id: uuid.UUID
    schema_version: str
    created_at: datetime
    payload: ResearchReport
    markdown: str
    exports: list[ExportJob] = Field(
        default_factory=list,
        description="Every export ever requested for this report, newest first",
    )


class ExportJob(BaseModel):
    """`POST /reports/{run_id}/export` (202) and `GET /exports/{job_id}`.

    `id` is the job id *and* the export id — one identifier, because §14 polls
    `/exports/{job_id}` and downloads `/exports/{id}/download`, and two ids for
    one thing is how a client ends up polling something it cannot fetch.
    """

    id: uuid.UUID
    report_id: uuid.UUID
    run_id: uuid.UUID
    format: ExportFormat
    status: ExportStatus
    bytes: int | None = Field(default=None, description="Set once `status` is `ready`")
    filename: str | None = Field(default=None, description="What a download will be called")
    error: str | None = Field(default=None, description="Set only when `status` is `failed`")
    created_at: datetime
    ready_at: datetime | None = None

    @property
    def job_id(self) -> uuid.UUID:
        """PRD §12 names this `job_id` in the 202 body. Same value, both names."""
        return self.id


class ExportAccepted(BaseModel):
    """The `202` body of PRD §12, which names the field `job_id`."""

    job_id: uuid.UUID
    export: ExportJob


ReportResponse.model_rebuild()
