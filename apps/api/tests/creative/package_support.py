"""A golden `CreativePackage` and its `CheckContext`, in memory (Stage 04 PRD §11 4.7.2).

Every one of the thirteen blocking checks passes on `golden()`. Each check's
test breaks exactly the one thing its check asserts and expects exactly that
check to say so, naming the asset — so a check that passes everything, or
fails the golden package, is caught either way.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from agent.creative import offers
from agent.creative.checklist import CheckContext
from agent.creative.constants import get_creative_constants
from agent.schemas.creative_package import (
    AssetReviewRef,
    CampaignCreative,
    CostSummary,
    CreativePackage,
    Dependency,
    ExceptionRef,
    Extensions,
    GateDecision,
    HumanTaskRef,
    LintResultRef,
    LintSummary,
    ManifestEntry,
    MediaAsset,
    MediaRendition,
    MinimumCheck,
    Pins,
    Provenance,
    RequiredCount,
    ResponsiveSearchAd,
    ShippedPairs,
    TextAsset,
    VideoFacts,
)
from agent.schemas.creative_qa import ConformanceCheck, SpecConformance
from agent.schemas.guardrails import OfferRecord
from tests.creative.helpers import EXPIRED, LICENSED, ruleset
from tests.creative.package_rows import SHA, specs

NOW = datetime(2026, 9, 26, 12, tzinfo=UTC)
PIN = "1.0+aa"
DOMAIN = "sdsmanager.com"
CAMPAIGN = "c-sds"
AD_GROUP = "sds software"


def uid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


PACKAGE_ID = uid(9000)
RUN_ID = uid(41)
PROJECT_ID = uid(1)
G7_ID, G8_ID = uid(9101), uid(9102)
H3_ID = uid(9201)
IMAGE_ID, VIDEO_ID = uid(9301), uid(9302)
IMAGE_MEDIA, VIDEO_MEDIA = uid(9401), uid(9402)
JOB_IMAGE, JOB_VIDEO = uid(9501), uid(9502)
SITELINK_1, SITELINK_2, PROMOTION = uid(9601), uid(9602), uid(9603)
PASS = LintResultRef(verdict="pass", ruleset_version=PIN)

#: 3 / 3 / 2 / 2 / 2 / 2 — `copy.headline_quotas` — and one more benefit: 15.
CATEGORIES = [
    *["keyword"] * 3, *["benefit"] * 4, *["offer"] * 2, *["proof"] * 2,
    *["objection"] * 2, *["cta"] * 2,
]  # fmt: skip


def headline_ids(variant: str) -> list[uuid.UUID]:
    base = 1000 if variant == "A" else 2000
    return [uid(base + index) for index in range(len(CATEGORIES))]


def description_ids(variant: str) -> list[uuid.UUID]:
    base = 1100 if variant == "A" else 2100
    return [uid(base + index) for index in range(4)]


def text(asset_id: uuid.UUID, kind: str, surface: str, **fields: Any) -> TextAsset:
    return TextAsset.model_validate(
        {
            "asset_id": asset_id,
            "kind": kind,
            "surface": surface,
            "campaign_ref": CAMPAIGN,
            "ad_group_ref": fields.pop("ad_group_ref", AD_GROUP),
            "text": fields.pop("text", f"{kind} {asset_id.int}"),
            "lint": PASS,
            "ruleset_version": PIN,
            "generated_by_ai": True,
            **fields,
        }
    )


def offer_record() -> OfferRecord:
    return OfferRecord.model_validate(
        {
            "sku": "SDS-PRO",
            "product_set": "plans",
            "list_price": 129.0,
            "reference_price": 129.0,
            "current_price": 99.0,
            "currency": "USD",
            "market": "US",
            "effective_from": (NOW - timedelta(days=1)).isoformat(),
            "ends_at": (NOW + timedelta(days=18)).isoformat(),
            "observed_at": (NOW - timedelta(hours=1)).isoformat(),
        }
    )


def _texts() -> list[TextAsset]:
    assets: list[TextAsset] = []
    for variant in ("A", "B"):
        for asset_id, category in zip(headline_ids(variant), CATEGORIES, strict=True):
            assets.append(
                text(asset_id, "headline", "rsa_headline", category=category, variant=variant)
            )
        for asset_id in description_ids(variant):
            assets.append(
                text(
                    asset_id,
                    "description",
                    "rsa_description",
                    claim_ids=[LICENSED],
                    variant=variant,
                )
            )
    for asset_id, url in ((SITELINK_1, "pricing"), (SITELINK_2, "features")):
        final = f"https://{DOMAIN}/{url}"
        assets.append(
            text(
                asset_id,
                "sitelink",
                "sitelink",
                ad_group_ref=None,
                fields={
                    "line1": "Keep every SDS current",
                    "line2": "Built for EHS teams",
                    "final_url": final,
                    "url_check": {
                        "status": "ok",
                        "final_url_after_redirects": final,
                        "http_status": 200,
                    },
                },
            )
        )
    binding = offers.bind(
        offer_record(),
        {
            "percent_off": "percent_off",
            "currency": "currency",
            "start": "effective_from",
            "end": "ends_at",
        },  # fmt: skip
    )
    assets.append(
        text(
            PROMOTION,
            "promotion",
            "promotion",
            ad_group_ref=None,
            category="offer",
            text="SDS software",
            fields={
                "discount_kind": "percent_off",
                "bound": {
                    "percent_off": binding.resolved["percent_off"],
                    "currency": binding.resolved["currency"],
                },
                "start": binding.resolved["start"],
                "end": binding.resolved["end"],
                "final_url": f"https://{DOMAIN}/pricing",
            },
            offer_binding=binding,
        )
    )
    return assets


def _ad(variant: str) -> ResponsiveSearchAd:
    return ResponsiveSearchAd(
        ad_ref=f"{CAMPAIGN}/{AD_GROUP}/{variant}",
        campaign_ref=CAMPAIGN,
        ad_group_ref=AD_GROUP,
        variant=variant,  # type: ignore[arg-type]
        angle="Every sheet current" if variant == "A" else "Audit-ready on demand",
        hypothesis=None if variant == "A" else "B beats A on cost per lead.",
        headlines=headline_ids(variant),
        descriptions=description_ids(variant),
        paths=("sds", "software"),
        final_url=f"https://{DOMAIN}/sds",
        pair_report=ShippedPairs(judged=True),
        distinctness_vs_a=None if variant == "A" else 0.8,
    )


def _rendition(media_id: uuid.UUID, ratio: str, *, video: bool) -> MediaRendition:
    width, height = (1920, 1080) if video else (1200, 628)
    return MediaRendition(
        media_id=media_id,
        path=f"media/{media_id}.{'mp4' if video else 'jpg'}",
        surface="video" if video else "pmax_image",
        aspect_ratio=ratio,
        width=width,
        height=height,
        bytes=400_000,
        media_type="video/mp4" if video else "image/jpeg",
        sha256=SHA,
        derivation="native",
        scale=(1.0, 1.0),
        lint=PASS,
        disclosure={"xmp_digital_source_type": "trainedAlgorithmicMedia"},
        duration_ms=15_000 if video else None,
        video=(
            VideoFacts(
                duration_ms=15_000,
                brand_first_at_ms=1_200,
                captions_burned=True,
                caption_ocr_min_similarity=0.93,
                has_audio=True,
            )
            if video
            else None
        ),
    )


def _media(asset_id: uuid.UUID, media_id: uuid.UUID, job: uuid.UUID, *, video: bool) -> MediaAsset:
    rendition = _rendition(media_id, "16:9" if video else "1.91:1", video=video)
    return MediaAsset(
        asset_id=asset_id,
        modality="video" if video else "image",
        campaign_ref=CAMPAIGN,
        concept_id="c1",
        generated_by_ai=True,
        renditions=[rendition],
        provenance=Provenance(
            model_id="google/veo-3" if video else "openai/gpt-image-1",
            provider="google" if video else "openai",
            job_ids=[job],
            openrouter_generation_ids=["gen-1"] if video else [],
            cost_usd=Decimal("1.50"),
        ),
        product_depiction="none",
        video=rendition.video,
        review=AssetReviewRef(
            gate="G8", round=1, decision="approve", decider=uid(9), decided_at=NOW
        ),
    )


def golden_package() -> CreativePackage:
    texts = _texts()
    media = [
        _media(IMAGE_ID, IMAGE_MEDIA, JOB_IMAGE, video=False),
        _media(VIDEO_ID, VIDEO_MEDIA, JOB_VIDEO, video=True),
    ]
    campaign = CampaignCreative(
        campaign_ref=CAMPAIGN,
        campaign_type="search",
        text_assets=texts,
        ads=[_ad("A"), _ad("B")],
        extensions=Extensions(sitelinks=[SITELINK_1, SITELINK_2], promotions=[PROMOTION]),
        media=media,
        launch_minimums=MinimumCheck(
            campaign_type="search",
            required=[
                RequiredCount(asset_type="headline", required=3, present=15, met=True),
                RequiredCount(asset_type="description", required=2, present=4, met=True),
            ],
            met=True,
        ),
    )
    return CreativePackage(
        package_id=PACKAGE_ID,
        project_id=PROJECT_ID,
        creative_run_id=RUN_ID,
        version=0,
        status="draft",
        pins=Pins(
            plan_id=uid(2),
            plan_version=1,
            ruleset_version=PIN,
            context_hash="cc",
            constants_version="2026.09.1",
            catalogue_hash="d" * 64,
        ),
        brief_hash="brief-1",
        campaigns=[campaign],
        decisions=[
            GateDecision(
                gate_key="G7",
                approval_id=G7_ID,
                node_id="4.1.1",
                status="approved",
                decided_by=uid(11),
                decided_at=NOW,
            ),
            GateDecision(
                gate_key="G8",
                approval_id=G8_ID,
                node_id="4.4.5",
                status="approved",
                decided_by=uid(9),
                decided_at=NOW,
            ),
        ],  # fmt: skip
        human_tasks=[HumanTaskRef(task_id=H3_ID, status="decided", task_status="completed")],
        exceptions=[
            ExceptionRef(
                exception_id=uid(9701),
                kind="new_claim",
                status="cleared",
                subject="#1",
                asset_ids=[description_ids("A")[0]],
            )
        ],
        open_dependencies=[
            Dependency(
                kind="youtube_upload",
                task="Upload the video to YouTube",
                blocking_for="launch",
                campaign_refs=[CAMPAIGN],
            )
        ],
        lint_summary=LintSummary(ruleset_version=PIN),
        manifest=[
            ManifestEntry(path=f"media/{m}.x", sha256=SHA, bytes=1, media_type="image/jpeg")
            for m in (IMAGE_MEDIA, VIDEO_MEDIA)
        ],
        cost=CostSummary(
            text_usd=Decimal("2"),
            image_usd=Decimal("1.5"),
            video_usd=Decimal("1.5"),
            media_estimate_usd=Decimal("3"),
            media_actual_usd=Decimal("3"),
            total_usd=Decimal("5"),
        ),
    )


def conformance() -> SpecConformance:
    def check(asset: uuid.UUID, media: uuid.UUID, constraint: str, source: str) -> Any:
        return ConformanceCheck.model_validate(
            {
                "asset_id": asset,
                "media_id": media,
                "constraint": constraint,
                "expected": "x",
                "measured": "x",
                "source": source,
                "verdict": "pass",
            }
        )

    return SpecConformance(
        ruleset_version=PIN,
        checks=[
            *(check(IMAGE_ID, IMAGE_MEDIA, c, "pillow") for c in ("ratio", "max_bytes", "format")),
            *(
                check(VIDEO_ID, VIDEO_MEDIA, c, "ffprobe")
                for c in ("ratio", "format", "min_duration_s", "max_duration_s")
            ),
        ],
        failed=0,
    )


def golden(**changes: Any) -> CheckContext:
    """The golden package in its context; `changes` replace context fields."""
    pinned = ruleset().model_copy(update={"asset_specs": specs()})
    fields: dict[str, Any] = {
        "package": golden_package(),
        "ruleset": pinned,
        "constants": get_creative_constants(),
        "approved_hash": "brief-1",
        "brief_hash": "brief-1",
        "conformance": conformance(),
        "offers": (offer_record(),),
        "domain": DOMAIN,
        "crm_values": frozenset({"Acme Chemicals Ltd", "Student project"}),
        "now": NOW,
    }
    fields.update(changes)
    return CheckContext(**fields)


def edit(context: CheckContext, **changes: Any) -> CheckContext:
    """`context` with its package's fields replaced."""
    from dataclasses import replace

    return replace(context, package=context.package.model_copy(update=changes))


def edit_campaign(context: CheckContext, **changes: Any) -> CheckContext:
    campaign = context.package.campaigns[0].model_copy(update=changes)
    return edit(context, campaigns=[campaign])


def edit_text(context: CheckContext, asset_id: uuid.UUID, **changes: Any) -> CheckContext:
    campaign = context.package.campaigns[0]
    texts = [
        item.model_copy(update=changes) if item.asset_id == asset_id else item
        for item in campaign.text_assets
    ]
    return edit_campaign(context, text_assets=texts)


def edit_media(context: CheckContext, asset_id: uuid.UUID, **changes: Any) -> CheckContext:
    campaign = context.package.campaigns[0]
    media = [
        item.model_copy(update=changes) if item.asset_id == asset_id else item
        for item in campaign.media
    ]
    return edit_campaign(context, media=media)


__all__ = ["EXPIRED", "LICENSED"]
