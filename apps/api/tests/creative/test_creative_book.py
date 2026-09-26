"""The creative book — MD and its PDF (Stage 04 PRD §14).

The PDF is read back with `pypdf` rather than trusted: the watermark is looked
for in each page's own text (acceptance 6), the metadata dates are read out of
the document info (acceptance 4), and the §14 load is rendered for real and
weighed (acceptance 5) with noise images — the worst case for JPEG — so the
ceiling holds for content that does not compress.
"""

from __future__ import annotations

import io
import os
import re
import time
import uuid
from typing import Any

from PIL import Image
from pypdf import PdfReader

from agent.creative.package import hashed
from agent.export.creative_book_pdf import render_creative_book_html, render_creative_book_pdf
from agent.export.creative_markdown import render_creative_markdown
from agent.export.creative_sources import DRAFT_WATERMARK, LandingShot, PreviewShot
from agent.export.plan_contract import PlannedAdGroup, PlannedCampaign
from agent.schemas.creative_package import (
    CampaignCreative,
    MediaAsset,
    MediaRendition,
    MinimumCheck,
    Provenance,
    ResponsiveSearchAd,
    ShippedPairs,
    TextAsset,
)
from tests.creative import export_support as support
from tests.creative import package_support as golden

SECTIONS = [
    "1. Brief",
    "2. Ads by ad group",
    "3. Extras",
    "4. Media contact sheet",
    "5. Landing audits",
    "6. Exceptions and H3",
    "7. Package summary",
    "8. Sign-off",
]
MAX_PDF_BYTES = 25 * 1024 * 1024


def _pages(pdf: bytes) -> list[str]:
    return [page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf)).pages]


def _h2(markdown: str) -> list[str]:
    return re.findall(r"^## (.+)$", markdown, flags=re.MULTILINE)


# -- the markdown ------------------------------------------------------------


def test_the_markdown_has_every_section_14_names_in_order() -> None:
    assert _h2(render_creative_markdown(support.sources())) == SECTIONS


def test_the_cover_carries_version_status_and_every_pin() -> None:
    sources = support.sources()
    md = render_creative_markdown(sources)
    pins = sources.package.pins
    for value in (
        "v1",
        "released",
        str(sources.package.creative_run_id),
        sources.package.package_hash,
        str(pins.plan_id),
        pins.ruleset_version,
        pins.context_hash,
        pins.constants_version,
        pins.catalogue_hash,
        "2026-09-26 15:30 UTC",
    ):
        assert value in md, value


def test_the_brief_is_the_approved_brief_verbatim() -> None:
    md = render_creative_markdown(support.sources())
    assert "**Objective.** Book demos from EHS managers." in md
    assert "#### Audience" in md  # demoted under section 1, not a peer of it


def test_each_ad_group_shows_its_headline_matrix_with_categories_and_counts() -> None:
    md = render_creative_markdown(support.sources())
    first = golden.headline_ids("A")[0]
    assert f"### {support.SEARCH_NAME} / {golden.AD_GROUP}" in md
    assert f"| 1 | headline {first.int} | keyword | 13/30 |" in md
    assert "| objection |" in md and "| cta |" in md


def test_descriptions_carry_their_claim_chips() -> None:
    md = render_creative_markdown(support.sources())
    description = golden.description_ids("A")[0]
    assert f"description {description.int} | 16/90 | `sds updates within 24 hours` |" in md


def test_variant_b_carries_its_hypothesis() -> None:
    md = render_creative_markdown(support.sources())
    assert "Variant B — Audit-ready on demand" in md
    assert "**Hypothesis.** B beats A on cost per lead." in md


def test_extras_are_listed_per_campaign() -> None:
    md = render_creative_markdown(support.sources())
    assert f"sitelink {golden.SITELINK_1.int}" in md
    assert "Keep every SDS current" in md
    assert "SDS software" in md  # the promotion's target


def test_the_contact_sheet_has_provenance_per_asset() -> None:
    md = render_creative_markdown(support.sources())
    assert "openai/gpt-image-1" in md and "google/veo-3" in md
    assert "1.50 USD" in md
    assert "trainedAlgorithmicMedia" in md
    assert "G8 approve by Bea Brand" in md


def test_landing_audits_show_before_and_after() -> None:
    md = render_creative_markdown(support.sources())
    assert f"https://{golden.DOMAIN}/sds" in md
    assert "Welcome to SDS Manager" in md  # before
    assert "Keep every SDS current" in md  # after
    assert "fax" in md
    assert f"landing/{support.AUDIT}/patch.json" in md


def test_exceptions_carry_the_h3_receipt() -> None:
    md = render_creative_markdown(support.sources())
    assert "new claim" in md and "cleared" in md
    assert "I license claim #1 for this creative run." in md
    assert str(support.SIGNATURE) in md
    assert "e" * 64 in md


def test_the_sign_off_page_lists_g7_g8_g8b_and_h3_with_decider_method_and_time() -> None:
    md = render_creative_markdown(support.sources())
    sign_off = md.split("## 8. Sign-off", 1)[1]
    assert re.search(
        r"\| G7 \| approved \| Pat Performance \| Approval[^|]*\| 2026-09-26 12:00 UTC \|", sign_off
    )
    assert re.search(
        r"\| G8 \| approved \| Bea Brand \| Approval[^|]*\| 2026-09-26 12:00 UTC \|", sign_off
    )
    assert re.search(r"\| G8b \| not reached \|", sign_off)
    assert re.search(
        r"\| H3 \| decided \| Lee Legal \| Person-task[^|]*step-up[^|]*\| 2026-09-26 12:00 UTC \|",
        sign_off,
    )


def test_a_draft_markdown_is_watermarked_and_a_released_one_is_not() -> None:
    assert DRAFT_WATERMARK in render_creative_markdown(support.draft_sources())
    assert DRAFT_WATERMARK not in render_creative_markdown(support.sources())


def test_the_markdown_is_the_same_bytes_every_time(monkeypatch: Any) -> None:
    first = render_creative_markdown(support.sources())
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 86_400)
    assert render_creative_markdown(support.sources()) == first


def test_the_print_html_renders_the_markdowns_sections_in_the_same_order() -> None:
    sources = support.sources()
    html = render_creative_book_html(sources)
    headings = [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<h2[^>]*>(.*?)</h2>", html)]
    assert headings == _h2(render_creative_markdown(sources))
    h3_md = re.findall(r"^### (.+)$", render_creative_markdown(sources), flags=re.MULTILINE)
    h3_html = [re.sub(r"<[^>]+>", "", h).strip() for h in re.findall(r"<h3[^>]*>(.*?)</h3>", html)]
    assert [h.replace("&amp;", "&") for h in h3_html] == h3_md


def test_the_print_html_embeds_previews_renditions_posters_and_landing_shots() -> None:
    html = render_creative_book_html(support.sources())
    # 4 SERP previews, 4 still renditions, 1 video poster, 2 landing screenshots
    assert html.count('src="data:image/jpeg;base64,') == 4 + 4 + 1 + 2


def test_an_unreadable_image_is_named_not_fatal() -> None:
    sources = support.sources()
    broken = dict.fromkeys(("creative/landing/mobile.png",), b"not an image")
    html = render_creative_book_html(
        support.sources(read=lambda key: broken[key] if key in broken else sources.read(key))
    )
    assert "could not be read" in html


# -- the PDF -----------------------------------------------------------------


def test_a_draft_pdf_is_watermarked_on_page_one_and_every_page_after() -> None:
    sources = support.draft_sources()
    pages = _pages(render_creative_book_pdf(sources))
    assert len(pages) >= 4
    for number, text in enumerate(pages, start=1):
        assert DRAFT_WATERMARK in text, f"page {number} carries no watermark"
        assert "ready_to_release" in text and str(sources.package.creative_run_id) in text, number


def test_a_released_pdf_is_not_watermarked() -> None:
    pages = _pages(render_creative_book_pdf(support.sources()))
    assert not any(DRAFT_WATERMARK in text for text in pages)
    assert all("released" in text for text in pages)


def test_two_pdf_exports_are_byte_identical_across_a_clock_shift(monkeypatch: Any) -> None:
    first = render_creative_book_pdf(support.sources())
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + 86_400 * 2 + 3)
    assert render_creative_book_pdf(support.sources()) == first


def test_the_pdf_metadata_dates_are_pinned_to_released_at() -> None:
    meta = PdfReader(io.BytesIO(render_creative_book_pdf(support.sources()))).metadata
    assert meta is not None
    assert meta["/CreationDate"] == meta["/ModDate"] == "D:20260926153000Z"


def test_a_draft_pdf_is_stamped_with_its_rows_last_change() -> None:
    meta = PdfReader(io.BytesIO(render_creative_book_pdf(support.draft_sources()))).metadata
    assert meta is not None and meta["/CreationDate"] == "D:20260926140000Z"


# -- acceptance 5: the §14 load ---------------------------------------------


def _noise(width: int, height: int) -> bytes:
    image = Image.frombytes("RGB", (width, height), os.urandom(width * height * 3))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=92)
    return buffer.getvalue()


def _load() -> tuple[Any, dict[str, bytes]]:
    """10 ad groups × 2 RSAs, 3 concepts, 4 videos (posters only), and a
    landing audit per ad group — every image noise."""
    package, blobs = support.released_package()
    groups = [f"ag {n}" for n in range(10)]
    texts: list[TextAsset] = []
    ads: list[ResponsiveSearchAd] = []
    serial = 50_000
    for group in groups:
        for variant in ("A", "B"):
            heads, descs = [], []
            for index, category in enumerate(golden.CATEGORIES):
                serial += 1
                heads.append(uuid.UUID(int=serial))
                texts.append(golden.text(heads[-1], "headline", "rsa_headline", ad_group_ref=group,
                                         category=category, variant=variant,
                                         text=f"Headline {index} for {group}"))  # fmt: skip
            for index in range(4):
                serial += 1
                descs.append(uuid.UUID(int=serial))
                texts.append(golden.text(descs[-1], "description", "rsa_description",
                                         ad_group_ref=group, claim_ids=[support.LICENSED],
                                         variant=variant,
                                         text=f"Description {index} for {group}, audit-ready."))  # fmt: skip
            ads.append(
                ResponsiveSearchAd(
                    ad_ref=f"{golden.CAMPAIGN}/{group}/{variant}", campaign_ref=golden.CAMPAIGN,
                    ad_group_ref=group, variant=variant, angle=f"Angle {variant}",  # type: ignore[arg-type]
                    hypothesis=None if variant == "A" else "B wins.", headlines=heads,
                    descriptions=descs, paths=("sds", "software"),
                    final_url=f"https://{golden.DOMAIN}/{group.replace(' ', '-')}",
                    pair_report=ShippedPairs(judged=True),
                )
            )  # fmt: skip
    media: list[MediaAsset] = []
    posters: dict[uuid.UUID, str] = {}
    for n in range(7):
        serial += 10
        video = n >= 3
        sizes = [("16:9", 1920, 1080)] if video else [
            ("1.91:1", 1200, 628), ("1:1", 1200, 1200), ("4:5", 960, 1200)]  # fmt: skip
        renditions = []
        for index, (ratio, width, height) in enumerate(sizes):
            media_id = uuid.UUID(int=serial + index)
            key = f"package/{package.package_id}/media/{media_id}"
            blobs[key] = b"mp4" if video else _noise(width, height)
            if video:
                posters[media_id] = f"posters/{media_id}.jpg"
                blobs[posters[media_id]] = _noise(width, height)
            renditions.append(
                MediaRendition(
                    media_id=media_id, path=f"media/{media_id}", surface="video" if video else "image",
                    aspect_ratio=ratio, width=width, height=height, bytes=len(blobs[key]),
                    media_type="video/mp4" if video else "image/jpeg", sha256="0" * 64,
                    derivation="native", lint=support.PASS, duration_ms=15_000 if video else None,
                )
            )  # fmt: skip
        media.append(
            MediaAsset(
                asset_id=uuid.UUID(int=serial + 9), modality="video" if video else "image",
                campaign_ref=golden.CAMPAIGN, concept_id=f"c{n % 3}", generated_by_ai=True,
                renditions=renditions, provenance=Provenance(model_id="m", cost_usd=None),
            )
        )  # fmt: skip
    search = CampaignCreative(
        campaign_ref=golden.CAMPAIGN, campaign_type="search", text_assets=texts, ads=ads,
        media=media, launch_minimums=MinimumCheck(campaign_type="search", met=True),
    )  # fmt: skip
    package = hashed(package.model_copy(update={"campaigns": [search]}))
    previews = []
    for ad in ads:
        for device, (width, height) in (("mobile", (824, 1400)), ("desktop", (1300, 520))):
            key = f"previews/{ad.ad_ref}/{device}"
            blobs[key] = _noise(width, height)
            previews.append(PreviewShot(ad_ref=ad.ad_ref, device=device, verdict="pass", key=key,
                                        template_version="1"))  # fmt: skip
    landing = []
    for n, group in enumerate(groups):
        shots = {}
        for device, (width, height) in (("mobile", (780, 3200)), ("desktop", (1280, 3000))):
            shots[device] = f"landing/{n}/{device}"
            blobs[shots[device]] = _noise(width, height)
        landing.append(
            LandingShot(
                audit_id=uuid.UUID(int=70_000 + n), url=f"https://{golden.DOMAIN}/{n}",
                verdict="needs_change", ad_group_refs=(group,), h1_before={"desktop": "Welcome"},
                patch={"h1": "Keep every SDS current", "remove_fields": [], "html_snippet": "<h1/>"},
                screenshots=shots, fold_px={"mobile": 780, "desktop": 800}, patch_path="",
            )
        )  # fmt: skip
    campaigns = {
        golden.CAMPAIGN: PlannedCampaign(
            name=support.SEARCH_NAME, campaign_ref=golden.CAMPAIGN, type="Search",
            ad_groups=[PlannedAdGroup(name=group) for group in groups],
        )
    }  # fmt: skip
    media_keys = {
        r.media_id: f"package/{package.package_id}/{r.path}" for a in media for r in a.renditions
    }
    return {
        "campaigns": campaigns,
        "media_keys": media_keys,
        "posters": posters,
        "previews": tuple(previews),
        "landing": tuple(landing),
    }, blobs | {"package": package}  # type: ignore[dict-item]


def test_the_pdf_of_the_section_14_load_is_at_most_25_mb() -> None:
    changes, blobs = _load()
    package = blobs.pop("package")
    pdf = render_creative_book_pdf(support.sources(package, blobs, **changes))  # type: ignore[arg-type]
    pages = PdfReader(io.BytesIO(pdf)).pages
    assert len(pdf) <= MAX_PDF_BYTES, f"{len(pdf) / 1024 / 1024:.1f} MB"
    assert len(pages) > 20
