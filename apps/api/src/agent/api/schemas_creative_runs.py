"""Stage 04's per-run reads (PRD §16 "Brief, assets, lint" and "Media").

The Creative Console and the brief page render from these. Nothing here is a
verdict the browser could have computed: `can_check` is the server's answer to
"would Check again do anything, and may this caller ask", and `authorises` is
G7's own reading of the brief it hashes.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, Field

from agent.db.models import (
    CreativeAssetKind,
    CreativeAssetStatus,
    CreativeAssetVariant,
    GenerationModality,
    GenerationStatus,
)
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.guardrails import LintTarget


class BriefAuthorisation(BaseModel):
    """What approving the brief authorises (PRD §15.4 D, §15.2 rule 8)."""

    rsas: int = Field(
        description="Two per Search ad group in the brief: RSA A (4.2.3) and B (4.2.4)."
    )
    images: int = Field(description="Image jobs in the brief's media plan.")
    videos: int = Field(description="Video jobs in the brief's media plan.")
    media_usd: Decimal = Field(
        description="The media plan's estimate — what G7 authorises spend up to."
    )


class CreativeBriefResponse(BaseModel):
    """`GET /creative-runs/{id}/brief` — the brief on record and what it authorises."""

    run_id: uuid.UUID
    brief: CreativeBrief
    brief_hash: str = Field(
        description="sha256 over the canonical brief; G7 approves exactly this."
    )
    approved_hash: str | None = Field(
        description="Set once G7 approves — equal to `brief_hash`, which is then frozen."
    )
    word_count: int = Field(description="Words in the rendered brief, counted by the server.")
    max_words: int = Field(description="The one-page limit the schema enforces.")
    authorises: BriefAuthorisation
    approval_id: uuid.UUID | None = Field(
        description="The G7 gate deciding this brief. The approvals surface carries its state."
    )


class CreativeAssetItem(BaseModel):
    """One `CreativeAsset`, with the lint verdict it was stored with (law 33)."""

    id: uuid.UUID
    node_id: str
    campaign_ref: str
    ad_group_ref: str | None
    ad_ref: str | None
    kind: CreativeAssetKind
    surface: str
    variant: CreativeAssetVariant | None
    category: str | None
    text: str | None
    fields: dict[str, Any]
    claim_ids: list[uuid.UUID]
    pin_position: str | None
    generated_by_ai: bool
    status: CreativeAssetStatus
    lint_verdict: str | None = Field(
        description="`pass`, `pass_with_warnings` or `fail`, as the linter returned it at creation."
    )
    ruleset_version: str | None
    lineage: dict[str, Any]
    content_hash: str
    frozen_at: datetime | None
    created_at: datetime


class CreativeAssetListResponse(BaseModel):
    items: list[CreativeAssetItem]


class GenerationJobItem(BaseModel):
    """One media request, as the Jobs tab reads it (PRD §15.4 C)."""

    id: uuid.UUID
    node_id: str
    asset_id: uuid.UUID | None
    round: int
    modality: GenerationModality
    model_id: str
    provider_tag: str | None
    status: GenerationStatus
    estimate_usd: Decimal
    cost_usd: Decimal | None = Field(description="What OpenRouter billed. Null until it answers.")
    attempts: int
    polls: int
    error: dict[str, Any] | None
    submitted_at: datetime | None
    completed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    can_check: bool = Field(
        description=(
            "Whether `POST /generation-jobs/{id}/check` would do anything for this job and "
            "this caller may ask: a timed-out video, or an image whose submit state is "
            "unknown, for someone holding `creative_execute`."
        )
    )


class GenerationJobListResponse(BaseModel):
    items: list[GenerationJobItem]


class GenerationCheckAccepted(BaseModel):
    """`POST /generation-jobs/{id}/check` — queued for the worker, which re-polls."""

    job_id: uuid.UUID
    status: GenerationStatus = Field(description="The job's status when the check was queued.")
    queued: bool = Field(description="False when an identical check is already waiting.")


# ---------------------------------------------------------------------------
# the Ad Studio's writes (PRD §16 "Brief, assets, lint", §15.4 E)
# ---------------------------------------------------------------------------


class LintPreviewRequest(BaseModel):
    """What to lint at the run's pin. No side effects, so it is safe on a debounce."""

    targets: list[LintTarget] = Field(
        min_length=1,
        max_length=50,
        description=(
            "Each is linted as a candidate is at creation: every per-target rule its scope "
            "selects, none of the set rules (those count the assembled ad). An RSA headline "
            "is measured on its keyword-insertion default, as 4.2.1 measures it."
        ),
    )


class AssetTextEdit(BaseModel):
    """A person's rewrite of one headline or description."""

    text: str = Field(min_length=1, max_length=500)


class ReserveSwap(BaseModel):
    """Put a reserve into the ad in place of the asset the path names."""

    with_reserve_id: uuid.UUID


class ReserveSwapResponse(BaseModel):
    """Both rows after the swap: the one now kept in reserve, and the one now carried."""

    out: CreativeAssetItem
    into: CreativeAssetItem
