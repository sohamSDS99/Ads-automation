"""Wire shapes for Stage 04's gated entry (PRD §4.2, §16).

Two lists, as in Stage 03, and for a sharper reason here: Stage 04 is gated,
so the blockers are the stage. CR-E1 and CR-E2 are blockers and never
warnings (law 32) — a separate `warnings[]` is what makes it impossible for a
client to render one as the other by filtering on a severity field.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.db.models import CreativePackageStatus, RunStatus
from agent.schemas.creative_input import CreativeScope, MediaModelSelection

CreativeBlockerCode = Literal[
    "no_frozen_plan",  # CR-E1
    "no_published_ruleset",  # CR-E2
    "schema_unsupported",  # CR-E3
    "ruleset_incomplete",  # CR-E4
    "no_signoff_matrix",  # CR-E5
    "creative_in_flight",  # CR-E6
    "missing_credential",  # CR-E7
    "media_model_unselected",  # CR-E8
    "media_model_not_allowlisted",  # CR-E8
    "media_model_unavailable",  # CR-E8
    "capability_unsupported",  # CR-E8: a default the chosen model does not take
    "media_model_out_of_scope",  # CR-E8: a model for a modality the scope has off
    "estimate_exceeds_cap",  # CR-E9
    "estimate_unavailable",  # CR-E9: a model whose price cannot be computed
    "zdr_blocks_video",  # CR-E10
    "storage_insufficient",  # CR-E11
    "missing_permission",  # CR-E15, on the read endpoint
]
CreativeWarningCode = Literal[
    "ruleset_category_missing",  # CR-E4
    "claims_unlicensed_stale",  # CR-E12
    "unreviewed_amendments",  # CR-E12
    "verification_open_blocks_launch",  # CR-E12
    "offer_data_stale",  # CR-E13
    "will_mint_new_version",  # CR-E14
]


class CreativeBlocker(BaseModel):
    code: CreativeBlockerCode
    detail: str
    #: Relative, always.
    fix_url: str
    #: CR-E8: the modality the blocker is about.
    modality: Literal["image", "video"] | None = None
    #: CR-E9: the estimate, the caps it breaches, and the smallest
    #: degrade-ladder reduction that fits (None when nothing does).
    estimate: dict[str, Any] | None = None
    cap: dict[str, float] | None = None
    reduction: dict[str, Any] | None = None


class CreativeWarning(BaseModel):
    code: CreativeWarningCode
    detail: str
    fix_url: str


class CreativeEligibility(BaseModel):
    """`GET /projects/{id}/creative/eligibility`. `eligible` = no blockers."""

    eligible: bool
    blockers: list[CreativeBlocker] = Field(default_factory=list)
    warnings: list[CreativeWarning] = Field(default_factory=list)
    #: What a run started now would pin (§4.4): the plan, the ruleset and the
    #: creative context. Present only for the halves that resolved.
    pins: dict[str, str | int | None] = Field(default_factory=dict)
    #: CR-E9's pre-flight estimate, when every enabled modality resolved.
    estimate: dict[str, Any] | None = None


class StartCreativeRequest(BaseModel):
    scope: CreativeScope
    #: One selection per enabled modality; the server snapshots each one's
    #: capability record from the live catalogue (Law 36).
    media_models: list[MediaModelSelection] = Field(default_factory=list)
    reuse_cache: bool = True


class CreativeRunAccepted(BaseModel):
    run_id: uuid.UUID
    status: RunStatus
    input_hash: str


class CreativeRunSummary(BaseModel):
    run_id: uuid.UUID
    status: RunStatus
    source_run_id: uuid.UUID | None
    input_hash: str | None
    pins: list[dict[str, str]] = Field(default_factory=list)
    triggered_by: uuid.UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    cost_usd: Decimal


class CreativePackageSummary(BaseModel):
    package_id: uuid.UUID
    creative_run_id: uuid.UUID
    version: int
    status: CreativePackageStatus
    plan_version: int
    ruleset_version: str
    released_at: datetime | None
    plan_superseded: bool
    ruleset_superseded: bool


class CreativeOverview(BaseModel):
    """`GET /projects/{id}/creative` — runs and package history, newest first."""

    runs: list[CreativeRunSummary] = Field(default_factory=list)
    packages: list[CreativePackageSummary] = Field(default_factory=list)
