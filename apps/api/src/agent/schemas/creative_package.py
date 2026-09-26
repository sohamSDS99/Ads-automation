"""Stage 4.7 — the `CreativePackage`, the one thing Stage 05 reads (Stage 04 PRD §12.2, §12.3).

`CreativePackage` is §12.3's contract, field for field. The asset types it
carries are §12.2's, with three differences a package has to have and a
node's output does not (docs/stage-04-questions.md § S4-P16):

* **`CampaignCreative.text_assets`.** §12.3 lists ads, asset groups and
  extensions by asset id and nowhere holds the text those ids name — and
  Stage 05 never reads a `CreativeAsset` row. Every text asset a campaign
  ships is here once; the ads, asset groups and extensions refer to it.
* **`ResponsiveSearchAd.pair_report` is a `ShippedPairs`**, not 4.2.3's
  `PairReport`. A package describes the ad that ships, and after 4.2.3 judged
  an ad an H3 fallback or an operator's reserve swap can change it. Its pairs
  are re-flagged by `combinatorics.pair_flags_v1` over the shipped text; a
  pair 4.2.3 or 4.2.4 read keeps that reading and a pair nobody read says so
  (`label=None`) instead of inheriting one.
* **`MediaRendition.path`** — the manifest path of the file, so a rendition
  can be found among the files Stage 05 is handed.

`package_hash` is sha256 over the sorted manifest and the canonical payload
minus `package_hash` and `status` (`creative/package.package_hash`). Status
is the one field that moves after release (released → superseded), so a
hash over it could not be recomputed from an export of a superseded package.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from agent.schemas.creative_brief import OfferBinding
from agent.schemas.search_ads import Category, PairFlag, PairKind, PairLabel, PinPosition, Variant

PACKAGE_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

PackageStatus = Literal["draft", "blocked", "ready_to_release", "released", "superseded"]
IssueSeverity = Literal["blocking", "warning", "note"]
#: What an open dependency stops. `launch` is §11 check 11's word; `none` is
#: something to do that the campaigns can run without.
BlockingFor = Literal["launch", "none"]
TextKind = Literal[
    "headline",
    "long_headline",
    "description",
    "path",
    "sitelink",
    "callout",
    "structured_snippet",
    "promotion",
    "price",
    "lead_form",
    "business_name",
    "video_script",
]
MediaModality = Literal["image", "video", "logo"]
LintVerdictRef = Literal["pass", "pass_with_warnings", "fail", "indeterminate", "unlinted"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------------------
# pins and assets (§12.2)
# ---------------------------------------------------------------------------


class Pins(_Frozen):
    plan_id: uuid.UUID
    plan_version: int = Field(ge=1)
    #: The final pin: the start pin, or the MINOR an H3 clearance repinned to.
    ruleset_version: str = Field(min_length=1)
    context_hash: str = Field(min_length=1)
    constants_version: str = Field(min_length=1)
    #: `media.catalogue.catalogue_hash` over the capability records the run
    #: pinned (`CreativeInput.media_models`) — the run pins those, never the
    #: whole live catalogue.
    catalogue_hash: str = Field(min_length=1)


class LintResultRef(_Frozen):
    """An asset's lint, as its row holds it at the pin it was reached at."""

    verdict: LintVerdictRef
    ruleset_version: str | None = None
    rule_ids: list[str] = Field(default_factory=list)


class TextAsset(_Frozen):
    asset_id: uuid.UUID
    kind: TextKind
    surface: str = Field(min_length=1)
    campaign_ref: str = Field(min_length=1)
    ad_group_ref: str | None = None
    text: str | None = None
    fields: dict[str, Any] = Field(default_factory=dict)
    category: Category | None = None
    #: ⊆ licensed(final pin); at least one on a description (§11 check 4).
    claim_ids: list[uuid.UUID] = Field(default_factory=list)
    offer_binding: OfferBinding | None = None
    pin_position: PinPosition | None = None
    variant: Variant | None = None
    lint: LintResultRef
    #: The pin the asset's lint was reached at. None only on an asset nothing
    #: linted — which §11 check 2 then reports rather than this model refusing.
    ruleset_version: str | None = None
    lineage: dict[str, Any] = Field(default_factory=dict)
    generated_by_ai: bool


class ShippedPair(_Frozen):
    a: uuid.UUID
    b: uuid.UUID
    kind: PairKind
    #: `combinatorics.pair_flags_v1` over the text that ships.
    flags: list[PairFlag] = Field(default_factory=list)
    #: 4.2.3's (or 4.2.4's) reading of this pair; None when no node read it.
    label: PairLabel | None = None


class ShippedPairs(_Frozen):
    flags_version: Literal["combinatorics.pair_flags_v1"] = "combinatorics.pair_flags_v1"
    #: The ad is exactly the combination 4.2.3 / 4.2.4 judged, in order.
    judged: bool
    pairs: list[ShippedPair] = Field(default_factory=list)
    #: Every pair with a deterministic flag (§11 check 3).
    unresolved: list[tuple[uuid.UUID, uuid.UUID]] = Field(default_factory=list)


class ResponsiveSearchAd(_Frozen):
    ad_ref: str = Field(min_length=1)
    campaign_ref: str = Field(min_length=1)
    ad_group_ref: str = Field(min_length=1)
    variant: Variant
    angle: str = Field(min_length=1)
    #: Required when variant='B' (§11 check 5 asserts it).
    hypothesis: str | None = None
    headlines: list[uuid.UUID] = Field(default_factory=list)
    descriptions: list[uuid.UUID] = Field(default_factory=list)
    paths: tuple[str | None, str | None] = (None, None)
    final_url: str = Field(min_length=1)
    pair_report: ShippedPairs
    #: `copy.distinctness_v1` from the shipped variant A, over the shipped text.
    distinctness_vs_a: float | None = Field(default=None, ge=0.0, le=1.0)


class AssetGroupCreative(_Frozen):
    """One Performance Max / Demand Gen / Display asset group: its text and media."""

    campaign_ref: str = Field(min_length=1)
    ad_group_ref: str = Field(min_length=1)
    campaign_type: str
    headlines: list[uuid.UUID] = Field(default_factory=list)
    long_headlines: list[uuid.UUID] = Field(default_factory=list)
    descriptions: list[uuid.UUID] = Field(default_factory=list)
    business_name: uuid.UUID | None = None
    #: The campaign's media assets (images, videos, logos), by asset id.
    media: list[uuid.UUID] = Field(default_factory=list)


class Extensions(_Frozen):
    sitelinks: list[uuid.UUID] = Field(default_factory=list)
    callouts: list[uuid.UUID] = Field(default_factory=list)
    snippets: list[uuid.UUID] = Field(default_factory=list)
    promotions: list[uuid.UUID] = Field(default_factory=list)
    #: One text asset per price ITEM; `fields.price_asset_id` groups them.
    prices: list[uuid.UUID] = Field(default_factory=list)
    lead_form: uuid.UUID | None = None


class VideoFacts(_Frozen):
    duration_ms: int = Field(ge=1)
    brand_first_at_ms: int = Field(ge=0)
    captions_burned: bool
    caption_ocr_min_similarity: float | None = None
    has_audio: bool
    loudness_lufs: float | None = None


class MediaRendition(_Frozen):
    media_id: uuid.UUID
    #: The manifest path of this file.
    path: str = Field(min_length=1)
    surface: str = Field(min_length=1)
    aspect_ratio: str = Field(min_length=1)
    width: int = Field(ge=1)
    height: int = Field(ge=1)
    bytes: int = Field(ge=1)
    media_type: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    derivation: str = Field(min_length=1)
    #: (sx, sy): 1.0 each on a native file (§11 check 9: sx == sy).
    scale: tuple[float, float] = (1.0, 1.0)
    logo_composited: bool = False
    lint: LintResultRef
    disclosure: dict[str, Any] | None = None
    duration_ms: int | None = None
    #: A video rendition's own verification facts (§11 check 10), per file.
    video: VideoFacts | None = None


class Provenance(_Frozen):
    model_id: str | None = None
    provider: str | None = None
    seed: int | None = None
    #: sha256 of the prompt the first job sent, when the request kept one.
    prompt_hash: str | None = None
    reference_sha256s: list[str] = Field(default_factory=list)
    job_ids: list[uuid.UUID] = Field(default_factory=list)
    openrouter_generation_ids: list[str] = Field(default_factory=list)
    #: None while any job has no reconciled cost — incomplete provenance.
    cost_usd: Decimal | None = None


class AssetReviewRef(_Frozen):
    """The decision that settled the asset: G8b's when it had one, else G8's."""

    gate: Literal["G8", "G8b"] | None = None
    round: int | None = None
    decision: Literal["approve", "reject", "regenerate"] | None = None
    decider: uuid.UUID | None = None
    decided_at: datetime | None = None


class MediaAsset(_Frozen):
    asset_id: uuid.UUID
    modality: MediaModality
    campaign_ref: str = Field(min_length=1)
    concept_id: str | None = None
    generated_by_ai: bool
    renditions: list[MediaRendition] = Field(default_factory=list)
    provenance: Provenance
    product_depiction: Literal["reference_guided", "composited_real", "none"] = "none"
    #: The worst of the asset's renditions: the longest, the latest brand, the
    #: lowest caption OCR; captions burned only when burned in every file.
    video: VideoFacts | None = None
    review: AssetReviewRef = Field(default_factory=AssetReviewRef)


# ---------------------------------------------------------------------------
# the package (§12.3)
# ---------------------------------------------------------------------------


class RequiredCount(_Frozen):
    asset_type: str = Field(min_length=1)
    required: int = Field(ge=1)
    #: The fewest any container of the campaign carries: each ad for RSA text,
    #: each asset group for asset-group text, the campaign for everything else.
    present: int = Field(ge=0)
    met: bool


class MinimumCheck(_Frozen):
    """3.4.2's launch minimum set, derived from the final pin's spec sheet."""

    campaign_type: str
    required: list[RequiredCount] = Field(default_factory=list)
    met: bool
    #: The campaign type's specs name no minimum at all (3.4.2 `no_stated_minimum`).
    no_stated_minimum: bool = False


class CampaignCreative(_Frozen):
    campaign_ref: str = Field(min_length=1)
    campaign_type: str
    text_assets: list[TextAsset] = Field(default_factory=list)
    ads: list[ResponsiveSearchAd] = Field(default_factory=list)
    asset_groups: list[AssetGroupCreative] = Field(default_factory=list)
    extensions: Extensions = Field(default_factory=Extensions)
    media: list[MediaAsset] = Field(default_factory=list)
    logos: list[MediaAsset] = Field(default_factory=list)
    launch_minimums: MinimumCheck


class LandingPatchRef(_Frozen):
    audit_id: uuid.UUID
    url: str
    ad_group_refs: list[str] = Field(default_factory=list)
    verdict: Literal["ok", "needs_change", "blocking_for_launch", "unreachable"]
    #: Manifest paths of the patch's JSON and HTML.
    json_path: str
    html_path: str


class GateDecision(_Frozen):
    gate_key: Literal["G7", "G8", "G8b"]
    approval_id: uuid.UUID
    node_id: str
    status: Literal["approved", "rejected", "pending", "expired"]
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    note: str = ""


class HumanTaskRef(_Frozen):
    task_id: uuid.UUID | None = None
    task_key: Literal["H3"] = "H3"
    #: 4.6.3's status: not_required, required (open) or decided.
    status: Literal["not_required", "required", "decided"]
    #: The person-task row's own status, when there is one.
    task_status: str | None = None
    assignee_id: uuid.UUID | None = None
    blocking_for: BlockingFor = "launch"


class ExceptionRef(_Frozen):
    exception_id: uuid.UUID
    kind: Literal["new_claim", "disclaimer", "image_right"]
    status: Literal["open", "cleared", "rejected", "withdrawn"]
    subject: str | None = None
    asset_ids: list[uuid.UUID] = Field(default_factory=list)
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None


class Dependency(_Frozen):
    kind: Literal["youtube_upload", "landing_patch", "inherited"]
    task: str = Field(min_length=1)
    owner: str = "unassigned"
    blocking_for: BlockingFor
    #: The campaigns it stops. Empty means the project's, not a campaign's.
    campaign_refs: list[str] = Field(default_factory=list)
    source: str = ""


class LintCount(_Frozen):
    verdict: LintVerdictRef
    count: int = Field(ge=0)


class RuleCount(_Frozen):
    rule_id: str
    count: int = Field(ge=0)


class LintSummary(_Frozen):
    ruleset_version: str
    by_verdict: list[LintCount] = Field(default_factory=list)
    by_rule: list[RuleCount] = Field(default_factory=list)


class ManifestEntry(_Frozen):
    path: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    bytes: int = Field(ge=0)
    media_type: str = Field(min_length=1)


class CostSummary(_Frozen):
    text_usd: Decimal
    image_usd: Decimal
    video_usd: Decimal
    media_estimate_usd: Decimal
    media_actual_usd: Decimal
    total_usd: Decimal


class CreativePackage(_Frozen):
    schema_version: Literal["1.0"] = PACKAGE_SCHEMA_VERSION
    package_id: uuid.UUID
    project_id: uuid.UUID
    creative_run_id: uuid.UUID
    #: 0 until release mints `max(version) + 1`.
    version: int = Field(ge=0)
    status: PackageStatus
    pins: Pins
    brief_hash: str
    campaigns: list[CampaignCreative] = Field(default_factory=list)
    landing_patches: list[LandingPatchRef] = Field(default_factory=list)
    decisions: list[GateDecision] = Field(default_factory=list)
    human_tasks: list[HumanTaskRef] = Field(default_factory=list)
    exceptions: list[ExceptionRef] = Field(default_factory=list)
    open_dependencies: list[Dependency] = Field(default_factory=list)
    lint_summary: LintSummary
    manifest: list[ManifestEntry] = Field(default_factory=list)
    cost: CostSummary
    package_hash: str = ""


# ---------------------------------------------------------------------------
# 4.7.1 and 4.7.2
# ---------------------------------------------------------------------------


class PackageAssembly(_Frozen):
    """4.7.1's output: what it assembled and where it wrote it."""

    package_id: uuid.UUID
    version: int = Field(ge=0)
    status: PackageStatus
    ruleset_version: str
    campaigns: int = Field(ge=0)
    assets: int = Field(ge=0)
    manifest: list[ManifestEntry] = Field(default_factory=list)
    launch_minimums: list[MinimumCheck] = Field(default_factory=list)
    package_hash: str


class CritiqueIssue(_Frozen):
    severity: IssueSeverity
    section: str = Field(min_length=1)
    finding: str = Field(min_length=1)
    fix: str = ""
    #: Which of §11's thirteen (`check_1` … `check_13`), or `reader` for the
    #: CRITIQUE model's own findings.
    check: str = Field(min_length=1)
    #: The assets the finding is about — what release's 409 names.
    asset_ids: list[uuid.UUID] = Field(default_factory=list)


class CreativeCritique(_Frozen):
    """4.7.2's output: the thirteen blocking checks, then the reader's notes."""

    package_id: uuid.UUID
    status: Literal["blocked", "ready_to_release"]
    issues: list[CritiqueIssue] = Field(default_factory=list)
    blocking: int = Field(ge=0)
    warnings: int = Field(ge=0)
    notes: int = Field(ge=0)
    failed_checks: list[str] = Field(default_factory=list)
    checks_run: Literal[13] = 13
