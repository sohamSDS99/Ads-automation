"""The creative book's context — one object for the markdown and the print HTML (§14).

§14 names MD "the source of truth for the PDF". As in Stages 01–03 that is
held by construction: both templates render *this* context, section for
section in the same order, and `test_creative_book` compares their headings.
Nothing here judges — every status, verdict and count is the package's or
its rows'; this module arranges them and resolves names, limits and images.

**Deterministic.** No clock is read, every list keeps the package's (already
sorted) order, and images are re-encoded with fixed settings, so two renders of
one package are the same bytes.

**Bounded.** §14 acceptance 5 caps the PDF at 25 MB for 10 ad groups × 2 RSAs,
3 concepts and 4 videos. Every image is therefore re-encoded as a JPEG inside
a fixed box before it is embedded — a SERP preview at most 560×900 px, a
landing screenshot cropped to the top of the page and fitted to 420×840, a
contact-sheet tile 300×300 — and a video appears as its poster frame, never as
itself. The box, not the source file, bounds the document.
"""

from __future__ import annotations

import base64
import html
import io
import re
import uuid
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any, Final

from PIL import Image, UnidentifiedImageError

from agent.export.creative_sources import CreativeExportSources, chars
from agent.export.templating import EMPTY, fmt_datetime, fmt_money
from agent.schemas.creative_package import (
    CampaignCreative,
    MediaAsset,
    ResponsiveSearchAd,
    TextAsset,
)

#: (max width, max height) of each embedded image, in pixels.
PREVIEW_BOX: Final = (560, 900)
LANDING_BOX: Final = (420, 840)
TILE_BOX: Final = (300, 300)
#: A landing screenshot is a whole page; the audit is about its top. Kept:
#: at most this many heights per width (a phone screen is about 2:1).
LANDING_MAX_ASPECT: Final = 2.0
JPEG_QUALITY: Final = 80

GATE_METHODS: Final[Mapping[str, str]] = {
    "G7": "Approval — the brief, scoped to its hash",
    "G8": "Approval — per-asset AI media review",
    "G8b": "Approval — the regenerated assets, final round",
}
H3_METHOD: Final = "Person-task — named legal owner, password step-up"
DEVICES: Final = ("mobile", "desktop")


# ---------------------------------------------------------------------------
# small formatters
# ---------------------------------------------------------------------------


def _words(value: str | None) -> str:
    return value.replace("_", " ") if value else EMPTY


def _short(identifier: uuid.UUID | str | None) -> str:
    return str(identifier)[:8] if identifier else EMPTY


def _when(value: Any) -> str:
    if isinstance(value, str) and value:
        try:
            return fmt_datetime(datetime.fromisoformat(value))
        except ValueError:
            return value
    return fmt_datetime(value)


def _count(text: str | None, limit: int | None) -> str:
    if text is None:
        return EMPTY
    return f"{chars(text)}/{limit}" if limit is not None else str(chars(text))


# ---------------------------------------------------------------------------
# images
# ---------------------------------------------------------------------------


def _jpeg(data: bytes, box: tuple[int, int], *, max_aspect: float | None = None) -> str:
    """`data` as a `data:` URI of a JPEG inside `box`. Raises on a file that is
    not an image; the caller names it."""
    with Image.open(io.BytesIO(data)) as source:
        source.load()
        image: Image.Image = source
        if image.mode in ("RGBA", "LA", "P"):
            rgba = image.convert("RGBA")
            flat = Image.new("RGB", rgba.size, (255, 255, 255))
            flat.paste(rgba, mask=rgba.getchannel("A"))
            image = flat
        elif image.mode != "RGB":
            image = image.convert("RGB")
        if max_aspect is not None and image.height > image.width * max_aspect:
            image = image.crop((0, 0, image.width, int(image.width * max_aspect)))
        image = image.copy()
        image.thumbnail(box, Image.Resampling.LANCZOS)
        out = io.BytesIO()
        image.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=False, progressive=False)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode("ascii")


def _picture(
    sources: CreativeExportSources,
    key: str | None,
    box: tuple[int, int],
    *,
    images: bool,
    max_aspect: float | None = None,
) -> dict[str, str | None]:
    """`{"src": data-uri | None, "note": why there is no picture | None}`."""
    if not images:
        return {"src": None, "note": None}
    if key is None:
        return {"src": None, "note": "not rendered"}
    data = sources.try_read(key)
    if data is None:
        return {"src": None, "note": "not stored"}
    try:
        return {"src": _jpeg(data, box, max_aspect=max_aspect), "note": None}
    except (UnidentifiedImageError, OSError, ValueError):
        return {"src": None, "note": "could not be read"}


# ---------------------------------------------------------------------------
# the brief
# ---------------------------------------------------------------------------

_HEADING = re.compile(r"^(#{1,6}) (.+)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_CODE = re.compile(r"`([^`]+)`")
#: The brief sits under section 1's `##`, so its own levels move down two.
BRIEF_SHIFT: Final = 2


def demote(markdown: str, shift: int = BRIEF_SHIFT) -> str:
    """The brief's headings, `shift` levels down (never past `######`)."""

    def one(line: str) -> str:
        match = _HEADING.match(line)
        if match is None:
            return line
        return "#" * min(len(match.group(1)) + shift, 6) + " " + match.group(2)

    return "\n".join(one(line) for line in markdown.strip("\n").splitlines())


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = _BOLD.sub(r"<strong>\1</strong>", escaped)
    return _CODE.sub(r"<code>\1</code>", escaped)


def brief_html(markdown: str) -> str:
    """The stored brief as HTML: headings, `- ` lists and paragraphs with bold
    and code — exactly the constructs `creative/templates/creative_brief.md.j2`
    emits. Everything is escaped first; nothing in the brief can become markup.
    """
    out: list[str] = []
    in_list = False
    paragraph: list[str] = []

    def flush() -> None:
        nonlocal in_list
        if paragraph:
            out.append("<p>" + " ".join(_inline(part) for part in paragraph) + "</p>")
            paragraph.clear()
        if in_list:
            out.append("</ul>")
            in_list = False

    for line in demote(markdown).splitlines():
        heading = _HEADING.match(line)
        if heading:
            flush()
            level = len(heading.group(1))
            out.append(f"<h{level}>{_inline(heading.group(2))}</h{level}>")
        elif line.startswith("- "):
            if paragraph:
                out.append("<p>" + " ".join(_inline(part) for part in paragraph) + "</p>")
                paragraph.clear()
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{_inline(line[2:])}</li>")
        elif not line.strip():
            flush()
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            paragraph.append(line.strip())
    flush()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# ads by ad group
# ---------------------------------------------------------------------------


def _name(sources: CreativeExportSources, campaign_ref: str) -> str:
    planned = sources.campaigns.get(campaign_ref)
    return planned.name if planned is not None else campaign_ref


def _texts(campaign: CampaignCreative) -> dict[uuid.UUID, TextAsset]:
    return {asset.asset_id: asset for asset in campaign.text_assets}


def _lint(asset: TextAsset) -> str:
    verdict = _words(asset.lint.verdict)
    return f"{verdict} ({', '.join(asset.lint.rule_ids)})" if asset.lint.rule_ids else verdict


def _headline_rows(
    sources: CreativeExportSources, campaign: CampaignCreative, ids: Sequence[uuid.UUID]
) -> list[dict[str, Any]]:
    texts = _texts(campaign)
    rows = []
    for number, asset_id in enumerate(ids, start=1):
        asset = texts.get(asset_id)
        if asset is None:
            continue
        rows.append(
            {
                "n": number,
                "text": asset.text or EMPTY,
                "category": asset.category or EMPTY,
                "chars": _count(asset.text, sources.limit(campaign.campaign_type, asset.surface)),
                "pin": asset.pin_position or EMPTY,
                "lint": _lint(asset),
            }
        )
    return rows


def _description_rows(
    sources: CreativeExportSources, campaign: CampaignCreative, ids: Sequence[uuid.UUID]
) -> list[dict[str, Any]]:
    texts = _texts(campaign)
    rows = []
    for number, asset_id in enumerate(ids, start=1):
        asset = texts.get(asset_id)
        if asset is None:
            continue
        rows.append(
            {
                "n": number,
                "text": asset.text or EMPTY,
                "chars": _count(asset.text, sources.limit(campaign.campaign_type, asset.surface)),
                "claims": [
                    sources.claims.get(claim, f"claim {_short(claim)}") for claim in asset.claim_ids
                ],
                "lint": _lint(asset),
            }
        )
    return rows


def _previews(
    sources: CreativeExportSources, ad: ResponsiveSearchAd, *, images: bool
) -> list[dict[str, Any]]:
    shots = {shot.device: shot for shot in sources.previews if shot.ad_ref == ad.ad_ref}
    out = []
    for device in DEVICES:
        shot = shots.get(device)
        if shot is None:
            out.append({"device": device, "verdict": "not rendered", "template": EMPTY,
                        "src": None, "note": "not rendered" if images else None})  # fmt: skip
            continue
        picture = _picture(sources, shot.key, PREVIEW_BOX, images=images)
        out.append(
            {
                "device": device,
                "verdict": _words(shot.verdict),
                "template": shot.template_version,
                **picture,
            }
        )
    return out


def _ad(
    sources: CreativeExportSources,
    campaign: CampaignCreative,
    ad: ResponsiveSearchAd,
    *,
    images: bool,
) -> dict[str, Any]:
    return {
        "variant": ad.variant,
        "angle": ad.angle,
        "hypothesis": ad.hypothesis,
        "distinctness": (
            f"{ad.distinctness_vs_a:.2f}" if ad.distinctness_vs_a is not None else None
        ),
        "final_url": ad.final_url,
        "paths": " / ".join(path for path in ad.paths if path) or EMPTY,
        "previews": _previews(sources, ad, images=images),
        "headlines": _headline_rows(sources, campaign, ad.headlines),
        "descriptions": _description_rows(sources, campaign, ad.descriptions),
        "unresolved_pairs": len(ad.pair_report.unresolved),
    }


def _groups(sources: CreativeExportSources, *, images: bool) -> list[dict[str, Any]]:
    groups: list[dict[str, Any]] = []
    for campaign in sources.package.campaigns:
        name = _name(sources, campaign.campaign_ref)
        by_group: dict[str, dict[str, Any]] = {}
        for ad in campaign.ads:
            entry = by_group.setdefault(
                ad.ad_group_ref,
                {"title": f"{name} / {ad.ad_group_ref}", "ads": [], "asset_group": None},
            )
            entry["ads"].append(_ad(sources, campaign, ad, images=images))
        texts = _texts(campaign)
        for group in campaign.asset_groups:
            entry = by_group.setdefault(
                group.ad_group_ref,
                {"title": f"{name} / {group.ad_group_ref}", "ads": [], "asset_group": None},
            )
            business = texts.get(group.business_name) if group.business_name else None
            entry["asset_group"] = {
                "type": _words(group.campaign_type),
                "headlines": _headline_rows(sources, campaign, group.headlines),
                "long_headlines": _headline_rows(sources, campaign, group.long_headlines),
                "descriptions": _description_rows(sources, campaign, group.descriptions),
                "business_name": business.text if business is not None else EMPTY,
                "media": len(group.media),
            }
        groups.extend(by_group.values())
    return groups


# ---------------------------------------------------------------------------
# extras
# ---------------------------------------------------------------------------


def _field(asset: TextAsset, key: str) -> str:
    value = asset.fields.get(key)
    return EMPTY if value in (None, "") else str(value)


def _extras(sources: CreativeExportSources) -> list[dict[str, Any]]:
    out = []
    for campaign in sources.package.campaigns:
        ext = campaign.extensions
        texts = _texts(campaign)

        def pick(ids: Iterable[uuid.UUID]) -> list[TextAsset]:
            return [texts[asset_id] for asset_id in ids if asset_id in texts]  # noqa: B023

        sitelinks = [
            {
                "text": asset.text or EMPTY,
                "line1": _field(asset, "line1"),
                "line2": _field(asset, "line2"),
                "url": _field(asset, "final_url"),
                "lint": _lint(asset),
            }
            for asset in pick(ext.sitelinks)
        ]
        callouts = [
            {"text": asset.text or EMPTY, "chars": _count(asset.text, sources.limit(
                campaign.campaign_type, asset.surface)), "lint": _lint(asset)}
            for asset in pick(ext.callouts)
        ]  # fmt: skip
        snippets = [
            {"header": asset.text or EMPTY,
             # Not `values`: Jinja resolves `row.values` to `dict.values` first.
             "entries": "; ".join(str(v) for v in asset.fields.get("values") or []) or EMPTY,
             "lint": _lint(asset)}
            for asset in pick(ext.snippets)
        ]  # fmt: skip
        promotions = []
        for asset in pick(ext.promotions):
            bound = asset.fields.get("bound") or {}
            discount = (
                f"{bound['percent_off']}% off"
                if bound.get("percent_off")
                else f"{bound.get('money_off')} {bound.get('currency', '')} off".strip()
                if bound.get("money_off")
                else EMPTY
            )
            promotions.append(
                {
                    "text": asset.text or EMPTY,
                    "discount": discount,
                    "window": f"{_field(asset, 'start')} → {_field(asset, 'end')}",
                    "url": _field(asset, "final_url"),
                    "bound": "yes" if asset.offer_binding is not None else "no",
                }
            )
        prices = []
        for asset in pick(ext.prices):
            bound = asset.fields.get("bound") or {}
            prices.append(
                {
                    "type": _field(asset, "type"),
                    "header": asset.text or EMPTY,
                    "description": _field(asset, "description"),
                    "price": f"{bound.get('price', EMPTY)} {bound.get('currency', '')}".strip(),
                    "url": _field(asset, "final_url"),
                }
            )
        lead_form = None
        if ext.lead_form is not None and ext.lead_form in texts:
            asset = texts[ext.lead_form]
            lead_form = {
                "headline": asset.text or EMPTY,
                "description": _field(asset, "description"),
                "cta": _field(asset, "cta"),
                "questions": ", ".join(
                    str(q.get("text") or q.get("type")) for q in asset.fields.get("questions") or []
                    if isinstance(q, Mapping)
                ) or EMPTY,
                "privacy": _field(asset, "privacy_policy_url"),
            }  # fmt: skip
        if sitelinks or callouts or snippets or promotions or prices or lead_form:
            out.append(
                {
                    "title": _name(sources, campaign.campaign_ref),
                    "sitelinks": sitelinks,
                    "callouts": callouts,
                    "snippets": snippets,
                    "promotions": promotions,
                    "prices": prices,
                    "lead_form": lead_form,
                }
            )
    return out


# ---------------------------------------------------------------------------
# media contact sheet
# ---------------------------------------------------------------------------


def _media_asset(
    sources: CreativeExportSources, campaign: CampaignCreative, asset: MediaAsset, *, images: bool
) -> dict[str, Any]:
    provenance = asset.provenance
    review = asset.review
    renditions = []
    for rendition in asset.renditions:
        if asset.modality == "video":
            picture = _picture(
                sources, sources.posters.get(rendition.media_id), TILE_BOX, images=images
            )
            if images and picture["src"] is None and picture["note"] == "not rendered":
                picture["note"] = "no poster frame"
        else:
            picture = _picture(
                sources, sources.media_keys.get(rendition.media_id), TILE_BOX, images=images
            )
        limit = sources.max_bytes(campaign.campaign_type, asset.modality, rendition.aspect_ratio)
        renditions.append(
            {
                "media_id": _short(rendition.media_id),
                "path": rendition.path,
                "ratio": rendition.aspect_ratio,
                "px": f"{rendition.width}×{rendition.height}",
                "bytes": f"{rendition.bytes:,}" + (f" / {limit:,}" if limit else ""),
                "derivation": _words(rendition.derivation),
                "scale": f"{rendition.scale[0]:g} × {rendition.scale[1]:g}",
                "duration": f"{rendition.duration_ms / 1000:g} s" if rendition.duration_ms
                else None,
                "disclosure": ", ".join(f"{key}: {value}" for key, value in sorted(
                    (rendition.disclosure or {}).items())) or EMPTY,
                "lint": _words(rendition.lint.verdict),
                "sha256": rendition.sha256,
                **picture,
            }
        )  # fmt: skip
    review_line = (
        f"{review.gate} {review.decision} by {sources.name(review.decider) or EMPTY}, "
        f"{fmt_datetime(review.decided_at)}"
        if review.gate is not None and review.decision is not None
        else "not reviewed"
    )
    return {
        "title": " · ".join(
            (asset.modality, asset.concept_id or "no concept", _short(asset.asset_id))
        ),
        "generated_by_ai": "AI-generated" if asset.generated_by_ai else "supplied",
        "product_depiction": _words(asset.product_depiction),
        "model": provenance.model_id or EMPTY,
        "provider": provenance.provider or EMPTY,
        "seed": EMPTY if provenance.seed is None else str(provenance.seed),
        "prompt_hash": provenance.prompt_hash or EMPTY,
        "references": len(provenance.reference_sha256s),
        "jobs": ", ".join(_short(job) for job in provenance.job_ids) or EMPTY,
        "cost": fmt_money(provenance.cost_usd, "USD")
        if provenance.cost_usd is not None
        else "incomplete",  # fmt: skip
        "review": review_line,
        "renditions": renditions,
    }


def _contact_sheet(sources: CreativeExportSources, *, images: bool) -> list[dict[str, Any]]:
    out = []
    for campaign in sources.package.campaigns:
        assets = [
            _media_asset(sources, campaign, asset, images=images)
            for asset in (*campaign.media, *campaign.logos)
        ]
        if assets:
            out.append({"title": _name(sources, campaign.campaign_ref), "assets": assets})
    return out


# ---------------------------------------------------------------------------
# landing, exceptions, summary, sign-off
# ---------------------------------------------------------------------------


def _landing(sources: CreativeExportSources, *, images: bool) -> list[dict[str, Any]]:
    out = []
    for shot in sources.landing:
        patch = shot.patch or {}
        offer = patch.get("offer_block") or None
        out.append(
            {
                "url": shot.url,
                "verdict": _words(shot.verdict),
                "ad_groups": ", ".join(shot.ad_group_refs) or EMPTY,
                "before": [
                    {
                        "device": device,
                        "h1": shot.h1_before.get(device) or EMPTY,
                        "fold": shot.fold_px.get(device),
                        **_picture(
                            sources,
                            shot.screenshots.get(device),
                            LANDING_BOX,
                            images=images,
                            max_aspect=LANDING_MAX_ASPECT,
                        ),
                    }
                    for device in DEVICES
                ],  # fmt: skip
                "after": {
                    "h1": patch.get("h1") or "unchanged",
                    "offer": (
                        f"“{offer['phrase']}” above the fold on {', '.join(offer['devices'])}"
                        if offer
                        else "unchanged"
                    ),
                    "remove_fields": ", ".join(patch.get("remove_fields") or []) or "none",
                }
                if shot.patch
                else None,
                "patch_path": shot.patch_path or EMPTY,
            }
        )
    return out


def _exceptions(sources: CreativeExportSources) -> list[dict[str, Any]]:
    return [
        {
            "id": _short(row.exception_id),
            "kind": _words(row.kind),
            "status": row.status,
            "subject": row.subject or EMPTY,
            "assets": len(row.asset_ids),
            "decided_by": sources.name(row.decided_by) or EMPTY,
            "decided_at": fmt_datetime(row.decided_at),
        }
        for row in sources.package.exceptions
    ]


def _h3(sources: CreativeExportSources) -> dict[str, Any] | None:
    stored = sources.h3
    if not stored:
        return None
    return {
        "decided_by": sources.name(stored.get("decided_by")) or EMPTY,
        "decided_at": _when(stored.get("decided_at")),
        "statement": str(stored.get("statement") or EMPTY),
        "signature_id": str(stored.get("signature_id") or EMPTY),
        "ruleset_version": str(stored.get("ruleset_version") or EMPTY),
        "set_hash": str(stored.get("decided_hash") or EMPTY),
        "register_hash": str(stored.get("register_hash") or EMPTY),
        "cleared": len(stored.get("cleared") or []),
        "rejected": len(stored.get("rejected") or []),
    }


def _summary(sources: CreativeExportSources) -> dict[str, Any]:
    package = sources.package
    cost = package.cost
    return {
        "ruleset_version": package.lint_summary.ruleset_version,
        "by_verdict": [
            (_words(item.verdict), item.count) for item in package.lint_summary.by_verdict
        ],
        "by_rule": [(item.rule_id, item.count) for item in package.lint_summary.by_rule],
        "cost": [
            ("Text", fmt_money(cost.text_usd, "USD")),
            ("Images", fmt_money(cost.image_usd, "USD")),
            ("Video", fmt_money(cost.video_usd, "USD")),
            ("Media estimate", fmt_money(cost.media_estimate_usd, "USD")),
            ("Media actual", fmt_money(cost.media_actual_usd, "USD")),
            ("Total", fmt_money(cost.total_usd, "USD")),
        ],
        "dependencies": [
            {
                "kind": _words(dep.kind),
                "task": dep.task,
                "owner": dep.owner,
                "blocking": dep.blocking_for,
                "campaigns": ", ".join(dep.campaign_refs) or "project",
            }
            for dep in package.open_dependencies
        ],
        "minimums": [
            (
                _name(sources, campaign.campaign_ref),
                "met" if campaign.launch_minimums.met else "not met",
            )
            for campaign in package.campaigns
        ],
    }


def _signoff(sources: CreativeExportSources) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for gate, method in GATE_METHODS.items():
        decisions = [d for d in sources.package.decisions if d.gate_key == gate]
        if not decisions:
            rows.append({"gate": gate, "status": "not reached", "decider": EMPTY,
                         "method": method, "at": EMPTY})  # fmt: skip
        for decision in decisions:
            role = sources.approval_roles.get(decision.approval_id)
            rows.append(
                {
                    "gate": gate,
                    "status": decision.status,
                    "decider": sources.name(decision.decided_by) or EMPTY,
                    "method": f"{method} ({role})" if role else method,
                    "at": fmt_datetime(decision.decided_at),
                }
            )
    task = sources.package.human_tasks[0] if sources.package.human_tasks else None
    stored = sources.h3 or {}
    if task is None or task.status == "not_required":
        rows.append({"gate": "H3", "status": "not required", "decider": EMPTY,
                     "method": H3_METHOD, "at": EMPTY})  # fmt: skip
    else:
        signature = stored.get("signature_id")
        rows.append(
            {
                "gate": "H3",
                "status": "decided" if task.status == "decided" else "open",
                "decider": sources.name(stored.get("decided_by")) or EMPTY,
                "method": H3_METHOD + (f", signature {signature}" if signature else ""),
                "at": _when(stored.get("decided_at")),
            }
        )
    return rows


# ---------------------------------------------------------------------------
# the context
# ---------------------------------------------------------------------------


def build_book(sources: CreativeExportSources, *, images: bool) -> dict[str, Any]:
    """Everything both templates render. `images=False` skips decoding files —
    the markdown names them rather than embedding them."""
    package = sources.package
    pins = package.pins
    return {
        "watermark": sources.watermark,
        "status": package.status,
        "status_label": _words(package.status),
        "project_name": sources.project_name or "Untitled project",
        "version": f"v{package.version}" if package.version > 0 else "draft (v0)",
        "package_id": str(package.package_id),
        "run_id": str(package.creative_run_id),
        "package_hash": package.package_hash or EMPTY,
        "brief_hash": package.brief_hash or EMPTY,
        "released_at": fmt_datetime(sources.released_at),
        "stamped": sources.stamped,
        "stamped_iso": sources.stamped.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "pins": [
            ("Plan", f"{pins.plan_id} (v{pins.plan_version})"),
            ("Ruleset", pins.ruleset_version),
            ("Creative context", pins.context_hash),
            ("Constants", pins.constants_version),
            ("Media catalogue", pins.catalogue_hash),
        ],
        "brief_markdown": demote(sources.brief_markdown) if sources.brief_markdown else "",
        "brief_html": brief_html(sources.brief_markdown) if sources.brief_markdown else "",
        "groups": _groups(sources, images=images),
        "extras": _extras(sources),
        "media": _contact_sheet(sources, images=images),
        "landing": _landing(sources, images=images),
        "exceptions": _exceptions(sources),
        "h3": _h3(sources),
        "summary": _summary(sources),
        "signoff": _signoff(sources),
    }
