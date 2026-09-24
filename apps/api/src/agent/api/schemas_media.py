"""Wire shapes for the media routes (PRD §16 "Media", §9.2).

A client *selects* a model — modality, id, optional provider pin, default
params — and the server snapshots the capability record and its hash from the
live catalogue (Law 36). No request carries a capability record: one that did
could claim a model supports whatever it liked.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.schemas.creative_input import CreativeScope, MediaModelSelection

Modality = Literal["image", "video"]

__all__ = ["MediaModelSelection"]

#: The request fields a default may set, per modality. Everything else a
#: request carries — model, prompt, references, frames — belongs to a job.
IMAGE_DEFAULT_FIELDS = frozenset(
    {
        "aspect_ratio",
        "resolution",
        "size",
        "quality",
        "output_format",
        "background",
        "output_compression",
        "n",
        "seed",
    }
)
VIDEO_DEFAULT_FIELDS = frozenset(
    {"duration", "resolution", "aspect_ratio", "size", "generate_audio", "seed"}
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AllowlistEntry(_Strict):
    model_id: str = Field(min_length=1)
    provider_tag: str | None = None
    enabled: bool = True


class MediaAllowlist(_Strict):
    image: list[AllowlistEntry] = Field(default_factory=list)
    video: list[AllowlistEntry] = Field(default_factory=list)


class MediaDefaults(_Strict):
    """Workspace default params per modality, applied under a project's own."""

    image: dict[str, Any] = Field(default_factory=dict)
    video: dict[str, Any] = Field(default_factory=dict)


class MediaSettings(BaseModel):
    """`GET /settings/media`. Nothing is allowlisted by default (PRD §9.2)."""

    media_allowlist: MediaAllowlist = Field(default_factory=MediaAllowlist)
    media_defaults: MediaDefaults = Field(default_factory=MediaDefaults)


class MediaSettingsUpdate(_Strict):
    """`PUT /settings/media`. A section left out is left as it is."""

    media_allowlist: MediaAllowlist | None = None
    media_defaults: MediaDefaults | None = None


class ProjectMediaModel(_Strict):
    model_id: str = Field(min_length=1)
    provider_tag: str | None = None
    defaults: dict[str, Any] = Field(default_factory=dict)


class ProjectMediaSettingsPatch(_Strict):
    """`PATCH /projects/{id}/settings/media`. `null` for a modality clears its
    default model (turning that modality off by default)."""

    media_models: dict[Modality, ProjectMediaModel | None] | None = None
    media_references_allowed: bool | None = None
    max_creative_cost_usd: str | None = Field(default=None, pattern=r"^\d+(\.\d{1,4})?$")
    max_media_cost_usd: str | None = Field(default=None, pattern=r"^\d+(\.\d{1,4})?$")


class ProjectMediaSettings(BaseModel):
    media_models: dict[str, Any] = Field(default_factory=dict)
    media_references_allowed: bool = False
    max_creative_cost_usd: str
    max_media_cost_usd: str


class MediaModelRow(BaseModel):
    """One allowlisted model, and what the live catalogue says about it."""

    modality: Modality
    model_id: str
    provider_tag: str | None
    enabled: bool
    available: bool
    #: Why it cannot be chosen, when it cannot.
    reason: Literal["media_model_unavailable", "disabled"] | None = None
    capability: dict[str, Any] | None = None
    capability_hash: str | None = None
    #: `{ratio: native|relaid|crop|gap}` against the project's spec sheet, when
    #: `project_id` was given and resolves to a published ruleset.
    ratio_coverage: dict[str, str] | None = None


class MediaModelsResponse(BaseModel):
    models: list[MediaModelRow]
    catalogue_hash: str | None = None
    warning: str | None = None


class CatalogueResponse(BaseModel):
    modality: Modality
    models: list[dict[str, Any]]
    catalogue_hash: str
    fetched_at: str
    warning: str | None = None


class EstimateRequest(_Strict):
    scope: CreativeScope
    media_models: list[MediaModelSelection] = Field(default_factory=list)


class ScopeReduction(BaseModel):
    """The smallest change to the request's own scope that fits both caps —
    what the Start dialog offers as one click (PRD §15.4 B). Only the rungs
    of the degrade ladder a start request can say (`calc.media.SCOPE_RUNGS`);
    `fits` is False when none of them is enough."""

    scope: CreativeScope
    steps: list[str]
    fits: bool
    jobs: dict[str, int]
    text_usd: float
    image_usd: float
    video_usd: float
    media_usd: float
    total_usd: float


class EstimateResponse(BaseModel):
    """`POST /projects/{id}/creative/estimate`. No spend, no job rows."""

    text_usd: float
    image_usd: float
    video_usd: float
    total_usd: float
    confidence: Literal["high", "medium", "low"]
    calc_evidence_id: uuid.UUID
    jobs: dict[str, int]
    fits: bool
    caps: dict[str, float]
    #: The smallest degrade-ladder reduction that fits both caps, when the
    #: full scope does not.
    reduction: dict[str, Any] | None = None
    #: The same answer restricted to what a start request can carry: `None`
    #: when the scope fits as it is.
    scope_reduction: ScopeReduction | None = None
    ratio_plan: dict[str, Any]
    ratio_plan_evidence_id: uuid.UUID


class MediaReferenceOut(BaseModel):
    """One `MediaReference` (PRD §10.3). Its storage path is not part of it: the
    Volume has no public address, and a reference reaches a provider only as the
    bytes Law 44 lets through."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    project_id: uuid.UUID
    kind: Literal["product_reference", "style_reference"]
    origin: Literal["own", "licensed", "third_party"]
    product_ref: str | None
    rights_statement: str
    attested_by: uuid.UUID
    attested_at: datetime
    retired_at: datetime | None
    media_type: str
    width: int
    height: int
    bytes: int
    sha256: str
    created_at: datetime
