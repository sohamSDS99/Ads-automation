"""`CreativeBrief` — the one-page brief G7 approves (Stage 04 PRD §12.1).

Law 1 applied to the brief: **every line carries at least one `SourceRef`**
back to Stage 01, 02 or 03, and a line with none fails the schema. The model
never sources a fact; it writes the words of a line and picks, from a menu the
node builds in code, which recorded sources the line stands on.

"One page" is a validator: `rendered_word_count` is measured by rendering the
brief through its Jinja template (`creative/brief.py`), never taken from the
model or from an approver's edit, and it may not exceed 600.

`brief_hash` is sha256 over the canonical JSON of everything else. G7 stores it
as `approved_hash`, and `media/jobs.py` spends nothing unless they match.
"""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from agent.schemas.creative_input import PlanRef, RuleSetRef
from agent.schemas.guardrails import ClaimRef

CREATIVE_BRIEF_SCHEMA_VERSION: Literal["1.0"] = "1.0"

#: §11 4.1.1: "rendered ≤ 600 words — 'one page' as a validator".
MAX_RENDERED_WORDS = 600


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceRef(_Frozen):
    """Where a brief line comes from: a stage, a node, and what in it."""

    stage: Literal["S1", "S2", "S3"]
    node_id: str = Field(min_length=1)
    evidence_ids: list[UUID] = Field(default_factory=list)
    rule_id: str | None = None
    field: str | None = None


class BriefLine(_Frozen):
    text: str = Field(min_length=1)
    #: Law 1, applied to the brief.
    sources: list[SourceRef] = Field(min_length=1)


class AdGroupBrief(_Frozen):
    campaign_ref: str = Field(min_length=1)
    ad_group_ref: str = Field(min_length=1)
    theme: str = Field(min_length=1)
    primary_message: BriefLine
    top_keywords: list[str] = Field(default_factory=list)
    landing_url: HttpUrl
    kpi: str = Field(min_length=1)
    #: The variant-B angle, decided up front (4.2.4 writes B from it).
    angle_b: BriefLine


class OfferBinding(_Frozen):
    """§12.2. References only — no number or date is model-written (law 35)."""

    offer_record_id: UUID
    sku_or_set: str = Field(min_length=1)
    fields: dict[str, str] = Field(default_factory=dict)
    resolved: dict[str, str] = Field(default_factory=dict)


class NonNegotiables(_Frozen):
    """From the pinned `CreativeContext`, copied in code — never model-written."""

    voice_words: list[str] = Field(default_factory=list)
    never_terms: list[str] = Field(default_factory=list)
    required_terms: list[str] = Field(default_factory=list)
    disclosures: list[str] = Field(default_factory=list)


class VisualConstraints(_Frozen):
    permitted_subjects: list[str] = Field(default_factory=list)
    forbidden_subjects: list[str] = Field(default_factory=list)
    palette_tokens: list[str] = Field(default_factory=list)
    #: Resolved in code from capability × references × law 44 (§10.3, §11 4.4.1).
    product_depiction: Literal["reference_guided", "composited_real", "none"]


class MediaPlanSummary(_Frozen):
    """What the approver is authorising spend on (G7: "and the estimated spend").

    Every figure is copied from the `media.cost_estimate_v1` and
    `media.ratio_plan_v1` results the node computed in `calc/`; the field is
    `calc_evidence_ids` so the executor checks that both are `derived` rows
    this node produced (Stage 02 §9.1 item 4).
    """

    images: bool
    video: bool
    jobs: dict[str, int]
    ratios: dict[str, dict[str, str]] = Field(default_factory=dict)
    media_usd: str
    total_usd: str
    confidence: str
    fits: bool
    calc_evidence_ids: list[UUID] = Field(min_length=1)


class CreativeBrief(_Frozen):
    schema_version: Literal["1.0"] = CREATIVE_BRIEF_SCHEMA_VERSION
    creative_run_id: UUID
    plan_ref: PlanRef
    ruleset_ref: RuleSetRef
    objective: BriefLine
    audience: list[BriefLine]
    exclusions: list[BriefLine]
    angle: BriefLine
    #: Licensed at the pin; nothing else may be asserted (law 34).
    proof_points: list[ClaimRef]
    offer: OfferBinding | None
    non_negotiables: NonNegotiables
    ad_groups: list[AdGroupBrief]
    visual_constraints: VisualConstraints
    media_plan: MediaPlanSummary
    rendered_word_count: int = Field(ge=1, le=MAX_RENDERED_WORDS)
    brief_hash: str = Field(min_length=64, max_length=64)
