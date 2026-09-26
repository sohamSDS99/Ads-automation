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
    LandingAuditVerdict,
    PreviewDevice,
    PreviewVerdict,
)
from agent.schemas.creative_brief import CreativeBrief, OfferBinding
from agent.schemas.creative_qa import ConformanceCheck, Unchecked
from agent.schemas.guardrails import LintTarget
from agent.schemas.landing import (
    DeviceFlag,
    DevicePx,
    DeviceText,
    FormAudit,
    MessageMatch,
    OfferAboveFold,
    ProposedH1,
    Screenshots,
)


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


class OfferSource(BaseModel):
    """The `OfferRecord` a bound asset was rendered from (law 35, PRD §15.4 F).

    Read from the run's pinned offer snapshot — the observation whose figures
    the binding holds — so the window is the one the asset was written against.
    `evidence_id` is the `offer_record` row that observation came from, for the
    link; None when that row is no longer stored. Dates without a zone are read
    as UTC, as `creative/offers.py` ages them.
    """

    evidence_id: uuid.UUID | None
    sku: str
    product_set: str
    market: str
    effective_from: datetime | None
    effective_to: datetime | None
    ends_at: datetime | None
    observed_at: datetime | None


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
    offer_binding: OfferBinding | None = Field(
        default=None,
        description="A promotion's or price's figures, each an `OfferRecord` field reference.",
    )
    offer: OfferSource | None = Field(
        default=None,
        description="The record `offer_binding` was rendered from; None when unbound or not found.",
    )


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


class RegenerateRequest(BaseModel):
    """`POST /creative-assets/{id}/regenerate` — an operator's, before G8 (§16)."""

    model_config = {"extra": "forbid"}

    note: str = Field(
        min_length=1, max_length=2000, description="What the regeneration should change."
    )
    model_override: str | None = Field(
        default=None, min_length=1, description="Another allowlisted model of the asset's modality."
    )
    params_override: dict[str, Any] | None = Field(
        default=None, description="Default params for this regeneration, capability-validated."
    )


class RegenerateAccepted(BaseModel):
    """Queued. The new asset is produced by the worker through node 4.4.6's code;
    its generation jobs appear under `GET /creative-runs/{id}/generation-jobs`
    with `asset_id` = `asset_id` below, and it takes the old one's place on the
    G8 card when it has something to review."""

    job_id: str | None = Field(description="The queue job; null when it was already queued.")
    asset_id: uuid.UUID = Field(description="The regenerated asset being produced.")
    parent_asset_id: uuid.UUID
    model_id: str
    estimate_usd: Decimal
    media_remaining_usd: Decimal = Field(description="Media budget left before this regeneration.")
    total_remaining_usd: Decimal = Field(
        description="Creative budget left before this regeneration."
    )


class LandingAuditItem(BaseModel):
    """One landing URL as 4.5.1 and 4.5.2 audited it (PRD §15.4 J).

    Read from the stored `landing_page_audit` row; the verdict and every score
    are the nodes', never re-derived. `has_patch` says whether
    `GET /landing-audits/{id}/patch` has anything to return.
    """

    id: uuid.UUID
    creative_run_id: uuid.UUID
    url: str
    final_url: str | None
    http_status: int | None
    ad_group_refs: list[str]
    verdict: LandingAuditVerdict
    reasons: list[str] = Field(default_factory=list)
    h1: DeviceText
    fold_px: DevicePx
    obscured_by_overlay: DeviceFlag
    message_match: MessageMatch | None = None
    proposed_h1: ProposedH1 | None = None
    proposed_h1_note: str | None = None
    offer_above_fold: list[OfferAboveFold] = Field(default_factory=list)
    form: FormAudit | None = None
    screenshots: Screenshots = Field(
        description="StorageBackend keys of the full-page screenshots, per device."
    )
    has_patch: bool
    evidence_ids: list[uuid.UUID]
    created_at: datetime


class LandingAuditListResponse(BaseModel):
    items: list[LandingAuditItem]


# ---------------------------------------------------------------------------
# final checks (4.6.1, 4.6.4) — PRD §16 "Landing and QA"
# ---------------------------------------------------------------------------


class RenderPreviewItem(BaseModel):
    """One `RenderPreview`: one RSA combination on one device (§11 4.6.4)."""

    id: uuid.UUID
    creative_run_id: uuid.UUID
    ad_ref: str
    device: PreviewDevice
    #: `{roles[], headlines[], descriptions[], paths[], likelihood, model}`.
    combination: dict[str, Any]
    #: False when the render did not happen (`verdict='unavailable'`, or a
    #: `blocking` spec failure the browser never drew).
    has_screenshot: bool
    #: `{truncated[], overflow_px[], elements[], requests}` — advisory (D12).
    dom_metrics: dict[str, Any]
    #: `{missing[], extra[], mismatched[], unchecked[]}` — the only source of `blocking`.
    spec_diff: dict[str, Any]
    visual_diff: dict[str, Any] | None
    template_version: str
    verdict: PreviewVerdict
    created_at: datetime


class RenderPreviewListResponse(BaseModel):
    items: list[RenderPreviewItem]


class ConformanceResponse(BaseModel):
    """4.6.1's checks as the node recorded them; `verdict` filters `checks`.
    `unchecked[]` is always returned: an asset nothing could be checked against
    is neither a pass nor a fail, and must not vanish behind a filter."""

    ruleset_version: str
    checks: list[ConformanceCheck]
    unchecked: list[Unchecked]
    failed: int


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
