"""G8 / G8b — the per-asset AI media review (Stage 04 PRD §8.5, §11 4.4.5–4.4.7).

Three static nodes, because the executor halts a gate node once:

    4.4.5 ⛳G8  (approve | reject | regenerate)
      → 4.4.6 asset_regeneration (only the `regenerate` items; not_required if none)
      → 4.4.7 ⛳G8b (approve | reject; not_required when 4.4.6 was)

`AiAssetReview` is what both gates propose and — once decided — what their
`NodeRun` carries: every item with the reviewer's `decision` bound to it. The
reviewer sends only a `ReviewSubmission` (`edited_proposal.items[]`); the
server revalidates it and merges it into the proposal (`creative/review.py`),
so the renditions a decision is recorded against are the ones on the card,
never ones a client re-typed.
"""

from __future__ import annotations

import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool

from agent.schemas.creative_input import MediaModelChoice
from agent.schemas.creative_media import ConceptMasters, Rendition
from agent.schemas.creative_video import CampaignVideo

#: `Approval.gate_key` of 4.4.5 and 4.4.7.
G8 = "G8"
G8B = "G8b"

#: Which review round each gate is — `AssetDecision.round`, and the
#: `GenerationJob.round` of what 4.4.6 regenerates.
ROUND: dict[str, Literal[1, 2]] = {G8: 1, G8B: 2}

Decision = Literal["approve", "reject", "regenerate"]
MediaKind = Literal["image", "video"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ReviewChecklist(_Frozen):
    """§15.4 H's four ticks: label correct, product matches, subjects allowed,
    rights clear. Each is `false` until a person sets it — an approval is the
    four of them, never an absence of objections. Strict: `1` or `"yes"` is not
    a tick, only JSON `true` is."""

    label_ok: StrictBool = False
    product_match_ok: StrictBool = False
    subjects_ok: StrictBool = False
    rights_ok: StrictBool = False

    def missing(self) -> list[str]:
        return [name for name, ticked in self.model_dump().items() if ticked is not True]


class ReviewRendition(_Frozen):
    """One file the reviewer is shown. Image renditions carry their lint
    verdict; video renditions their length, brand window and proxy/poster."""

    media_id: uuid.UUID
    surface: str
    ratio: str
    px: str
    derivation: str
    bytes: int
    lint_verdict: str | None = None
    duration_ms: int | None = None
    brand_first_at_ms: int | None = None
    preview_media_id: uuid.UUID | None = None
    poster_media_id: uuid.UUID | None = None


class ReviewAdvisory(_Frozen):
    """VISION's notes and flags, shown under a plain "Advisory" label. Advice
    only: nothing here decides anything."""

    notes: list[str] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)


class ItemDecision(_Frozen):
    """The reviewer's decision on one item, as the gate's `NodeRun` records it."""

    decision: Decision
    checklist: ReviewChecklist = Field(default_factory=ReviewChecklist)
    note: str | None = None
    model_override: str | None = None
    params_override: dict[str, Any] | None = None
    #: Server-written for `regenerate`: the model the regeneration uses, with
    #: the capability record it was validated against at decision time (Law 36)
    #: — the run's pinned choice, or the allowlisted override snapshotted from
    #: the live catalogue, with `params_override` merged into its defaults.
    regeneration_choice: MediaModelChoice | None = None


class ReviewItem(_Frozen):
    """One AI-made asset (§11 4.4.5): `items[]{asset_id, renditions[],
    disclosure, product_refs[], vision_advisory{notes[], flags[]}}`."""

    asset_id: uuid.UUID
    kind: MediaKind
    campaign_ref: str
    concept_id: str
    #: The asset this one was regenerated from, on a G8b item and on a G8 item
    #: an operator regenerated before review; None for a first generation.
    regenerated_from: uuid.UUID | None = None
    renditions: list[ReviewRendition] = Field(min_length=1)
    disclosure: dict[str, Any] = Field(default_factory=dict)
    product_refs: list[str] = Field(default_factory=list)
    vision_advisory: ReviewAdvisory = Field(default_factory=ReviewAdvisory)
    decision: ItemDecision | None = None


class AiAssetReview(_Frozen):
    """What G8 (round 1) and G8b (round 2) ask, and — decided — what they carry."""

    status: Literal["review", "not_required"]
    why: str | None = None
    round: Literal[1, 2]
    items: list[ReviewItem] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# what a reviewer sends
# ---------------------------------------------------------------------------


class ReviewDecisionItem(BaseModel):
    """One item of `edited_proposal.items[]` on `POST /approvals/{id}`."""

    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID
    decision: Decision
    checklist: ReviewChecklist = Field(default_factory=ReviewChecklist)
    note: str | None = Field(default=None, max_length=2000)
    model_override: str | None = Field(default=None, min_length=1)
    params_override: dict[str, Any] | None = None


class ReviewSubmission(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ReviewDecisionItem]


class ReviewDraftItem(BaseModel):
    """A decision in progress: every field optional, nothing enforced but shape."""

    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID
    decision: Decision | None = None
    checklist: ReviewChecklist = Field(default_factory=ReviewChecklist)
    note: str | None = Field(default=None, max_length=2000)
    model_override: str | None = Field(default=None, min_length=1)
    params_override: dict[str, Any] | None = None


class ReviewDraft(BaseModel):
    """`Approval.draft_state` on G8/G8b — autosaved, never a decision (§7.1)."""

    model_config = ConfigDict(extra="forbid")

    items: list[ReviewDraftItem] = Field(default_factory=list)
    #: Which review item the reviewer had open, so a reload lands on it.
    cursor: uuid.UUID | None = None


# ---------------------------------------------------------------------------
# 4.4.6
# ---------------------------------------------------------------------------


class RegeneratedAsset(_Frozen):
    """One `regenerate` item after 4.4.6: the new asset and what the pipeline
    made for it, or the gap that left nothing to review."""

    parent_asset_id: uuid.UUID
    asset_id: uuid.UUID
    kind: MediaKind
    campaign_ref: str
    concept_id: str
    model_id: str
    status: Literal["regenerated", "gap"]
    gap: str | None = None
    #: Image: the regenerated master (4.4.2's shape) and its renditions (4.4.3's).
    masters: ConceptMasters | None = None
    renditions: list[Rendition] = Field(default_factory=list)
    #: Video: the regenerated campaign video (4.4.4's shape).
    video: CampaignVideo | None = None
    product_refs: list[str] = Field(default_factory=list)


class AssetRegeneration(_Frozen):
    status: Literal["regenerated", "not_required"]
    why: str | None = None
    items: list[RegeneratedAsset] = Field(default_factory=list)
