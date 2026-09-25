"""The outputs of 4.4.1 `creative_concepts`, 4.4.2 `image_masters` and 4.4.3
`image_renditions` (PRD §11).

What a model wrote and what code decided are kept apart on purpose: `name`,
`rationale`, `subject`, `setting`, the composition notes and the scene are the
model's; `product_depiction`, `surfaces` and every `negative_constraints` entry
code adds are not, and nothing here lets a model set them (Law 38).
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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


# ---------------------------------------------------------------------------
# 4.4.3 image_renditions
# ---------------------------------------------------------------------------

RenditionDerivation = Literal["native", "relaid", "crop"]


class Scale(_Frozen):
    """Law 39: one factor on both axes. A pair that differs is refused here as
    well as by `ck_media_artifact_uniform_scale`."""

    sx: float = Field(gt=0)
    sy: float = Field(gt=0)

    @model_validator(mode="after")
    def _uniform(self) -> Scale:
        if self.sx != self.sy:
            raise ValueError(f"sx {self.sx} != sy {self.sy}: relay out, never stretch (Law 39)")
        return self


class Rendition(_Frozen):
    concept_id: str = Field(min_length=1)
    campaign_ref: str = Field(min_length=1)
    #: The concept's image asset (4.4.2's); the file is a `rendition` artifact on it.
    asset_id: uuid.UUID
    media_id: uuid.UUID
    #: The generation job that painted the source — None for a crop of the master.
    job_id: uuid.UUID | None = None
    surface: ImageSurface
    ratio: str = Field(min_length=1)
    px: str = Field(min_length=3)
    derivation: RenditionDerivation
    scale: Scale
    retained_saliency: float = Field(ge=0.0, le=1.0)
    logo_composited: bool
    #: Why no logo was composited, when none was — recorded, never silent.
    logo_note: str | None = None
    bytes: int = Field(ge=1)
    #: The slot's byte limit — the smallest `max_bytes` of the spec-sheet asset
    #: types asking for this ratio; None when none sets one. What the Media
    #: Library prints beside `bytes` (§15.4 G). None on runs before S4-P21.
    max_bytes: int | None = None
    lint: CandidateLint
    #: `{xmp_digital_source_type, visible_labels[]}` — read back from the file.
    disclosure: dict[str, Any]


class FittedLogo(_Frozen):
    """A registered logo fitted to a spec-sheet logo slot by padding (§11 `logos[]`)."""

    campaign_ref: str = Field(min_length=1)
    asset_type: str = Field(min_length=1)
    ratio: str = Field(min_length=1)
    px: str = Field(min_length=3)
    registered_logo_id: uuid.UUID
    asset_id: uuid.UUID
    media_id: uuid.UUID
    scale: Scale
    bytes: int = Field(ge=1)
    #: The campaign's image surface and the slot's byte limit, as on a
    #: `Rendition`. None on runs before S4-P21.
    surface: str | None = None
    max_bytes: int | None = None
    lint: CandidateLint


class RenditionGap(_Frozen):
    campaign_ref: str = Field(min_length=1)
    #: None for a logo slot, which belongs to the campaign rather than a concept.
    concept_id: str | None = None
    surface: str = Field(min_length=1)
    ratio: str = Field(min_length=1)
    why: str = Field(min_length=1)


class ImageRenditions(_Frozen):
    renditions: list[Rendition] = Field(default_factory=list)
    logos: list[FittedLogo] = Field(default_factory=list)
    gaps: list[RenditionGap] = Field(default_factory=list)
