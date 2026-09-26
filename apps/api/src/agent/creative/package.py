"""4.7.1 `package_assembly` — the `CreativePackage`, by code alone (Stage 04 PRD §11 4.7, §12.3).

**Deterministic, no LLM.** `assemble` is a pure function of a `Snapshot`: the
run's rows and its nodes' stored outputs, read once. It sorts everything it
emits, so two processes handed the same snapshot produce byte-identical JSON
and therefore the same `package_hash` (§17 CC9). Release (`release.py`)
re-reads the snapshot and assembles again; that is how it knows the package
it is about to freeze is still what the rows say.

* **What ships** is every asset in `clearance.CARRIED` — the statuses an asset
  ships from — and nothing else: a `draft` failed lint, a `reserve` was not
  chosen, a `dropped` was refused.
* **Rows are the truth.** An ad is its carried rows, not 4.2.3's report: an
  H3 fallback or an operator's reserve swap may have changed it since. Its
  pairs are re-flagged over the text that ships (`ShippedPairs`).
* **Launch minimums** are 3.4.2's arithmetic — every spec with a `min_count`
  above zero is required — over the final pin's spec sheet. Stage 03 publishes
  3.4.2's own output only inside the guideline payload, which Stage 04 does
  not read (law 27); the spec sheet it was derived from is on the pin.
* **The manifest** lists every file the package ships — media masters and
  renditions, and each landing patch as JSON and HTML — with its sha256, bytes
  and media type, sorted by path. Release copies them under `package/` and
  checks each copy against it.
* **`package_hash`** is sha256 over the sorted manifest and the canonical
  payload, without `package_hash` itself and without `status` (the one field
  that still moves once a package is released).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import clearance, combinatorics, edits, metrics
from agent.creative.combinatorics import Asset, FlagRules
from agent.creative.conformance import media_spec
from agent.creative.constants import CreativeConstants
from agent.db.models import (
    Approval,
    AssetDecision,
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetVariant,
    CreativeBrief,
    CreativeException,
    GenerationJob,
    GenerationModality,
    GenerationStatus,
    HumanTask,
    LandingPageAudit,
    MediaArtifact,
    MediaArtifactRole,
    NodeRun,
    NodeRunStatus,
    Run,
)
from agent.db.models import CreativePackage as CreativePackageRow
from agent.export.plan_contract import PlannedCampaign
from agent.guardrails.verdicts import image_verdict
from agent.media.catalogue import catalogue_hash
from agent.media.types import CapabilityRecord
from agent.nodes.creative.n4_2_3_combination_coherence import flag_rules
from agent.nodes.creative.n4_2_4_variant_b import ad_ref
from agent.schemas.creative_brief import CreativeBrief as BriefPayload
from agent.schemas.creative_brief import OfferBinding
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_package import (
    AssetGroupCreative,
    AssetReviewRef,
    CampaignCreative,
    CostSummary,
    CreativePackage,
    Dependency,
    ExceptionRef,
    Extensions,
    GateDecision,
    HumanTaskRef,
    LandingPatchRef,
    LintCount,
    LintResultRef,
    LintSummary,
    ManifestEntry,
    MediaAsset,
    MediaRendition,
    MinimumCheck,
    PackageStatus,
    Pins,
    Provenance,
    RequiredCount,
    ResponsiveSearchAd,
    RuleCount,
    ShippedPair,
    ShippedPairs,
    TextAsset,
    VideoFacts,
)
from agent.schemas.creative_qa import LegalExceptionClearance
from agent.schemas.creative_video import CampaignVideo, VideoProduction, VideoRendition
from agent.schemas.guardrails import SURFACE_ASSET_TYPES, AssetSpec, LintResult, RuleSet
from agent.schemas.search_ads import (
    CombinationCoherenceOutput,
    HeadlineGroup,
    HeadlineSpreadOutput,
    PairReport,
)

#: Fields `package_hash` does not cover: itself, and the status that still
#: moves after release.
HASH_EXCLUDED: Final = frozenset({"package_hash", "status"})
#: Also left out when release asks "is this still the package 4.7.2 checked?":
#: the version it mints and the run's text spend, which 4.7.2's own reading
#: adds to after 4.7.1 wrote the draft.
CONTENT_EXCLUDED: Final = HASH_EXCLUDED | {"version", "cost"}

MEDIA_KINDS: Final = frozenset(
    {CreativeAssetKind.IMAGE, CreativeAssetKind.VIDEO, CreativeAssetKind.LOGO}
)
RSA_SURFACES: Final = {"rsa_headline": "headline", "rsa_description": "description",
                       "rsa_path": "path"}  # fmt: skip
ASSET_GROUP_SURFACES: Final = frozenset(
    {
        "pmax_headline",
        "long_headline",
        "pmax_description",
        "asset_group_description",
        "business_name",
    }  # fmt: skip
)
#: Google shows two display-URL paths.
MAX_PATHS: Final = 2
#: The gate each review round is (`schemas/creative_review.ROUND`, inverted).
ROUND_GATE: Final[Mapping[int, Literal["G8", "G8b"]]] = {1: "G8", 2: "G8b"}
GATE_ORDER: Final = ("G7", "G8", "G8b")
#: The outputs whose document order is the order their assets are listed in:
#: asset-group text (4.2.5) and the extras (4.3.1–4.3.3).
ORDERED_OUTPUTS: Final = ("4.2.5", "4.3.1", "4.3.2", "4.3.3")
SUFFIXES: Final[Mapping[str, str]] = {"image/jpeg": ".jpg", "image/png": ".png",
                                      "image/webp": ".webp", "video/mp4": ".mp4"}  # fmt: skip


class AssemblyError(ValueError):
    """The rows cannot be assembled into an honest package; nothing is guessed."""


# ---------------------------------------------------------------------------
# canonical JSON and the hash
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> bytes:
    """Sorted keys, no whitespace, UTF-8 — `creative_input.canonical_hash`'s encoding."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _digest(payload: Mapping[str, Any], excluded: frozenset[str]) -> str:
    body = {key: value for key, value in payload.items() if key not in excluded}
    manifest = sorted(body.pop("manifest", None) or [], key=lambda entry: entry["path"])
    return sha256(canonical_json({"manifest": manifest, "payload": body}))


def package_hash(payload: Mapping[str, Any]) -> str:
    """sha256 over the sorted manifest and the canonical payload (§12.3)."""
    return _digest(payload, HASH_EXCLUDED)


def content_digest(payload: Mapping[str, Any]) -> str:
    """`package_hash` without the version and the text spend — what release compares."""
    return _digest(payload, CONTENT_EXCLUDED)


def hashed(package: CreativePackage) -> CreativePackage:
    """`package` with its `package_hash` filled in."""
    payload = package.model_dump(mode="json")
    return package.model_copy(update={"package_hash": package_hash(payload)})


# ---------------------------------------------------------------------------
# the snapshot
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Snapshot:
    """Everything 4.7.1 assembles from and 4.7.2 checks, read once."""

    package_id: uuid.UUID
    version: int
    status: PackageStatus
    input: CreativeInput
    #: The final pin (`lint_adapter.current_pin`).
    ruleset: RuleSet
    constants: CreativeConstants
    brief: CreativeBrief | None
    approvals: Sequence[Approval]
    assets: Sequence[CreativeAsset]
    media: Sequence[MediaArtifact]
    jobs: Sequence[GenerationJob]
    decisions: Sequence[AssetDecision]
    exceptions: Sequence[CreativeException]
    h3: HumanTask | None
    landing: Sequence[LandingPageAudit]
    #: node id -> its latest succeeded output.
    outputs: Mapping[str, Any]
    text_cost_usd: Decimal

    @property
    def run_id(self) -> uuid.UUID:
        return self.input.creative_run_id

    @property
    def project_id(self) -> uuid.UUID:
        return self.input.project_id

    def included(self) -> list[CreativeAsset]:
        """Every asset that ships, by id."""
        return sorted(
            (asset for asset in self.assets if asset.status in clearance.CARRIED),
            key=lambda asset: str(asset.id),
        )


@dataclass(frozen=True, slots=True)
class PackageFile:
    """One file the package ships: copied from storage, or written from rows."""

    path: str
    media_type: str
    sha256: str
    size: int
    #: The stored file this is a copy of.
    source_key: str | None = None
    #: The bytes, when the file is generated rather than copied.
    content: bytes | None = None

    def entry(self) -> ManifestEntry:
        return ManifestEntry(
            path=self.path, sha256=self.sha256, bytes=self.size, media_type=self.media_type
        )


@dataclass
class _Files:
    files: dict[str, PackageFile] = field(default_factory=dict)

    def add(self, file: PackageFile) -> str:
        self.files[file.path] = file
        return file.path

    def sorted(self) -> list[PackageFile]:
        return [self.files[path] for path in sorted(self.files)]


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def assemble(snap: Snapshot) -> tuple[CreativePackage, list[PackageFile]]:
    """The package and the files it ships. Pure and deterministic."""
    files = _Files()
    included = snap.included()
    brief = _brief(snap)
    media = _media_assets(snap, included, files)
    hint = _order_hint(snap.outputs)
    campaigns = [
        _campaign(snap, campaign, included, media, brief, hint)
        for campaign in _campaigns(snap.input)
    ]
    landing = _landing(snap, files)
    package = CreativePackage(
        package_id=snap.package_id,
        project_id=snap.project_id,
        creative_run_id=snap.run_id,
        version=snap.version,
        status=snap.status,
        pins=pins(snap),
        brief_hash=snap.brief.brief_hash if snap.brief is not None else "",
        campaigns=campaigns,
        landing_patches=landing,
        decisions=_decisions(snap),
        human_tasks=_human_tasks(snap),
        exceptions=_exceptions(snap),
        open_dependencies=_dependencies(snap, media, brief),
        lint_summary=_lint_summary(snap, included),
        manifest=[file.entry() for file in files.sorted()],
        cost=_cost(snap),
    )
    return hashed(package), files.sorted()


def pins(snap: Snapshot) -> Pins:
    records = sorted(
        (CapabilityRecord.model_validate(model.capability) for model in snap.input.media_models),
        key=lambda record: (record.modality, record.model_id),
    )
    return Pins(
        plan_id=snap.input.plan_ref.plan_id,
        plan_version=snap.input.plan_ref.version,
        ruleset_version=snap.ruleset.ruleset_version,
        context_hash=snap.input.context_ref.hash,
        constants_version=snap.input.constants_version,
        catalogue_hash=catalogue_hash(records),
    )


def _campaigns(inp: CreativeInput) -> list[PlannedCampaign]:
    """The campaigns in the run's scope (every campaign when the scope names none)."""
    scope = set(inp.scope.campaign_refs)
    campaigns = [
        campaign
        for campaign in inp.account_structure.campaigns
        if not scope or campaign_ref_of(campaign) in scope
    ]
    return sorted(campaigns, key=campaign_ref_of)


def campaign_ref_of(campaign: PlannedCampaign) -> str:
    return campaign.campaign_ref or campaign.name


def campaign_type_of(campaign: PlannedCampaign) -> str:
    return campaign.type.strip().lower()


def _brief(snap: Snapshot) -> BriefPayload | None:
    if snap.brief is None or not snap.brief.payload:
        return None
    return BriefPayload.model_validate(snap.brief.payload)


def _campaign(
    snap: Snapshot,
    campaign: PlannedCampaign,
    included: Sequence[CreativeAsset],
    media: Sequence[MediaAsset],
    brief: BriefPayload | None,
    hint: Mapping[str, int],
) -> CampaignCreative:
    ref = campaign_ref_of(campaign)
    texts = [a for a in included if a.campaign_ref == ref and a.kind not in MEDIA_KINDS]
    ours = [m for m in media if m.campaign_ref == ref]
    ads = _ads(snap, texts, brief)
    groups = _asset_groups(texts, campaign, ours, hint)
    specs = snap.ruleset.asset_specs.for_campaign(campaign_type_of(campaign))
    return CampaignCreative(
        campaign_ref=ref,
        campaign_type=campaign_type_of(campaign),
        text_assets=[_text_asset(asset) for asset in texts],
        ads=ads,
        asset_groups=groups,
        extensions=_extensions(texts, hint),
        media=[m for m in ours if m.modality != "logo"],
        logos=[m for m in ours if m.modality == "logo"],
        launch_minimums=launch_minimums(
            campaign_type_of(campaign), specs, texts, ours, snap.constants
        ),
    )


def lint_ref(asset: CreativeAsset) -> LintResultRef:
    """The asset's lint, as its row holds it; `unlinted` when it holds none."""
    if not asset.lint:
        return LintResultRef(verdict="unlinted", ruleset_version=asset.ruleset_version)
    result = LintResult.model_validate(asset.lint)
    verdict: str = result.verdict
    if asset.kind in (CreativeAssetKind.IMAGE, CreativeAssetKind.LOGO):
        verdict, _ = image_verdict(result)
    return LintResultRef.model_validate(
        {
            "verdict": verdict,
            "ruleset_version": result.ruleset_version,
            "rule_ids": sorted({finding.rule_id for finding in result.findings}),
        }
    )


def _text_asset(asset: CreativeAsset) -> TextAsset:
    return TextAsset.model_validate(
        {
            "asset_id": asset.id,
            "kind": asset.kind.value,
            "surface": asset.surface,
            "campaign_ref": asset.campaign_ref,
            "ad_group_ref": asset.ad_group_ref,
            "text": asset.text,
            "fields": dict(asset.fields or {}),
            "category": asset.category,
            "claim_ids": sorted(asset.claim_ids or [], key=str),
            "offer_binding": asset.offer_binding,
            "pin_position": asset.pin_position,
            "variant": asset.variant.value if asset.variant is not None else None,
            "lint": lint_ref(asset),
            "ruleset_version": asset.ruleset_version,
            "lineage": dict(asset.lineage or {}),
            "generated_by_ai": asset.generated_by_ai,
        }
    )


def _order_hint(outputs: Mapping[str, Any]) -> dict[str, int]:
    """Each asset id's first position in the outputs that list extras and
    asset-group text, in document order — the order those nodes emitted them."""
    hint: dict[str, int] = {}

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            asset_id = node.get("asset_id")
            if isinstance(asset_id, str) and asset_id not in hint:
                hint[asset_id] = len(hint)
            for key in sorted(node):
                walk(node[key])
        elif isinstance(node, list):
            for item in node:
                walk(item)

    for node_id in ORDERED_OUTPUTS:
        walk(outputs.get(node_id))
    return hint


def _ordered(rows: Iterable[CreativeAsset], hint: Mapping[str, int]) -> list[CreativeAsset]:
    return sorted(rows, key=lambda row: (hint.get(str(row.id), len(hint)), str(row.id)))


# --- responsive search ads --------------------------------------------------


def _variant(asset: CreativeAsset) -> Literal["A", "B"]:
    return "B" if asset.variant is CreativeAssetVariant.B else "A"


def _ads(
    snap: Snapshot, texts: Sequence[CreativeAsset], brief: BriefPayload | None
) -> list[ResponsiveSearchAd]:
    pools: dict[tuple[str, Literal["A", "B"]], dict[str, list[CreativeAsset]]] = {}
    for asset in texts:
        role = RSA_SURFACES.get(asset.surface)
        if role is None or asset.ad_group_ref is None:
            continue
        pool = pools.setdefault((asset.ad_group_ref, _variant(asset)), {})
        pool.setdefault(role, []).append(asset)
    judged = _judged(snap.outputs)
    variant_b = _variant_b(snap.outputs)
    groups_a = _headline_groups(snap.outputs)
    paths = _path_order(snap.outputs)
    slots = {group.ad_group_ref: group for group in brief.ad_groups} if brief else {}
    ads: dict[tuple[str, Literal["A", "B"]], ResponsiveSearchAd] = {}
    for (ad_group_ref, variant), pool in sorted(pools.items()):
        heads = pool.get("headline", [])
        if not heads:
            continue
        campaign_ref = heads[0].campaign_ref
        report = judged.get((campaign_ref, ad_group_ref, variant))
        b_group = variant_b.get((campaign_ref, ad_group_ref))
        headlines = _by_report(heads, report.headlines if report else [])
        descriptions = _by_report(
            pool.get("description", []), report.descriptions if report else []
        )
        pool_group = (
            b_group.headlines
            if variant == "B" and b_group
            else groups_a.get((campaign_ref, ad_group_ref))
        )
        slot = slots.get(ad_group_ref)
        if slot is None:
            # 4.1.1 briefs every ad-group slot exactly once, and G7 approved
            # that brief: an ad with no slot has no angle or landing page
            # anybody authorised, and inventing one is not assembly.
            raise AssemblyError(
                f"{campaign_ref} / {ad_group_ref} ships a variant {variant} ad, but the "
                "approved brief has no slot for that ad group"
            )
        ads[(ad_group_ref, variant)] = ResponsiveSearchAd(
            ad_ref=ad_ref(campaign_ref, ad_group_ref, variant),
            campaign_ref=campaign_ref,
            ad_group_ref=ad_group_ref,
            variant=variant,
            angle=slot.angle_b.text if variant == "B" else slot.primary_message.text,
            hypothesis=b_group.hypothesis if variant == "B" and b_group is not None else None,
            headlines=[asset.id for asset in headlines],
            descriptions=[asset.id for asset in descriptions],
            paths=_paths(
                pool.get("path", []), paths.get((campaign_ref, ad_group_ref, variant), ())
            ),
            final_url=str(slot.landing_url),
            pair_report=shipped_pairs(headlines, descriptions, report, snap, pool_group),
        )
    for (ad_group_ref, variant), ad in list(ads.items()):
        if variant != "B":
            continue
        a = ads.get((ad_group_ref, "A"))
        if a is None:
            continue
        by_id = {asset.id: asset for asset in texts}
        distance = metrics.distinctness(ad_texts(a, by_id), ad_texts(ad, by_id))
        ads[(ad_group_ref, variant)] = ad.model_copy(update={"distinctness_vs_a": distance})
    return [ads[key] for key in sorted(ads)]


def _judged(outputs: Mapping[str, Any]) -> dict[tuple[str, str, str], PairReport]:
    """Every ad 4.2.3 (A) and 4.2.4 (B) judged, by (campaign, ad group, variant)."""
    reports: dict[tuple[str, str, str], PairReport] = {}
    raw_a = outputs.get("4.2.3")
    if raw_a:
        for report in CombinationCoherenceOutput.model_validate(raw_a).ads:
            reports[(report.campaign_ref, report.ad_group_ref, report.variant)] = report
    for group in _variant_b(outputs).values():
        report = group.report
        reports[(report.campaign_ref, report.ad_group_ref, report.variant)] = report
    return reports


@dataclass(frozen=True, slots=True)
class _VariantB:
    """What a package needs of one 4.2.4 group."""

    hypothesis: str
    headlines: HeadlineGroup
    paths: tuple[str, ...]
    report: PairReport


def _variant_b(outputs: Mapping[str, Any]) -> dict[tuple[str, str], _VariantB]:
    """4.2.4's groups, read field by field rather than as a `VariantBGroup`.

    A `VariantBGroup` re-validates its descriptions' claims against a pin
    (`search_ads.licensed_at_pin`), and the pin they were licensed at is the
    start pin at the run's start. Whether they are licensed *now*, at the
    final pin, is check 4's question, asked of the rows — so the package reads
    the hypothesis, the headline pool, the paths and the judged report, which
    carry no claim licence.
    """
    found: dict[tuple[str, str], _VariantB] = {}
    for group in (outputs.get("4.2.4") or {}).get("ad_groups", []):
        found[(group["campaign_ref"], group["ad_group_ref"])] = _VariantB(
            hypothesis=group["hypothesis"],
            headlines=HeadlineGroup.model_validate(group["headlines"]),
            paths=tuple(p for p in (group.get("descriptions") or {}).get("paths") or () if p),
            report=PairReport.model_validate(group["ad_b"]["pair_report"]),
        )
    return found


def _headline_groups(outputs: Mapping[str, Any]) -> dict[tuple[str, str], HeadlineGroup]:
    raw = outputs.get("4.2.1")
    if not raw:
        return {}
    return {
        (group.campaign_ref, group.ad_group_ref): group
        for group in HeadlineSpreadOutput.model_validate(raw).ad_groups
        if group.variant == "A"
    }


def _by_report(rows: Sequence[CreativeAsset], order: Sequence[uuid.UUID]) -> list[CreativeAsset]:
    """The ad's rows in the order its judged report lists them; any others after, by id."""
    position = {asset_id: index for index, asset_id in enumerate(order)}
    return sorted(rows, key=lambda row: (position.get(row.id, len(position)), str(row.id)))


def _path_order(outputs: Mapping[str, Any]) -> dict[tuple[str, str, str], tuple[str, ...]]:
    """The display paths 4.2.2 (A) and 4.2.4 (B) wrote, in the order they wrote them."""
    found: dict[tuple[str, str, str], tuple[str, ...]] = {}
    # Read as stored: `ClaimBoundDescriptionsOutput` validates its claims
    # against a pin, and the paths are all this needs from it.
    for item in (outputs.get("4.2.2") or {}).get("ad_groups", []):
        key = (item["campaign_ref"], item["ad_group_ref"], item.get("variant", "A"))
        found[key] = tuple(path for path in item.get("paths") or () if path)
    for (campaign_ref, ad_group_ref), group in _variant_b(outputs).items():
        found[(campaign_ref, ad_group_ref, "B")] = group.paths
    return found


def _paths(rows: Sequence[CreativeAsset], order: Sequence[str]) -> tuple[str | None, str | None]:
    """The ad's carried path rows, in the order their node wrote the paths."""
    position = {text: index for index, text in enumerate(order)}
    ranked = sorted(
        (row for row in rows if row.text),
        key=lambda row: (position.get(row.text or "", len(position)), str(row.id)),
    )
    texts: list[str | None] = [row.text for row in ranked[:MAX_PATHS]]
    texts.extend([None] * (MAX_PATHS - len(texts)))
    return texts[0], texts[1]


def pair_asset(row: CreativeAsset) -> Asset:
    """A shipped headline or description, as `pair_flags_v1` reads it (4.2.3's `_headline`)."""
    fields = row.fields or {}
    if row.surface == "rsa_headline":
        return Asset(
            ref=str(row.id),
            role="headline",
            text=str(fields.get("default_text") or edits.linted_text(row.surface, row.text or "")),
            category=row.category,
            keyword_ref=fields.get("keyword_ref"),
            claim_ids=tuple(str(claim) for claim in row.claim_ids or ()),
        )
    span = fields.get("claim_span")
    return Asset(
        ref=str(row.id),
        role="description",
        text=row.text or "",
        claim_ids=tuple(str(claim) for claim in row.claim_ids or ()),
        claim_span=(int(span[0]), int(span[1])) if span else None,
    )


def shipped_pairs(
    headlines: Sequence[CreativeAsset],
    descriptions: Sequence[CreativeAsset],
    report: PairReport | None,
    snap: Snapshot,
    pool: HeadlineGroup | None,
) -> ShippedPairs:
    """Every pair the shipped ad can serve, flagged over the shipped text."""
    heads = [pair_asset(row) for row in headlines]
    descs = [pair_asset(row) for row in descriptions]
    copy = snap.constants.copy_
    rules = (
        flag_rules(copy, pool)
        if pool is not None
        else FlagRules(
            near_duplicate=copy.near_duplicate_trigram.value,
            cta_verbs=combinatorics.cta_verbs(heads),
        )
    )
    labels = {frozenset((pair.a, pair.b)): pair.label for pair in report.pairs} if report else {}
    pairs: list[ShippedPair] = []
    for a, b, kind in combinatorics.enumerate_pairs(heads, descs):
        left, right = uuid.UUID(a.ref), uuid.UUID(b.ref)
        pairs.append(
            ShippedPair.model_validate(
                {
                    "a": left,
                    "b": right,
                    "kind": kind,
                    "flags": list(combinatorics.flags(a, b, kind, rules)),
                    "label": labels.get(frozenset((left, right))),
                }
            )
        )
    judged = report is not None and (
        [row.id for row in headlines] == list(report.headlines)
        and [row.id for row in descriptions] == list(report.descriptions)
    )
    return ShippedPairs(
        judged=judged,
        pairs=pairs,
        unresolved=[(pair.a, pair.b) for pair in pairs if pair.flags],
    )


def ad_texts(ad: ResponsiveSearchAd, by_id: Mapping[uuid.UUID, CreativeAsset]) -> list[str]:
    """What an ad carries, as text — headlines on their default, then descriptions
    (4.2.4's `ad_texts`, over rows)."""
    return [pair_asset(by_id[asset_id]).text for asset_id in (*ad.headlines, *ad.descriptions)]


# --- asset groups and extensions ---------------------------------------------


def _asset_groups(
    texts: Sequence[CreativeAsset],
    campaign: PlannedCampaign,
    media: Sequence[MediaAsset],
    hint: Mapping[str, int],
) -> list[AssetGroupCreative]:
    groups: dict[str, list[CreativeAsset]] = {}
    for asset in texts:
        if asset.surface in ASSET_GROUP_SURFACES and asset.ad_group_ref is not None:
            groups.setdefault(asset.ad_group_ref, []).append(asset)
    out: list[AssetGroupCreative] = []
    for ad_group_ref in sorted(groups):
        rows = _ordered(groups[ad_group_ref], hint)
        names = [row.id for row in rows if row.kind is CreativeAssetKind.BUSINESS_NAME]
        out.append(
            AssetGroupCreative(
                campaign_ref=campaign_ref_of(campaign),
                ad_group_ref=ad_group_ref,
                campaign_type=campaign_type_of(campaign),
                headlines=[r.id for r in rows if r.kind is CreativeAssetKind.HEADLINE],
                long_headlines=[r.id for r in rows if r.kind is CreativeAssetKind.LONG_HEADLINE],
                descriptions=[r.id for r in rows if r.kind is CreativeAssetKind.DESCRIPTION],
                business_name=names[0] if names else None,
                media=sorted((m.asset_id for m in media), key=str),
            )
        )
    return out


def _extensions(texts: Sequence[CreativeAsset], hint: Mapping[str, int]) -> Extensions:
    def of(kind: CreativeAssetKind) -> list[uuid.UUID]:
        return [row.id for row in _ordered((r for r in texts if r.kind is kind), hint)]

    forms = of(CreativeAssetKind.LEAD_FORM)
    return Extensions(
        sitelinks=of(CreativeAssetKind.SITELINK),
        callouts=of(CreativeAssetKind.CALLOUT),
        snippets=of(CreativeAssetKind.STRUCTURED_SNIPPET),
        promotions=of(CreativeAssetKind.PROMOTION),
        prices=of(CreativeAssetKind.PRICE),
        lead_form=forms[0] if forms else None,
    )


# --- media --------------------------------------------------------------------


def _suffix(media_type: str) -> str:
    return SUFFIXES.get(media_type, "")


def media_path(asset_id: uuid.UUID, artifact: MediaArtifact) -> str:
    prefix = "master_" if artifact.role is MediaArtifactRole.MASTER else ""
    return f"media/{asset_id}/{prefix}{artifact.id}{_suffix(artifact.media_type)}"


def _media_assets(
    snap: Snapshot, included: Sequence[CreativeAsset], files: _Files
) -> list[MediaAsset]:
    by_asset: dict[uuid.UUID, list[MediaArtifact]] = {}
    for artifact in snap.media:
        by_asset.setdefault(artifact.asset_id, []).append(artifact)
    facts = _rendition_facts(snap.outputs)
    videos = _videos(snap.outputs)
    concepts = _concepts(snap.outputs, videos)
    depiction = _depictions(snap.outputs)
    references = _references(snap.outputs)
    out: list[MediaAsset] = []
    for asset in included:
        if asset.kind not in MEDIA_KINDS:
            continue
        artifacts = sorted(by_asset.get(asset.id, []), key=lambda a: str(a.id))
        lint = lint_ref(asset)
        renditions: list[MediaRendition] = []
        for artifact in artifacts:
            if artifact.role not in (MediaArtifactRole.RENDITION, MediaArtifactRole.MASTER):
                continue
            path = files.add(
                PackageFile(
                    path=media_path(asset.id, artifact),
                    media_type=artifact.media_type,
                    sha256=artifact.sha256,
                    size=artifact.bytes,
                    source_key=artifact.storage_path,
                )
            )
            if artifact.role is MediaArtifactRole.RENDITION:
                renditions.append(_rendition(asset, artifact, path, lint, facts))
        renditions.sort(key=lambda r: (r.aspect_ratio, r.width, r.height, str(r.media_id)))
        concept_id = concepts.get(asset.id) or (asset.fields or {}).get("concept_id")
        modality: Literal["image", "video", "logo"] = (
            "logo"
            if asset.kind is CreativeAssetKind.LOGO
            else ("video" if asset.kind is CreativeAssetKind.VIDEO else "image")
        )
        out.append(
            MediaAsset(
                asset_id=asset.id,
                modality=modality,
                campaign_ref=asset.campaign_ref,
                concept_id=concept_id,
                generated_by_ai=asset.generated_by_ai,
                renditions=renditions,
                provenance=provenance(asset, snap.jobs, references.get(asset.id, [])),
                product_depiction=(
                    "none" if modality == "logo" else depiction.get(concept_id or "", "none")
                ),
                video=_aggregate(renditions) if modality == "video" else None,
                review=review_of(asset, snap.decisions),
            )
        )
    return out


@dataclass(frozen=True, slots=True)
class _Facts:
    surface: str | None
    logo_composited: bool
    video: VideoRendition | None


def _rendition_facts(outputs: Mapping[str, Any]) -> dict[uuid.UUID, _Facts]:
    """What 4.4.3 and 4.4.4/4.4.6 said about each file they made, by media id."""
    found: dict[uuid.UUID, _Facts] = {}
    raw = outputs.get("4.4.3") or {}
    for item in raw.get("renditions", []):
        found[uuid.UUID(item["media_id"])] = _Facts(
            surface=item.get("surface"), logo_composited=bool(item.get("logo_composited")),
            video=None,
        )  # fmt: skip
    for item in raw.get("logos", []):
        found[uuid.UUID(item["media_id"])] = _Facts(
            surface=item.get("surface"), logo_composited=False, video=None
        )
    for regenerated in (outputs.get("4.4.6") or {}).get("items", []):
        for item in regenerated.get("renditions") or []:
            found[uuid.UUID(item["media_id"])] = _Facts(
                surface=item.get("surface"), logo_composited=bool(item.get("logo_composited")),
                video=None,
            )  # fmt: skip
    for video in _videos(outputs):
        for rendition in video.renditions:
            found[rendition.media_id] = _Facts(surface=None, logo_composited=False, video=rendition)
    return found


def _videos(outputs: Mapping[str, Any]) -> list[CampaignVideo]:
    """4.4.4's videos and every video 4.4.6 re-shot."""
    videos: list[CampaignVideo] = []
    raw = outputs.get("4.4.4")
    if raw:
        videos.extend(VideoProduction.model_validate(raw).videos)
    for item in (outputs.get("4.4.6") or {}).get("items", []):
        if item.get("video"):
            videos.append(CampaignVideo.model_validate(item["video"]))
    return videos


def _concepts(outputs: Mapping[str, Any], videos: Sequence[CampaignVideo]) -> dict[uuid.UUID, str]:
    found: dict[uuid.UUID, str] = {}
    for concept in (outputs.get("4.4.2") or {}).get("concepts", []):
        found[uuid.UUID(concept["asset_id"])] = concept["concept_id"]
    for item in (outputs.get("4.4.6") or {}).get("items", []):
        found[uuid.UUID(item["asset_id"])] = item["concept_id"]
    for video in videos:
        found[video.asset_id] = video.concept_id
    return found


def _depictions(outputs: Mapping[str, Any]) -> dict[str, str]:
    raw = outputs.get("4.4.1") or {}
    found: dict[str, str] = {}
    for campaign in raw.get("campaigns", []):
        for concept in campaign.get("concepts", []):
            found[concept["id"]] = concept.get("product_depiction") or "none"
    return found


def _references(outputs: Mapping[str, Any]) -> dict[uuid.UUID, list[str]]:
    found: dict[uuid.UUID, list[str]] = {}
    for concept in (outputs.get("4.4.2") or {}).get("concepts", []):
        found[uuid.UUID(concept["asset_id"])] = sorted(concept.get("reference_sha256s") or [])
    return found


def _scale(artifact: MediaArtifact) -> tuple[float, float]:
    transform = artifact.transform or {}
    if "sx" in transform and "sy" in transform:
        return float(transform["sx"]), float(transform["sy"])
    return 1.0, 1.0


def _rendition(
    asset: CreativeAsset,
    artifact: MediaArtifact,
    path: str,
    lint: LintResultRef,
    facts: Mapping[uuid.UUID, _Facts],
) -> MediaRendition:
    known = facts.get(artifact.id)
    video = known.video if known is not None else None
    return MediaRendition(
        media_id=artifact.id,
        path=path,
        surface=(known.surface if known is not None and known.surface else asset.surface),
        aspect_ratio=artifact.aspect_ratio,
        width=artifact.width,
        height=artifact.height,
        bytes=artifact.bytes,
        media_type=artifact.media_type,
        sha256=artifact.sha256,
        derivation=artifact.derivation.value,
        scale=_scale(artifact),
        logo_composited=known.logo_composited if known is not None else False,
        lint=lint,
        disclosure=dict(artifact.disclosure) if artifact.disclosure else None,
        duration_ms=artifact.duration_ms,
        video=(
            VideoFacts(
                duration_ms=video.duration_ms,
                brand_first_at_ms=video.brand_first_at_ms,
                captions_burned=video.captions_burned,
                caption_ocr_min_similarity=video.caption_ocr_min_similarity,
                has_audio=video.has_audio,
            )
            if video is not None
            else None
        ),
    )


def _aggregate(renditions: Sequence[MediaRendition]) -> VideoFacts | None:
    facts = [r.video for r in renditions if r.video is not None]
    if not facts:
        return None
    similarities = [f.caption_ocr_min_similarity for f in facts]
    return VideoFacts(
        duration_ms=max(f.duration_ms for f in facts),
        brand_first_at_ms=max(f.brand_first_at_ms for f in facts),
        captions_burned=all(f.captions_burned for f in facts),
        caption_ocr_min_similarity=(
            None
            if any(s is None for s in similarities)
            else min(s for s in similarities if s is not None)
        ),
        has_audio=any(f.has_audio for f in facts),
    )


def provenance(
    asset: CreativeAsset, jobs: Sequence[GenerationJob], references: Sequence[str]
) -> Provenance:
    """Model, provider, seed, prompt, jobs and cost of what made the asset."""
    ours = sorted((job for job in jobs if job.asset_id == asset.id), key=lambda job: str(job.id))
    done = [job for job in ours if job.status is GenerationStatus.COMPLETED]
    if not done:
        return Provenance(reference_sha256s=list(references))
    models = sorted({job.model_id for job in done})
    providers = sorted({job.provider_tag for job in done if job.provider_tag})
    seeds = [job.request.get("seed") for job in done if isinstance(job.request.get("seed"), int)]
    prompts = [
        job.request.get("prompt") for job in done if isinstance(job.request.get("prompt"), str)
    ]
    costs = [job.cost_usd for job in done]
    return Provenance(
        model_id=", ".join(models),
        provider=", ".join(providers) or None,
        seed=seeds[0] if seeds else None,
        prompt_hash=sha256(str(prompts[0]).encode("utf-8")) if prompts else None,
        reference_sha256s=list(references),
        job_ids=[job.id for job in done],
        openrouter_generation_ids=sorted(
            job.openrouter_job_id for job in done if job.openrouter_job_id
        ),
        cost_usd=(
            None
            if any(cost is None for cost in costs)
            else sum((cost for cost in costs if cost is not None), Decimal(0))
        ),
    )


def review_of(asset: CreativeAsset, decisions: Sequence[AssetDecision]) -> AssetReviewRef:
    ours = sorted(
        (d for d in decisions if d.asset_id == asset.id), key=lambda d: (d.round, str(d.id))
    )
    if not ours:
        return AssetReviewRef()
    last = ours[-1]
    return AssetReviewRef(
        gate=ROUND_GATE.get(last.round),
        round=last.round,
        decision=last.decision.value,
        decider=last.decided_by,
        decided_at=last.decided_at,
    )


# --- launch minimums -----------------------------------------------------------


def _container(asset: CreativeAsset) -> tuple[str, str]:
    """The unit an asset type is counted in: an ad, an asset group, or the campaign."""
    if asset.surface in RSA_SURFACES:
        return ("ad", f"{asset.ad_group_ref}/{_variant(asset)}")
    if asset.surface in ASSET_GROUP_SURFACES:
        return ("asset_group", str(asset.ad_group_ref))
    return ("campaign", "")


def launch_minimums(
    campaign_type: str,
    specs: Mapping[str, AssetSpec],
    texts: Sequence[CreativeAsset],
    media: Sequence[MediaAsset],
    constants: CreativeConstants,
) -> MinimumCheck:
    """3.4.2's minimum set for the campaign type — each spec with `min_count > 0`
    — against what the campaign ships."""
    counts: dict[str, dict[tuple[str, str], int]] = {}
    for asset in texts:
        asset_type = SURFACE_ASSET_TYPES.get(asset.surface)
        if asset_type is None:
            continue
        per = counts.setdefault(asset_type, {})
        if asset.kind is CreativeAssetKind.STRUCTURED_SNIPPET:
            # A snippet's `min_count` counts its values, not snippets (S4-P8,
            # 4.3.1): the campaign has what its fullest snippet has.
            values = len((asset.fields or {}).get("values") or [])
            per[_container(asset)] = max(per.get(_container(asset), 0), values)
            continue
        per[_container(asset)] = per.get(_container(asset), 0) + 1
    tolerance = constants.media.ratio_tolerance.value
    for item in media:
        for rendition in item.renditions:
            matched = _media_type(specs, rendition.aspect_ratio, item.modality, tolerance)
            if matched is not None:
                per = counts.setdefault(matched, {})
                per[("campaign", "")] = per.get(("campaign", ""), 0) + 1
    required: list[RequiredCount] = []
    for asset_type in sorted(specs):
        spec = specs[asset_type]
        if not spec.min_count or spec.min_count <= 0:
            continue
        per = counts.get(asset_type, {})
        present = min(per.values()) if per else 0
        required.append(
            RequiredCount(
                asset_type=asset_type,
                required=spec.min_count,
                present=present,
                met=present >= spec.min_count,
            )
        )
    return MinimumCheck(
        campaign_type=campaign_type,
        required=required,
        met=all(line.met for line in required),
        no_stated_minimum=not required,
    )


def _media_type(
    specs: Mapping[str, AssetSpec], ratio: str, modality: str, tolerance: float
) -> str | None:
    found = media_spec(specs, ratio, kind=modality, tolerance=tolerance)
    return found[0] if found is not None else None


# --- landing, decisions, tasks, exceptions, dependencies -------------------------


def _landing(snap: Snapshot, files: _Files) -> list[LandingPatchRef]:
    refs: list[LandingPatchRef] = []
    for audit in sorted(snap.landing, key=lambda row: str(row.id)):
        if not audit.patch:
            continue
        body = canonical_json(audit.patch)
        html = str(audit.patch.get("html_snippet") or "").encode("utf-8")
        json_path = files.add(
            PackageFile(
                path=f"landing/{audit.id}/patch.json",
                media_type="application/json",
                sha256=sha256(body),
                size=len(body),
                content=body,
            )
        )
        html_path = files.add(
            PackageFile(
                path=f"landing/{audit.id}/patch.html",
                media_type="text/html",
                sha256=sha256(html),
                size=len(html),
                content=html,
            )
        )
        refs.append(
            LandingPatchRef(
                audit_id=audit.id,
                url=audit.url,
                ad_group_refs=sorted(audit.ad_group_refs or []),
                verdict=audit.verdict.value,
                json_path=json_path,
                html_path=html_path,
            )
        )
    return refs


def _decisions(snap: Snapshot) -> list[GateDecision]:
    rows = [row for row in snap.approvals if row.gate_key in GATE_ORDER]
    rows.sort(key=lambda row: (GATE_ORDER.index(row.gate_key), str(row.id)))
    return [
        GateDecision.model_validate(
            {
                "gate_key": row.gate_key,
                "approval_id": row.id,
                "node_id": row.node_id,
                "status": row.status.value,
                "decided_by": row.decided_by,
                "decided_at": row.decided_at,
                "note": row.decision_note or "",
            }
        )
        for row in rows
    ]


def clearance_of(snap: Snapshot) -> LegalExceptionClearance | None:
    raw = snap.outputs.get("4.6.3")
    return LegalExceptionClearance.model_validate(raw) if raw else None


def _human_tasks(snap: Snapshot) -> list[HumanTaskRef]:
    h3 = clearance_of(snap)
    if h3 is None and snap.h3 is None:
        return []
    task = snap.h3
    return [
        HumanTaskRef(
            task_id=task.id if task is not None else None,
            status=h3.status if h3 is not None else "required",
            task_status=task.status.value if task is not None else None,
            assignee_id=(
                task.assignee_id if task is not None else (h3.assignee_id if h3 else None)
            ),
        )
    ]


def _exceptions(snap: Snapshot) -> list[ExceptionRef]:
    return [
        ExceptionRef(
            exception_id=row.id,
            kind=row.kind.value,
            status=row.status.value,
            subject=row.subject_text,
            asset_ids=sorted(row.asset_ids or [], key=str),
            decided_by=row.decided_by,
            decided_at=row.decided_at,
        )
        for row in sorted(snap.exceptions, key=lambda row: str(row.id))
    ]


def _dependencies(
    snap: Snapshot, media: Sequence[MediaAsset], brief: BriefPayload | None
) -> list[Dependency]:
    found: list[Dependency] = [
        Dependency(
            kind="inherited",
            task=item.task,
            owner=item.owner,
            blocking_for="launch" if item.blocking else "none",
            source=item.source,
        )
        for item in snap.input.inherited_dependencies
    ]
    campaign_of = (
        {group.ad_group_ref: group.campaign_ref for group in brief.ad_groups} if brief else {}
    )
    for audit in snap.landing:
        verdict = audit.verdict.value
        if verdict == "ok":
            continue
        found.append(
            Dependency(
                kind="landing_patch",
                task=(
                    f"Fix {audit.url} ({verdict.replace('_', ' ')})"
                    if verdict != "needs_change"
                    else f"Apply the landing-page patch to {audit.url}"
                ),
                blocking_for="launch"
                if verdict in ("blocking_for_launch", "unreachable")
                else "none",
                campaign_refs=sorted(
                    {campaign_of[ref] for ref in audit.ad_group_refs or [] if ref in campaign_of}
                ),
                source=f"landing_page_audit:{audit.id}",
            )
        )
    for item in media:
        if item.modality != "video":
            continue
        found.append(
            Dependency(
                kind="youtube_upload",
                task=f"Upload video {item.asset_id} to YouTube and record its video id",
                blocking_for="launch",
                campaign_refs=[item.campaign_ref],
                source=f"creative_asset:{item.asset_id}",
            )
        )
    return sorted(found, key=lambda d: (d.kind, d.source, d.task))


def _lint_summary(snap: Snapshot, included: Sequence[CreativeAsset]) -> LintSummary:
    verdicts: Counter[str] = Counter()
    rules: Counter[str] = Counter()
    for asset in included:
        ref = lint_ref(asset)
        verdicts[ref.verdict] += 1
        rules.update(ref.rule_ids)
    return LintSummary(
        ruleset_version=snap.ruleset.ruleset_version,
        by_verdict=[
            LintCount.model_validate({"verdict": verdict, "count": verdicts[verdict]})
            for verdict in sorted(verdicts)
        ],
        by_rule=[RuleCount(rule_id=rule, count=rules[rule]) for rule in sorted(rules)],
    )


def _cost(snap: Snapshot) -> CostSummary:
    def total(modality: GenerationModality) -> Decimal:
        return sum(
            (job.cost_usd for job in snap.jobs if job.modality is modality and job.cost_usd),
            Decimal(0),
        )

    image, video = total(GenerationModality.IMAGE), total(GenerationModality.VIDEO)
    estimate = sum((job.estimate_usd for job in snap.jobs), Decimal(0))
    text = Decimal(snap.text_cost_usd)
    return CostSummary(
        text_usd=text,
        image_usd=image,
        video_usd=video,
        media_estimate_usd=estimate,
        media_actual_usd=image + video,
        total_usd=text + image + video,
    )


def binding_of(asset: CreativeAsset) -> OfferBinding | None:
    return OfferBinding.model_validate(asset.offer_binding) if asset.offer_binding else None


# ---------------------------------------------------------------------------
# reading the rows — the one function here that does I/O
# ---------------------------------------------------------------------------

#: The node ids whose outputs a package is assembled from and checked against.
OUTPUT_NODES: Final = (
    "4.2.1", "4.2.2", "4.2.3", "4.2.4", "4.2.5", "4.3.1", "4.3.2", "4.3.3",
    "4.4.1", "4.4.2", "4.4.3", "4.4.4", "4.4.6", "4.6.1", "4.6.3", "4.6.4",
)  # fmt: skip


async def read_outputs(db: AsyncSession, run_id: uuid.UUID) -> dict[str, Any]:
    """Each node's latest succeeded output, as the executor hands them to a node."""
    rows = (
        (
            await db.execute(
                sa.select(NodeRun)
                .where(
                    NodeRun.run_id == run_id,
                    NodeRun.node_id.in_(OUTPUT_NODES),
                    NodeRun.status == NodeRunStatus.SUCCEEDED,
                )
                .order_by(NodeRun.node_id, NodeRun.attempt.desc())
            )
        )
        .scalars()
        .all()
    )
    outputs: dict[str, Any] = {}
    for row in rows:
        outputs.setdefault(row.node_id, row.output)
    return outputs


async def read_snapshot(
    db: AsyncSession,
    run: Run,
    *,
    inp: CreativeInput,
    ruleset: RuleSet,
    constants: CreativeConstants,
    outputs: Mapping[str, Any],
    package_id: uuid.UUID,
    version: int,
    status: PackageStatus,
) -> Snapshot:
    """Every row of `run` a package is assembled from, read in one pass.

    `populate_existing` on each read: 4.6.4, H3's routes and G8 all write these
    rows, and a snapshot must be of the database, not of this session's cache.
    """

    async def rows(statement: Any) -> list[Any]:
        result = await db.execute(statement.execution_options(populate_existing=True))
        return list(result.scalars().all())

    brief = (
        await rows(sa.select(CreativeBrief).where(CreativeBrief.creative_run_id == run.id))
    ) or [None]
    approvals = await rows(
        sa.select(Approval).where(Approval.run_id == run.id, Approval.gate_key.in_(GATE_ORDER))
    )
    assets = await rows(sa.select(CreativeAsset).where(CreativeAsset.creative_run_id == run.id))
    media = await rows(
        sa.select(MediaArtifact)
        .join(CreativeAsset, CreativeAsset.id == MediaArtifact.asset_id)
        .where(CreativeAsset.creative_run_id == run.id)
    )
    jobs = await rows(sa.select(GenerationJob).where(GenerationJob.creative_run_id == run.id))
    decisions = await rows(
        sa.select(AssetDecision)
        .join(Approval, Approval.id == AssetDecision.approval_id)
        .where(Approval.run_id == run.id)
    )
    exceptions = await rows(
        sa.select(CreativeException).where(CreativeException.creative_run_id == run.id)
    )
    h3 = (
        await rows(
            sa.select(HumanTask)
            .where(HumanTask.guideline_run_id == run.id, HumanTask.task_key == "H3")
            .order_by(HumanTask.created_at.desc())
            .limit(1)
        )
    ) or [None]
    landing = await rows(
        sa.select(LandingPageAudit).where(LandingPageAudit.creative_run_id == run.id)
    )
    return Snapshot(
        package_id=package_id,
        version=version,
        status=status,
        input=inp,
        ruleset=ruleset,
        constants=constants,
        brief=brief[0],
        approvals=approvals,
        assets=assets,
        media=media,
        jobs=jobs,
        decisions=decisions,
        exceptions=exceptions,
        h3=h3[0],
        landing=landing,
        outputs=dict(outputs),
        text_cost_usd=Decimal(run.cost_usd or 0),
    )


async def package_row(
    db: AsyncSession, run_id: uuid.UUID, *, lock: bool = False
) -> CreativePackageRow | None:
    statement = sa.select(CreativePackageRow).where(CreativePackageRow.creative_run_id == run_id)
    if lock:
        statement = statement.with_for_update()
    return (
        await db.execute(statement.execution_options(populate_existing=True))
    ).scalar_one_or_none()
