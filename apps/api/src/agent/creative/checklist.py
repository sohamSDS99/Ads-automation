"""4.7.2's blocking checklist — §11's thirteen checks, as code (Stage 04 PRD §11 4.7.2, §12.4).

Every check reads the assembled `CreativePackage` and the few facts a package
does not carry (`CheckContext`): the final pin, the constants, the brief row's
approved hash, 4.6.1's measurements, the live offer rows, the project's domain
and the CRM values nothing may quote. Each returns `blocking` issues only —
the CRITIQUE model's findings are 4.7.2's, at `warning` or `note` — and names
the asset it is about, which is what release's 409 repeats.

**Nothing here is re-specified** (law 33). Counts and limits are the pin's
spec sheet; quotas are `select.quota_report`'s arithmetic; the licence is
`brief.licensed_claims` at `now`; offer figures are `offers.resolve`; domain
and uniqueness are `preview/urlcheck`; ratio, bytes and format are the
measurements 4.6.1 recorded; personal data is Stage 03's `is_personal_data`.

**At `now`.** `now` is a parameter, never read here: 4.7.2 passes the moment
it runs and release passes the moment it releases, so a claim that expired
or an offer that ended in between blocks the release (§12.4).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final, Literal

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.autofill import CRM_KINDS
from agent.creative import brief as briefs
from agent.creative import edits, offers, select
from agent.creative.conformance import media_spec
from agent.creative.constants import CreativeConstants
from agent.creative.offers import OfferBindingError
from agent.db.models import CreativeBrief, Evidence, Project
from agent.guidelines.critique import is_personal_data
from agent.orchestrator.creative_input import offer_snapshot
from agent.preview import urlcheck
from agent.schemas.creative_package import (
    CampaignCreative,
    CreativePackage,
    CritiqueIssue,
    MediaAsset,
    ResponsiveSearchAd,
    TextAsset,
)
from agent.schemas.creative_qa import SpecConformance
from agent.schemas.guardrails import SURFACE_ASSET_TYPES, AssetSpec, OfferRecord, RuleSet

#: A lint verdict an asset may ship with. `indeterminate` is not one: law 31
#: forbids a detector that could not run from producing a pass.
SHIPPABLE: Final = frozenset({"pass", "pass_with_warnings"})
#: The disclosure stamp's one required key (`postprod/image.stamp`, `video.stamp`).
STAMP_KEY: Final = "xmp_digital_source_type"
#: What 4.6.1 must have measured, and passed, on every rendition (check 9).
RENDITION_CONSTRAINTS: Final = frozenset({"ratio", "format"})
#: The host of every OpenRouter URL, and the prefix of every OpenRouter key.
OPENROUTER_HOST: Final = "openrouter.ai"
OPENROUTER_KEY_PREFIX: Final = "sk-or-"
#: A document's bytes as they would reach JSON: a data URI, a PDF or ZIP (DOCX)
#: header, or a long unbroken base64 run.
_BINARY: Final = re.compile(r"^data:|%PDF-|PK\x03\x04|[A-Za-z0-9+/]{200,}={0,2}")


@dataclass(frozen=True)
class CheckContext:
    package: CreativePackage
    #: The final pin.
    ruleset: RuleSet
    constants: CreativeConstants
    #: `CreativeBrief.approved_hash` and `.brief_hash` — the row, not the package.
    approved_hash: str | None
    brief_hash: str | None
    #: 4.6.1's output.
    conformance: SpecConformance | None
    #: The project's live `offer_record` rows (`creative_input.offer_snapshot`).
    offers: Sequence[OfferRecord]
    domain: str | None
    #: Every string value of the project's CRM evidence.
    crm_values: frozenset[str]
    now: datetime


Check = Callable[[CheckContext], list[CritiqueIssue]]


def _issue(
    check: int,
    section: str,
    finding: str,
    fix: str,
    asset_ids: Iterable[uuid.UUID] = (),
) -> CritiqueIssue:
    return CritiqueIssue(
        severity="blocking",
        section=section,
        finding=finding,
        fix=fix,
        check=f"check_{check}",
        asset_ids=sorted(set(asset_ids), key=str),
    )


def _texts(context: CheckContext) -> list[tuple[CampaignCreative, TextAsset]]:
    return [(c, t) for c in context.package.campaigns for t in c.text_assets]


def _media(context: CheckContext) -> list[tuple[CampaignCreative, MediaAsset]]:
    return [(c, m) for c in context.package.campaigns for m in (*c.media, *c.logos)]


def _specs(context: CheckContext, campaign: CampaignCreative) -> Mapping[str, AssetSpec]:
    return context.ruleset.asset_specs.for_campaign(campaign.campaign_type)


# ---------------------------------------------------------------------------
# 1 — G7 approved; package.brief_hash == approved_hash
# ---------------------------------------------------------------------------


def check_g7(context: CheckContext) -> list[CritiqueIssue]:
    package = context.package
    g7 = [d for d in package.decisions if d.gate_key == "G7"]
    issues: list[CritiqueIssue] = []
    if not g7 or g7[-1].status != "approved":
        status = g7[-1].status if g7 else "never opened"
        issues.append(
            _issue(
                1,
                "decisions",
                f"G7 is {status}, not approved.",
                "The performance owner approves the brief at G7.",
            )  # fmt: skip
        )
    if not context.approved_hash or package.brief_hash != context.approved_hash:
        issues.append(
            _issue(
                1,
                "brief_hash",
                f"The package carries brief {package.brief_hash or 'none'} but G7 approved "
                f"{context.approved_hash or 'no brief'}.",
                "Re-run from the approved brief, or approve the brief this package was built on.",
            )
        )
    elif context.brief_hash != context.approved_hash:
        issues.append(
            _issue(
                1,
                "brief_hash",
                "The brief changed after G7 approved it.",
                "A changed brief is a new G7 approval.",
            )  # fmt: skip
        )
    return issues


# ---------------------------------------------------------------------------
# 2 — every shipped asset linted at the final pin, verdict ≠ fail
# ---------------------------------------------------------------------------


def check_final_pin_lint(context: CheckContext) -> list[CritiqueIssue]:
    pin = context.ruleset.ruleset_version
    issues: list[CritiqueIssue] = []
    for _, asset in _texts(context):
        where = asset.ruleset_version or asset.lint.ruleset_version
        if asset.lint.verdict not in SHIPPABLE or where != pin or asset.lint.ruleset_version != pin:
            issues.append(_lint_issue(asset.asset_id, asset.lint.verdict, where, pin))
    for _, media in _media(context):
        for rendition in media.renditions:
            lint = rendition.lint
            if lint.verdict not in SHIPPABLE or lint.ruleset_version != pin:
                issues.append(_lint_issue(media.asset_id, lint.verdict, lint.ruleset_version, pin))
                break
    return issues


def _lint_issue(asset_id: uuid.UUID, verdict: str, at: str | None, pin: str) -> CritiqueIssue:
    return _issue(
        2,
        f"assets/{asset_id}",
        f"Asset {asset_id} is {verdict} at ruleset {at or 'none'}; the final pin is {pin}.",
        "Re-run 4.6.4 so every asset is linted at the final pin, and fix or drop what fails.",
        [asset_id],
    )


# ---------------------------------------------------------------------------
# 3 — every RSA: counts within the specs, quotas met, no flagged pair
# ---------------------------------------------------------------------------


def check_rsas(context: CheckContext) -> list[CritiqueIssue]:
    quotas = dict(context.constants.copy_.headline_quotas.value)
    issues: list[CritiqueIssue] = []
    for campaign in context.package.campaigns:
        specs = _specs(context, campaign)
        texts = {asset.asset_id: asset for asset in campaign.text_assets}
        for ad in campaign.ads:
            issues.extend(_counts(ad, specs))
            heads = [texts[a] for a in ad.headlines if a in texts]
            order = [
                select.Candidate(ref=str(a.asset_id), text=a.text or "", category=a.category or "")
                for a in heads
            ]
            report = select.quota_report(quotas, order, order, len(order))
            if not report.met:
                short = ", ".join(
                    f"{line.category} {line.selected}/{line.required}"
                    for line in report.lines
                    if line.selected < line.required
                )
                issues.append(
                    _issue(
                        3,
                        f"ads/{ad.ad_ref}",
                        f"{ad.ad_ref} misses its headline quotas: {short}.",
                        "Swap a reserve headline of the missing category into the ad.",
                        ad.headlines,
                    )  # fmt: skip
                )
            if ad.pair_report.unresolved:
                pairs = ad.pair_report.unresolved
                issues.append(
                    _issue(
                        3,
                        f"ads/{ad.ad_ref}",
                        f"{ad.ad_ref} can serve {len(pairs)} flagged pair(s) "
                        "(combinatorics.pair_flags_v1).",
                        "Swap one asset of each flagged pair for a reserve.",
                        [asset for pair in pairs for asset in pair],
                    )
                )
    return issues


def _counts(ad: ResponsiveSearchAd, specs: Mapping[str, AssetSpec]) -> list[CritiqueIssue]:
    found: list[CritiqueIssue] = []
    for surface, ids in (("rsa_headline", ad.headlines), ("rsa_description", ad.descriptions)):
        asset_type = SURFACE_ASSET_TYPES[surface]
        spec = specs.get(asset_type)
        if spec is None:
            found.append(
                _issue(
                    3,
                    f"ads/{ad.ad_ref}",
                    f"The final pin has no {asset_type} spec for {ad.campaign_ref}.",
                    "Publish a ruleset whose spec sheet covers it.",
                    ids,
                )  # fmt: skip
            )
            continue
        low, high = spec.min_count, spec.max_count
        if (low is not None and len(ids) < low) or (high is not None and len(ids) > high):
            found.append(
                _issue(
                    3,
                    f"ads/{ad.ad_ref}",
                    f"{ad.ad_ref} carries {len(ids)} {asset_type}(s); the spec allows "
                    f"{low if low is not None else 0}–{high if high is not None else '∞'}.",
                    f"Carry between {low} and {high} {asset_type}s.",
                    ids,
                )
            )
    return found


# ---------------------------------------------------------------------------
# 4 — every description: ≥ 1 claim, all licensed at the final pin at `now`
# ---------------------------------------------------------------------------


def check_claims(context: CheckContext) -> list[CritiqueIssue]:
    licensed = edits.licensed_ids(briefs.licensed_claims(context.ruleset, context.now))
    issues: list[CritiqueIssue] = []
    for _, asset in _texts(context):
        if asset.kind != "description":
            continue
        unlicensed = [str(claim) for claim in asset.claim_ids if claim not in licensed]
        if not asset.claim_ids or unlicensed:
            issues.append(
                _issue(
                    4,
                    f"assets/{asset.asset_id}",
                    (
                        f"Description {asset.asset_id} stands on no claim."
                        if not asset.claim_ids
                        else f"Description {asset.asset_id} cites claim(s) "
                        f"{', '.join(unlicensed)}, not licensed at "
                        f"{context.ruleset.ruleset_version} "
                        f"on {context.now:%Y-%m-%d}."
                    ),
                    "Drop it, or renew the claim in Stage 03.",
                    [asset.asset_id],
                )
            )
    return issues


# ---------------------------------------------------------------------------
# 5 — every variant B: distinct enough from A, and a hypothesis
# ---------------------------------------------------------------------------


def check_variant_b(context: CheckContext) -> list[CritiqueIssue]:
    minimum = context.constants.copy_.variant_min_distance.value
    issues: list[CritiqueIssue] = []
    for campaign in context.package.campaigns:
        for ad in campaign.ads:
            if ad.variant != "B":
                continue
            ids = [*ad.headlines, *ad.descriptions]
            if ad.distinctness_vs_a is None or ad.distinctness_vs_a < minimum:
                measured = (
                    "unmeasured" if ad.distinctness_vs_a is None else f"{ad.distinctness_vs_a:.2f}"
                )
                issues.append(
                    _issue(
                        5,
                        f"ads/{ad.ad_ref}",
                        f"{ad.ad_ref} is {measured} from A; "
                        f"copy.variant_min_distance is {minimum:.2f}.",
                        "Rewrite B from its own angle.",
                        ids,
                    )  # fmt: skip
                )
            if not (ad.hypothesis or "").strip():
                issues.append(
                    _issue(
                        5,
                        f"ads/{ad.ad_ref}",
                        f"{ad.ad_ref} states no hypothesis.",
                        "State the test B runs against A.",
                        ids,
                    )  # fmt: skip
                )
    return issues


# ---------------------------------------------------------------------------
# 6 — every promotion / price figure equals its live OfferRecord; window holds `now`
# ---------------------------------------------------------------------------

#: Where each bound figure is shown on a row (`n4_3_2_offer_assets`).
_SHOWN: Final[Mapping[str, Callable[[Mapping[str, Any]], Any]]] = {
    "percent_off": lambda f: (f.get("bound") or {}).get("percent_off"),
    "money_off": lambda f: (f.get("bound") or {}).get("money_off"),
    "price": lambda f: (f.get("bound") or {}).get("price"),
    "currency": lambda f: (f.get("bound") or {}).get("currency"),
    "start": lambda f: f.get("start"),
    "end": lambda f: f.get("end"),
}


def offer_drift(asset: TextAsset, records: Sequence[OfferRecord], now: datetime) -> str | None:
    """Why `asset`'s figures are not its live offer's at `now`, or None."""
    binding = asset.offer_binding
    if binding is None:
        return "carries no OfferBinding"
    observed = [record for record in records if offers.record_id(record) == binding.offer_record_id]
    if not observed:
        return f"is bound to offer {binding.sku_or_set}, which is no longer in the offer data"
    record = max(observed, key=offers.recency)
    try:
        live = offers.resolve(record, binding.fields)
    except OfferBindingError as exc:
        return f"can no longer be rendered from offer {binding.sku_or_set}: {exc}"
    moved = sorted(key for key in binding.fields if live.get(key) != binding.resolved.get(key))
    if moved:
        detail = ", ".join(f"{key} {binding.resolved.get(key)} → {live.get(key)}" for key in moved)
        return f"shows figures offer {binding.sku_or_set} no longer has ({detail})"
    shown = {key: _SHOWN[key](asset.fields) for key in binding.fields if key in _SHOWN}
    wrong = sorted(key for key, value in shown.items() if value != binding.resolved.get(key))
    if wrong:
        return f"shows {', '.join(wrong)} other than its OfferBinding renders"
    if not offers.live(record, now):
        return (
            f"is bound to offer {binding.sku_or_set}, whose window does not contain "
            f"{now:%Y-%m-%d %H:%M} UTC"
        )
    return None


def check_offers(context: CheckContext) -> list[CritiqueIssue]:
    issues: list[CritiqueIssue] = []
    for _, asset in _texts(context):
        if asset.kind not in ("promotion", "price"):
            continue
        why = offer_drift(asset, context.offers, context.now)
        if why is not None:
            issues.append(
                _issue(
                    6,
                    f"assets/{asset.asset_id}",
                    f"{asset.kind.title()} {asset.asset_id} {why}.",
                    "Re-run 4.3.2 against the current offer data.",
                    [asset.asset_id],
                )  # fmt: skip
            )
    return issues


# ---------------------------------------------------------------------------
# 7 — every sitelink: on-domain, 2xx at its check, unique in its campaign
# ---------------------------------------------------------------------------


def check_sitelinks(context: CheckContext) -> list[CritiqueIssue]:
    issues: list[CritiqueIssue] = []
    for campaign in context.package.campaigns:
        seen: dict[str, uuid.UUID] = {}
        for asset in campaign.text_assets:
            if asset.kind != "sitelink":
                continue
            check = asset.fields.get("url_check") or {}
            landed = check.get("final_url_after_redirects") or asset.fields.get("final_url") or ""
            problems: list[str] = []
            if check.get("status") != "ok":
                problems.append(f"its check was {check.get('status') or 'never run'}"
                                f" (HTTP {check.get('http_status') or '—'})")  # fmt: skip
            if not context.domain or not urlcheck.on_domain(landed, context.domain):
                problems.append(f"it lands on {landed or 'nothing'}, off {context.domain}")
            page = urlcheck.canonical(landed) if landed else ""
            if page and page in seen:
                problems.append(f"it lands on the same page as sitelink {seen[page]}")
            elif page:
                seen[page] = asset.asset_id
            if problems:
                issues.append(
                    _issue(
                        7,
                        f"assets/{asset.asset_id}",
                        f"Sitelink {asset.asset_id}: {'; '.join(problems)}.",
                        "Point it at a distinct on-domain page that answers 2xx.",
                        [asset.asset_id],
                    )  # fmt: skip
                )
    return issues


# ---------------------------------------------------------------------------
# 8 — every AI media asset: approved at G8/G8b, stamped, fully provenanced
# ---------------------------------------------------------------------------


def check_ai_media(context: CheckContext) -> list[CritiqueIssue]:
    issues: list[CritiqueIssue] = []
    refused = [
        d.gate_key for d in context.package.decisions
        if d.gate_key in ("G8", "G8b") and d.status in ("rejected", "expired")
    ]  # fmt: skip
    for gate in refused:
        issues.append(
            _issue(
                8,
                "decisions",
                f"Gate {gate} was not approved.",
                "The brand owner decides every AI asset at G8 and G8b.",
            )  # fmt: skip
        )
    for _, media in _media(context):
        if not media.generated_by_ai or media.modality == "logo":
            continue
        problems: list[str] = []
        review = media.review
        if review.decision != "approve" or review.gate not in ("G8", "G8b"):
            problems.append(f"its review is {review.decision or 'missing'} "
                            f"at {review.gate or 'no gate'}")  # fmt: skip
        if not media.renditions or any(
            not (r.disclosure or {}).get(STAMP_KEY) for r in media.renditions
        ):
            problems.append("a file carries no disclosure stamp")
        provenance = media.provenance
        if not provenance.model_id or not provenance.job_ids or provenance.cost_usd is None:
            missing = [
                name for name, value in (("model", provenance.model_id),
                                         ("job id", provenance.job_ids),
                                         ("cost", provenance.cost_usd))
                if value in (None, "", [])
            ]  # fmt: skip
            problems.append(f"its provenance has no {', '.join(missing)}")
        if problems:
            issues.append(
                _issue(
                    8,
                    f"media/{media.asset_id}",
                    f"AI {media.modality} {media.asset_id}: {'; '.join(problems)}.",
                    "Review it at G8, and re-run post-production so it is stamped.",
                    [media.asset_id],
                )  # fmt: skip
            )
    return issues


# ---------------------------------------------------------------------------
# 9 — every rendition: sx == sy; ratio, bytes and format as 4.6.1 measured them
# ---------------------------------------------------------------------------


def check_renditions(context: CheckContext) -> list[CritiqueIssue]:
    measured = context.conformance
    checks: dict[uuid.UUID, list[Any]] = {}
    unchecked: dict[uuid.UUID, list[str]] = {}
    if measured is not None:
        for check in measured.checks:
            if check.media_id is not None:
                checks.setdefault(check.media_id, []).append(check)
        for item in measured.unchecked:
            if item.media_id is not None:
                unchecked.setdefault(item.media_id, []).append(item.reason)
    issues: list[CritiqueIssue] = []
    for _, media in _media(context):
        problems: list[str] = []
        for rendition in media.renditions:
            sx, sy = rendition.scale
            if sx != sy:
                problems.append(f"{rendition.media_id} is scaled {sx}×{sy}")
            ours = checks.get(rendition.media_id, [])
            failed = sorted({c.constraint for c in ours if c.verdict != "pass"})
            missing = sorted(RENDITION_CONSTRAINTS - {c.constraint for c in ours})
            if failed:
                problems.append(f"{rendition.media_id} fails {', '.join(failed)}")
            if missing or rendition.media_id in unchecked:
                why = unchecked.get(rendition.media_id) or [f"no {', '.join(missing)} measured"]
                problems.append(f"{rendition.media_id} was not measured ({', '.join(why)})")
        if problems:
            issues.append(
                _issue(
                    9,
                    f"media/{media.asset_id}",
                    f"{media.modality.title()} {media.asset_id}: {'; '.join(problems)}.",
                    "Re-render the file to the spec and re-run 4.6.1.",
                    [media.asset_id],
                )  # fmt: skip
            )
    return issues


# ---------------------------------------------------------------------------
# 10 — every video: brand early, captions burned and readable, duration in spec
# ---------------------------------------------------------------------------


def check_videos(context: CheckContext) -> list[CritiqueIssue]:
    within = context.constants.video.brand_within_ms.value
    threshold = context.constants.video.caption_ocr_min_similarity.value
    tolerance = context.constants.media.ratio_tolerance.value
    issues: list[CritiqueIssue] = []
    for campaign, media in _media(context):
        if media.modality != "video":
            continue
        specs = _specs(context, campaign)
        problems: list[str] = []
        for rendition in media.renditions:
            facts = rendition.video
            if facts is None:
                problems.append(f"{rendition.media_id} has no verification")
                continue
            if facts.brand_first_at_ms > within:
                problems.append(f"the brand first shows at {facts.brand_first_at_ms} ms "
                                f"(within {within} ms)")  # fmt: skip
            if not facts.captions_burned:
                problems.append("its captions are not burned in")
            ocr = facts.caption_ocr_min_similarity
            if ocr is None or ocr < threshold:
                problems.append(f"its captions read at {ocr if ocr is not None else 'no'} "
                                f"similarity (at least {threshold})")  # fmt: skip
            found = media_spec(specs, rendition.aspect_ratio, kind="video", tolerance=tolerance)
            seconds = (rendition.duration_ms or facts.duration_ms) / 1000
            if found is None:
                problems.append(f"no video spec for {rendition.aspect_ratio}")
            else:
                _, spec = found
                if (spec.min_duration_s is not None and seconds < spec.min_duration_s) or (
                    spec.max_duration_s is not None and seconds > spec.max_duration_s
                ):
                    problems.append(
                        f"it runs {seconds:g} s; the spec allows "
                        f"{spec.min_duration_s or 0}–{spec.max_duration_s or '∞'} s"
                    )
        if problems:
            issues.append(
                _issue(
                    10,
                    f"media/{media.asset_id}",
                    f"Video {media.asset_id}: {'; '.join(problems)}.",
                    "Re-cut the video in post-production and re-verify it.",
                    [media.asset_id],
                )  # fmt: skip
            )
    return issues


# ---------------------------------------------------------------------------
# 11 — every campaign meets its launch minimums, or names what blocks launch
# ---------------------------------------------------------------------------


def check_launch_minimums(context: CheckContext) -> list[CritiqueIssue]:
    issues: list[CritiqueIssue] = []
    for campaign in context.package.campaigns:
        minimum = campaign.launch_minimums
        if minimum.met:
            continue
        named = [
            d for d in context.package.open_dependencies
            if d.blocking_for == "launch" and campaign.campaign_ref in d.campaign_refs
        ]  # fmt: skip
        if named:
            continue
        short = ", ".join(
            f"{line.asset_type} {line.present}/{line.required}"
            for line in minimum.required
            if not line.met
        )
        issues.append(
            _issue(
                11,
                f"campaigns/{campaign.campaign_ref}",
                f"{campaign.campaign_ref} is short of its launch minimum: {short}, and no "
                "dependency says what blocks its launch.",
                "Produce the missing assets, or record the dependency that blocks launch.",
            )  # fmt: skip
        )
    return issues


# ---------------------------------------------------------------------------
# 12 — H3 completed or not required; nothing waits on an open exception
# ---------------------------------------------------------------------------


def check_h3(context: CheckContext) -> list[CritiqueIssue]:
    package = context.package
    issues: list[CritiqueIssue] = []
    h3 = [task for task in package.human_tasks if task.task_key == "H3"]
    done = (
        bool(h3)
        and h3[-1].status in ("not_required", "decided")
        and (h3[-1].task_status in (None, "completed", "not_required"))
    )
    if not done:
        status = f"{h3[-1].status} ({h3[-1].task_status or 'no task'})" if h3 else "unknown"
        issues.append(
            _issue(
                12,
                "human_tasks",
                f"H3 is {status}, not completed or not required.",
                "The named legal owner clears or rejects the exceptions at H3.",
            )  # fmt: skip
        )
    shipped = {a.asset_id for c in package.campaigns for a in c.text_assets} | {
        m.asset_id for c in package.campaigns for m in (*c.media, *c.logos)
    }
    for exception in package.exceptions:
        if exception.status != "open":
            continue
        tied = [asset for asset in exception.asset_ids if asset in shipped]
        if tied:
            issues.append(
                _issue(
                    12,
                    f"exceptions/{exception.exception_id}",
                    f"{len(tied)} shipped asset(s) wait on open exception "
                    f"{exception.exception_id} ({exception.subject or exception.kind}).",
                    "Clear or reject the exception at H3, or withdraw it.",
                    tied,
                )  # fmt: skip
            )
    return issues


# ---------------------------------------------------------------------------
# 13 — nothing private or secret anywhere in the payload
# ---------------------------------------------------------------------------


#: An id inside a longer string — a manifest path is `media/{asset}/{media}.jpg`.
#: `redact_pii`'s phone pattern reads a UUID's digit groups as a number, so
#: ids are taken out before the scan, exactly as Stage 03 skips a bare one.
_UUID: Final = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE
)


def leak(value: str, crm_values: frozenset[str]) -> str | None:
    """What `value` gives away, or None."""
    if is_personal_data(_UUID.sub("id", value)):
        return "personal data (an email, phone number or postal address)"
    if value.strip() in crm_values:
        return "a raw CRM value"
    lowered = value.lower()
    if OPENROUTER_HOST in lowered:
        return "an OpenRouter URL"
    if OPENROUTER_KEY_PREFIX in lowered:
        return "an OpenRouter key"
    if _BINARY.search(value):
        return "a binary (a document's bytes)"
    return None


def check_payload(context: CheckContext) -> list[CritiqueIssue]:
    found: dict[str, set[uuid.UUID]] = {}

    def walk(node: Any, owner: uuid.UUID | None) -> None:
        if isinstance(node, Mapping):
            here = node.get("asset_id")
            mine = uuid.UUID(here) if isinstance(here, str) else owner
            for value in node.values():
                walk(value, mine)
        elif isinstance(node, list | tuple):
            for item in node:
                walk(item, owner)
        elif isinstance(node, str) and node:
            what = leak(node, context.crm_values)
            if what is not None:
                bucket = found.setdefault(what, set())
                if owner is not None:
                    bucket.add(owner)

    walk(context.package.model_dump(mode="json"), None)
    return [
        _issue(
            13,
            "payload",
            f"The payload holds {what}.",
            "Remove it from the copy or field that carries it; nothing private ships.",
            assets,
        )  # fmt: skip
        for what, assets in sorted(found.items())
    ]


# ---------------------------------------------------------------------------
# the checklist
# ---------------------------------------------------------------------------

CHECKS: Final[tuple[Check, ...]] = (
    check_g7,
    check_final_pin_lint,
    check_rsas,
    check_claims,
    check_variant_b,
    check_offers,
    check_sitelinks,
    check_ai_media,
    check_renditions,
    check_videos,
    check_launch_minimums,
    check_h3,
    check_payload,
)


def run_checks(context: CheckContext) -> list[CritiqueIssue]:
    """All thirteen, in §11's order."""
    return [issue for check in CHECKS for issue in check(context)]


def blocking(issues: Sequence[CritiqueIssue]) -> list[CritiqueIssue]:
    return [issue for issue in issues if issue.severity == "blocking"]


def status_for(issues: Sequence[CritiqueIssue]) -> Literal["blocked", "ready_to_release"]:
    """Any blocking issue ⇒ blocked (§11 4.7.2)."""
    return "blocked" if blocking(issues) else "ready_to_release"


# ---------------------------------------------------------------------------
# reading the context — the one function here that does I/O
# ---------------------------------------------------------------------------

#: A CRM value shorter than this is a word, not a record ("Won", "US").
CRM_MIN_CHARS: Final = 4


async def crm_values(db: AsyncSession, project_id: uuid.UUID) -> frozenset[str]:
    """Every string a CRM row of the project holds — what check 13 must not find."""
    payloads = (
        await db.execute(
            sa.select(Evidence.payload).where(
                Evidence.project_id == project_id, Evidence.kind.in_(CRM_KINDS)
            )
        )
    ).scalars()
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, Mapping):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and len(node.strip()) >= CRM_MIN_CHARS:
            found.add(node.strip())

    for payload in payloads:
        walk(payload)
    return frozenset(found)


async def read_context(
    db: AsyncSession,
    *,
    package: CreativePackage,
    project: Project,
    ruleset: RuleSet,
    constants: CreativeConstants,
    outputs: Mapping[str, Any],
    now: datetime,
) -> CheckContext:
    """What the checklist needs beyond the package, read at `now` — live rows only."""
    brief = (
        await db.execute(
            sa.select(CreativeBrief)
            .where(CreativeBrief.creative_run_id == package.creative_run_id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    raw = outputs.get("4.6.1")
    return CheckContext(
        package=package,
        ruleset=ruleset,
        constants=constants,
        approved_hash=brief.approved_hash if brief is not None else None,
        brief_hash=brief.brief_hash if brief is not None else None,
        conformance=SpecConformance.model_validate(raw) if raw else None,
        offers=tuple((await offer_snapshot(db, project.id)).records),
        domain=project.domain,
        crm_values=await crm_values(db, project.id),
        now=now,
    )
