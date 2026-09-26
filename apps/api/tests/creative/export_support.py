"""The golden package as an export sees it (Stage 04 PRD §14): released, with a
Performance Max campaign beside S4-P16's Search one, and real file bytes.

`package_support.golden_package()` is the package every §11 check passes on.
An export also needs what the payload does not carry — the frozen plan's
campaign and ad-group *names*, the final pin's spec sheet and the files — so
`sources()` builds a `CreativeExportSources` around it. Every rendition's
`sha256` and `bytes` are the real digest of the blob `blobs()` serves, so a
test that tampers with a file is caught by the check it means to exercise.
"""

from __future__ import annotations

import hashlib
import io
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from PIL import Image

from agent.creative.package import hashed
from agent.export.creative_sources import CreativeExportSources, LandingShot, PreviewShot
from agent.export.plan_contract import PlannedAdGroup, PlannedCampaign
from agent.schemas.creative_package import (
    AssetGroupCreative,
    AssetReviewRef,
    CampaignCreative,
    CreativePackage,
    LintResultRef,
    MediaAsset,
    MediaRendition,
    MinimumCheck,
    Provenance,
    TextAsset,
)
from agent.schemas.guardrails import AssetSpecSheet
from tests.creative import package_support as golden
from tests.creative.helpers import LICENSED
from tests.creative.package_rows import REVIEWED

RELEASED_AT = datetime(2026, 9, 26, 15, 30, tzinfo=UTC)
UPDATED_AT = datetime(2026, 9, 26, 14, 0, tzinfo=UTC)
PMAX = "c-pmax"
PMAX_GROUP = "sds platform"
PMAX_NAME = "SDS | PMax | US"
SEARCH_NAME = "SDS | Search | US"
PASS = LintResultRef(verdict="pass", ruleset_version=golden.PIN)
WARN = LintResultRef(verdict="pass_with_warnings", ruleset_version=golden.PIN, rule_ids=["r-1"])


def uid(n: int) -> uuid.UUID:
    return uuid.UUID(int=n)


PMAX_HEADLINES = [uid(3000 + n) for n in range(3)]
PMAX_LONG = uid(3100)
PMAX_DESCRIPTIONS = [uid(3200 + n) for n in range(2)]
PMAX_BUSINESS = uid(3300)
PMAX_IMAGE, PMAX_IMAGE_WIDE, PMAX_IMAGE_SQUARE = uid(3400), uid(3401), uid(3402)
PMAX_LOGO, PMAX_LOGO_FILE = uid(3500), uid(3501)
POSTER = uid(3600)


def jpeg(width: int, height: int, colour: tuple[int, int, int] = (40, 90, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="JPEG", quality=80)
    return buffer.getvalue()


def png(width: int, height: int, colour: tuple[int, int, int] = (250, 250, 250)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (width, height), colour).save(buffer, format="PNG")
    return buffer.getvalue()


def specs() -> AssetSpecSheet:
    spec = {"source": "unverified", "reviewed_at": REVIEWED}
    return AssetSpecSheet.model_validate(
        {
            "specs": {
                "search": {
                    "headline": {"max_chars": 30, "min_count": 3, "max_count": 15, **spec},
                    "description": {"max_chars": 90, "min_count": 2, "max_count": 4, **spec},
                    "path": {"max_chars": 15, "max_count": 2, **spec},
                    "sitelink": {"max_chars": 25, **spec},
                    "promotion": {"max_chars": 20, **spec},
                    "callout": {"max_chars": 25, **spec},
                    "structured_snippet": {"max_chars": 25, **spec},
                    "price": {"max_chars": 25, **spec},
                    "lead_form": {"max_chars": 30, **spec},
                    "image_landscape": {"ratio": "1.91:1", "max_bytes": 5_242_880, **spec},
                },
                "performance_max": {
                    "headline": {"max_chars": 30, "min_count": 3, "max_count": 15, **spec},
                    "long_headline": {"max_chars": 90, "min_count": 1, "max_count": 5, **spec},
                    "description": {"max_chars": 90, "min_count": 2, "max_count": 5, **spec},
                    "business_name": {"max_chars": 25, **spec},
                    "image_landscape": {"ratio": "1.91:1", "max_bytes": 5_242_880, **spec},
                    "image_square": {"ratio": "1:1", "max_bytes": 5_242_880, **spec},
                    "logo_square": {"ratio": "1:1", "max_bytes": 5_242_880, **spec},
                },
            }
        }
    )  # fmt: skip


def plan_campaigns() -> dict[str, PlannedCampaign]:
    return {
        golden.CAMPAIGN: PlannedCampaign(
            name=SEARCH_NAME,
            campaign_ref=golden.CAMPAIGN,
            type="Search",
            ad_groups=[PlannedAdGroup(name=golden.AD_GROUP)],
        ),
        PMAX: PlannedCampaign(
            name=PMAX_NAME,
            campaign_ref=PMAX,
            type="Performance_Max",
            ad_groups=[PlannedAdGroup(name=PMAX_GROUP)],
        ),
    }


def _pmax_text(asset_id: uuid.UUID, kind: str, surface: str, text: str) -> TextAsset:
    return TextAsset.model_validate(
        {
            "asset_id": asset_id,
            "kind": kind,
            "surface": surface,
            "campaign_ref": PMAX,
            "ad_group_ref": PMAX_GROUP,
            "text": text,
            "lint": PASS,
            "ruleset_version": golden.PIN,
            "generated_by_ai": kind != "business_name",
            "lineage": {"origin": "generated"},
        }
    )


def _image(
    asset_id: uuid.UUID,
    renditions: list[tuple[uuid.UUID, str, int, int]],
    *,
    modality: str = "image",
    concept: str | None = "c2",
) -> MediaAsset:
    return MediaAsset.model_validate(
        {
            "asset_id": asset_id,
            "modality": modality,
            "campaign_ref": PMAX,
            "concept_id": concept,
            "generated_by_ai": modality != "logo",
            "renditions": [
                MediaRendition(
                    media_id=media_id,
                    path=f"media/{asset_id}/{media_id}.jpg",
                    surface="pmax_logo" if modality == "logo" else "pmax_image",
                    aspect_ratio=ratio,
                    width=width,
                    height=height,
                    bytes=1,
                    media_type="image/jpeg",
                    sha256="0" * 64,
                    derivation="native" if modality == "logo" else "saliency_crop",
                    scale=(1.0, 1.0) if modality == "logo" else (0.5, 0.5),
                    lint=PASS,
                    disclosure=None
                    if modality == "logo"
                    else {"xmp_digital_source_type": "trainedAlgorithmicMedia"},
                )
                for media_id, ratio, width, height in renditions
            ],
            "provenance": Provenance(
                model_id=None if modality == "logo" else "openai/gpt-image-1",
                provider=None if modality == "logo" else "openai",
                cost_usd=None if modality == "logo" else Decimal("0.40"),
            ),
            "review": AssetReviewRef(
                gate="G8", round=1, decision="approve", decider=uid(9), decided_at=golden.NOW
            ),
        }
    )


def pmax_campaign() -> CampaignCreative:
    texts = [
        *(
            _pmax_text(asset_id, "headline", "pmax_headline", f"Every SDS current {n}")
            for n, asset_id in enumerate(PMAX_HEADLINES)
        ),
        _pmax_text(
            PMAX_LONG, "long_headline", "long_headline", "Keep every safety data sheet current"
        ),
        *(
            _pmax_text(asset_id, "description", "asset_group_description", f"Audit-ready SDS {n}")
            for n, asset_id in enumerate(PMAX_DESCRIPTIONS)
        ),
        _pmax_text(PMAX_BUSINESS, "business_name", "business_name", "SDS Manager"),
    ]
    image = _image(
        PMAX_IMAGE, [(PMAX_IMAGE_WIDE, "1.91:1", 1200, 628), (PMAX_IMAGE_SQUARE, "1:1", 1200, 1200)]
    )
    logo = _image(PMAX_LOGO, [(PMAX_LOGO_FILE, "1:1", 1200, 1200)], modality="logo", concept=None)
    return CampaignCreative(
        campaign_ref=PMAX,
        campaign_type="performance_max",
        text_assets=texts,
        asset_groups=[
            AssetGroupCreative(
                campaign_ref=PMAX,
                ad_group_ref=PMAX_GROUP,
                campaign_type="performance_max",
                headlines=PMAX_HEADLINES,
                long_headlines=[PMAX_LONG],
                descriptions=PMAX_DESCRIPTIONS,
                business_name=PMAX_BUSINESS,
                media=[PMAX_IMAGE, PMAX_LOGO],
            )
        ],
        media=[image],
        logos=[logo],
        launch_minimums=MinimumCheck(campaign_type="performance_max", met=True),
    )


def _blob(rendition: MediaRendition) -> bytes:
    if rendition.media_type.startswith("video/"):
        return b"\x00\x00\x00\x18ftypmp42" + rendition.media_id.bytes
    return jpeg(rendition.width // 4, rendition.height // 4)


def _with_files(package: CreativePackage) -> tuple[CreativePackage, dict[str, bytes]]:
    blobs: dict[str, bytes] = {}
    campaigns = []
    for campaign in package.campaigns:
        groups: dict[str, list[MediaAsset]] = {"media": [], "logos": []}
        for field in groups:
            for asset in getattr(campaign, field):
                renditions = []
                for rendition in asset.renditions:
                    data = _blob(rendition)
                    blobs[f"package/{package.package_id}/{rendition.path}"] = data
                    renditions.append(
                        rendition.model_copy(
                            update={"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                        )
                    )
                groups[field].append(asset.model_copy(update={"renditions": renditions}))
        campaigns.append(campaign.model_copy(update=groups))
    return package.model_copy(update={"campaigns": campaigns}), blobs


CALLOUT, SNIPPET, LEAD_FORM = uid(3800), uid(3801), uid(3802)
PRICE_ITEMS = [uid(3810), uid(3811), uid(3812)]
PRICE_ASSET = uid(3819)
SNIPPET_VALUES = ["SDS authoring", "SDS management", "Chemical inventory"]


def _extra(asset_id: uuid.UUID, kind: str, text: str, **fields: Any) -> TextAsset:
    return golden.text(asset_id, kind, kind, ad_group_ref=None, text=text, fields=fields)


def with_every_extra(campaign: CampaignCreative) -> CampaignCreative:
    """The golden Search campaign with a callout, a structured snippet, a
    three-item price asset and a lead form beside its sitelinks and promotion."""
    url = f"https://{golden.DOMAIN}/pricing"
    extras = [
        _extra(CALLOUT, "callout", "Audit-ready in minutes"),
        _extra(SNIPPET, "structured_snippet", "Types", values=SNIPPET_VALUES),
        *(
            _extra(item, "price", f"Plan {n}", description="Per site, per year", type="services",
                   qualifier=None, price_asset_id=str(PRICE_ASSET), final_url=url,
                   bound={"price": f"{99 + n}.00", "currency": "USD"})
            for n, item in enumerate(PRICE_ITEMS)
        ),
        _extra(LEAD_FORM, "lead_form", "Book a guided demo", description="See SDS Manager live",
               cta="book_now", privacy_policy_url=f"https://{golden.DOMAIN}/privacy",
               questions=[{"type": "FULL_NAME"}, {"type": "custom", "text": "Sites you run?"}]),
    ]  # fmt: skip
    extensions = campaign.extensions.model_copy(
        update={
            "callouts": [CALLOUT],
            "snippets": [SNIPPET],
            "prices": PRICE_ITEMS,
            "lead_form": LEAD_FORM,
        }
    )
    return campaign.model_copy(
        update={"text_assets": [*campaign.text_assets, *extras], "extensions": extensions}
    )


def released_package(**changes: Any) -> tuple[CreativePackage, dict[str, bytes]]:
    """The golden package, released as v1, with every extra, a PMax campaign
    and real files."""
    base = golden.golden_package()
    package = base.model_copy(
        update={
            "status": "released",
            "version": 1,
            "campaigns": [with_every_extra(base.campaigns[0]), pmax_campaign()],
            **changes,
        }
    )
    package, blobs = _with_files(package)
    return hashed(package), blobs


def media_keys(package: CreativePackage) -> dict[uuid.UUID, str]:
    return {
        rendition.media_id: f"package/{package.package_id}/{rendition.path}"
        for campaign in package.campaigns
        for asset in (*campaign.media, *campaign.logos)
        for rendition in asset.renditions
    }


def sources(
    package: CreativePackage | None = None,
    blobs: dict[str, bytes] | None = None,
    **changes: Any,
) -> CreativeExportSources:
    if package is None:
        package, made = released_package()
        blobs = {**made, **(blobs or {})}
    blobs = dict(blobs or {})
    video = golden.VIDEO_MEDIA
    blobs[f"creative/poster/{POSTER}.jpg"] = jpeg(480, 270, (200, 60, 60))
    previews = []
    for ad in package.campaigns[0].ads:
        for device, size in (("mobile", (824, 1100)), ("desktop", (1300, 480))):
            key = f"creative/previews/{ad.variant}-{device}.png"
            blobs[key] = png(*size)
            previews.append(
                PreviewShot(
                    ad_ref=ad.ad_ref, device=device, verdict="pass", key=key, template_version="1"
                )
            )
    for device, size in (("mobile", (780, 3000)), ("desktop", (1280, 2400))):
        blobs[f"creative/landing/{device}.png"] = png(*size, colour=(230, 236, 240))
    fields: dict[str, Any] = {
        "package": package,
        "project_name": "Northwind Safety",
        "released_at": RELEASED_AT if package.status in ("released", "superseded") else None,
        "generated_at": UPDATED_AT,
        "campaigns": plan_campaigns(),
        "specs": specs(),
        "media_keys": media_keys(package),
        "posters": {video: f"creative/poster/{POSTER}.jpg"},
        "read": blobs.__getitem__,
        "brief_markdown": BRIEF,
        "claims": {LICENSED: "sds updates within 24 hours"},
        "people": {uid(9): "Bea Brand", uid(11): "Pat Performance", uid(12): "Lee Legal"},
        "approval_roles": {golden.G7_ID: "approver", golden.G8_ID: "approver"},
        "previews": tuple(previews),
        "landing": (landing_shot(),),
        "h3": h3_receipt(),
    }
    fields.update(changes)
    return CreativeExportSources(**fields)


BRIEF = """# Creative brief

**Objective.** Book demos from EHS managers.

**Angle.** Every sheet current, audit-ready.

## Audience
- EHS managers at mid-size manufacturers

## Ad groups

### c-sds / sds software
- Theme: SDS software
- Landing: https://sdsmanager.com/sds
"""
AUDIT = uid(3700)
SIGNATURE = uid(3701)


def landing_shot() -> LandingShot:
    return LandingShot(
        audit_id=AUDIT,
        url=f"https://{golden.DOMAIN}/sds",
        verdict="needs_change",
        ad_group_refs=(golden.AD_GROUP,),
        h1_before={"mobile": "Welcome", "desktop": "Welcome to SDS Manager"},
        patch={
            "h1": "Keep every SDS current",
            "offer_block": {"phrase": "23% off SDS Pro", "devices": ["mobile"]},
            "remove_fields": ["fax"],
            "html_snippet": "<h1>Keep every SDS current</h1>",
        },
        screenshots={
            "mobile": "creative/landing/mobile.png",
            "desktop": "creative/landing/desktop.png",
        },
        fold_px={"mobile": 780, "desktop": 800},
        patch_path=f"landing/{AUDIT}/patch.json",
    )


def h3_receipt() -> dict[str, Any]:
    return {
        "decided_by": str(uid(12)),
        "decided_at": golden.NOW.isoformat(),
        "statement": "I license claim #1 for this creative run.",
        "signature_id": str(SIGNATURE),
        "ruleset_version": "1.1+bb",
        "decided_hash": "e" * 64,
        "register_hash": "f" * 64,
        "cleared": [str(uid(9701))],
        "rejected": [],
    }


def draft_sources(**changes: Any) -> CreativeExportSources:
    """The same package before release: v0, `ready_to_release`, no released_at."""
    package, blobs = released_package(status="ready_to_release", version=0)
    return sources(package, blobs, **changes)
