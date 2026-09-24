"""The outputs of 4.4.1 `creative_concepts` and 4.4.2 `image_masters` (PRD §11).

What a model wrote and what code decided are kept apart on purpose: `name`,
`rationale`, `subject`, `setting`, the composition notes and the scene are the
model's; `product_depiction`, `surfaces` and every `negative_constraints` entry
code adds are not, and nothing here lets a model set them (Law 38).
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

Depiction = Literal["reference_guided", "composited_real", "none"]
ImageSurface = Literal["search_image", "pmax_image", "display_image", "demand_gen_image"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# 4.4.1 creative_concepts
# ---------------------------------------------------------------------------


class Concept(_Frozen):
    id: str = Field(min_length=1)
    campaign_ref: str = Field(min_length=1)
    name: str = Field(min_length=1)
    #: Which brief angle the concept is built on, by key (`angle`,
    #: `{ad_group_ref}:primary`, `{ad_group_ref}:angle_b`) and in the brief's words.
    angle: str = Field(min_length=1)
    angle_text: str = Field(min_length=1)
    rationale: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    setting: str = Field(min_length=1)
    #: One note per ratio the campaign requires, keyed by ratio label.
    composition_by_ratio: dict[str, str] = Field(default_factory=dict)
    palette_tokens: list[str] = Field(default_factory=list)
    #: Resolved in code from capability × references × Law 44. Never a model's.
    product_depiction: Depiction
    #: What is sent: the model's scene plus every code-owned negative.
    prompt: str = Field(min_length=1)
    negative_constraints: list[str] = Field(default_factory=list)
    surfaces: list[ImageSurface] = Field(default_factory=list)


class CampaignConcepts(_Frozen):
    campaign_ref: str = Field(min_length=1)
    campaign_type: str = Field(min_length=1)
    concepts: list[Concept]


class CreativeConcepts(_Frozen):
    product_depiction: Depiction
    #: Why: every snapshot reference and the Law 44 clause it failed (None =
    #: it may be sent). What a reviewer and an auditor read (§13).
    reference_refusals: dict[str, str | None] = Field(default_factory=dict)
    campaigns: list[CampaignConcepts] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 4.4.2 image_masters
# ---------------------------------------------------------------------------


class CandidateLint(_Frozen):
    #: `image_verdict()`: pass | pass_with_warnings | fail | indeterminate.
    verdict: str
    unchecked: bool
    ruleset_version: str
    rule_ids: list[str] = Field(default_factory=list)


class VisionAdvisory(_Frozen):
    """VISION's notes on one candidate. Advisory: nothing blocks on it."""

    note: str
    flags: list[str] = Field(default_factory=list)


class Candidate(_Frozen):
    job_id: uuid.UUID
    media_id: uuid.UUID
    seed: int | None = None
    #: 1 for the first attempt, 2 for the one retry with strengthened negatives.
    attempt: Literal[1, 2]
    lint: CandidateLint
    #: None for a candidate discarded by lint — it was never shown to VISION.
    vision_advisory: VisionAdvisory | None = None


class Master(_Frozen):
    media_id: uuid.UUID
    why: str = Field(min_length=1)


class ConceptGap(_Frozen):
    reason: Literal["all_candidates_failed_lint", "generation_failed", "blocked_by_budget"]
    detail: str = Field(min_length=1)


class ConceptMasters(_Frozen):
    concept_id: str = Field(min_length=1)
    campaign_ref: str = Field(min_length=1)
    asset_id: uuid.UUID
    aspect_ratio: str | None
    reference_sha256s: list[str] = Field(default_factory=list)
    candidates: list[Candidate] = Field(default_factory=list)
    master: Master | None = None
    gap: ConceptGap | None = None


class ImageMasters(_Frozen):
    concepts: list[ConceptMasters] = Field(default_factory=list)
