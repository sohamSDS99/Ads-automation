"""Wire shapes of the package routes (Stage 04 PRD §16 — package, release, Stage 05)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.schemas.creative_package import CreativeCritique, CreativePackage, CritiqueIssue


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


# ---------------------------------------------------------------------------
# reading one package — the package screen, the canonical released page
# ---------------------------------------------------------------------------

Gate = Literal["G7", "G8", "G8b", "H3"]


class ChecklistItem(BaseModel):
    """One of §11's thirteen blocking checks as 4.7.2 ran it."""

    check: str
    title: str
    passed: bool
    #: The check's blocking findings, as 4.7.2 recorded them. Empty when it passed.
    issues: list[CritiqueIssue] = Field(default_factory=list)


class ReleaseStop(BaseModel):
    """One of the three stops (law 40) as the package records it."""

    gate: Gate
    #: G7/G8/G8b: `approved | rejected | pending | expired`, or `not_required`
    #: when the gate never opened (no AI media ⇒ no G8). H3: `not_required |
    #: required | decided`.
    status: str
    decided_by: uuid.UUID | None = None
    decided_by_name: str | None = None
    decided_at: datetime | None = None
    #: A plain count of what was decided: "3 exceptions: 2 cleared, 1 withdrawn".
    detail: str = ""


class ReleasePreview(BaseModel):
    """Everything the release dialog states before the approver types the version."""

    #: `status == ready_to_release` — the one state release's guard admits.
    releasable: bool
    #: `max(version) + 1` over the project's packages, now — what release would
    #: mint. None once released: a released package has its version.
    version_to_mint: int | None
    #: Why it cannot be released, in the words the screen shows. None when it can.
    reason: str | None = None
    stops: list[ReleaseStop] = Field(default_factory=list)


class PackageView(BaseModel):
    """`GET /creative-runs/{id}/package` and `GET /creative-packages/{id}`.

    The package as stored (its row's status included), 4.7.2's critique and
    the thirteen checks it ran, and what a release would do — all computed
    here, so the screen renders the server's checklist and never its own.
    """

    package: CreativePackage
    row_version: int
    package_hash: str | None
    released_at: datetime | None
    released_by: uuid.UUID | None
    released_by_name: str | None
    plan_superseded: bool
    ruleset_superseded: bool
    created_at: datetime
    updated_at: datetime
    #: 4.7.2's output. None until 4.7.2 has run.
    critique: CreativeCritique | None
    #: The thirteen, in §11's order. Empty until 4.7.2 has run.
    checklist: list[ChecklistItem] = Field(default_factory=list)
    release: ReleasePreview
