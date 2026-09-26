"""Wire shapes of the package routes (Stage 04 PRD §16 — package, release, Stage 05)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class ReleaseRequest(BaseModel):
    """The version the approver typed into the release dialog (§15.4 L: "v3")."""

    model_config = ConfigDict(extra="forbid")

    confirm_version: int = Field(ge=1)


class ReleaseResponse(BaseModel):
    package_id: uuid.UUID
    creative_run_id: uuid.UUID
    version: int
    status: str
    package_hash: str
    released_at: datetime
    released_by: uuid.UUID | None
    #: The package this release superseded, if the project had one released.
    superseded: list[uuid.UUID] = Field(default_factory=list)
    files: int
