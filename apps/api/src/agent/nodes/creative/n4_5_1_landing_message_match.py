"""4.5.1 `landing_message_match` — does the page headline echo the ad? (PRD §11 4.5, §10.2).

For every distinct `landing_url` of the approved brief's Search ad groups:

1. **Render** it through `preview/landing.py` at both viewports — GETs only,
   no consent clicked, no form submitted, concurrency 1 (law 41, §13). The
   screenshots go through `StorageBackend`; the facts become
   `Evidence(source='web', kind='landing_render' | 'landing_dom')`.
2. **Score** each device's H1 against the final A headlines 4.2.3 left each
   ad group pointing there, by `match.token_trigram_v1`; the page scores its
   weakest (ad group, device), recorded as `derived` / `metric_message_match`.
3. **Propose** an H1 when the score is below `landing.message_match_min`:
   COPYWRITE writes a few, each is linted at the run's pin as
   `landing_page_section` copy and scored by code, and only one that passes
   lint *and* the threshold is proposed (law 33). None is a recorded note,
   not a failure.

Rendering happens in `gather()`, because every Evidence row the output cites
must be one the node gathered. Nothing is written to `landing_page_audit`
until every page is judged; each row is then set absolutely (one per URL,
stale ones removed), so an attempt after a failed one overwrites it.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel, ConfigDict, Field

from agent.creative import brief as briefs
from agent.creative import landing_audit as audit
from agent.creative.metrics import MESSAGE_MATCH_V1
from agent.db.models import (
    Evidence,
    EvidenceSource,
    LandingAuditVerdict,
    LandingPageAudit,
    RunStage,
)
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.store import EvidenceStore
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative._ad_groups import Slot, proof_points, render_prompt, search_slots
from agent.nodes.creative._text_assets import lint_ref
from agent.preview import landing
from agent.preview.landing import DEVICES, LandingRender
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.guardrails import LintResult, LintTarget
from agent.schemas.landing import (
    Device,
    DeviceFlag,
    DevicePx,
    DeviceText,
    LandingMessageMatchOutput,
    LandingPageMatch,
    MessageMatch,
    ProposedH1,
    Screenshots,
)
from agent.schemas.search_ads import CombinationCoherenceOutput, HeadlineSpreadOutput
from agent.storage import get_storage

NODE_ID = "4.5.1"
#: Where a landing-page headline is linted (Stage 03 §12.2's surfaces).
SURFACE = "landing_page_section"
#: How many alternatives one COPYWRITE call proposes. One call per failing
#: page, never a second: the loop is bounded by the number of pages.
PROPOSALS = 3
METRIC_KIND = "metric_message_match"
_SEVERITY = {"pass": 0, "pass_with_warnings": 1, "fail": 2}

SYSTEM = """You rewrite the main headline (H1) of a landing page so that it says what the
ads linking to it say. A visitor who clicked one of the ads must recognise the
ad's promise in the page's first line.

Write {n} alternative H1s. Each one:
- repeats the words of the ad headlines below, as a reader would recognise them;
- is plain and specific, in the language given, with no exclamation mark;
- states no fact that is not in PROOF, and no number, price, date or discount;
- never uses a word listed in NEVER.
"""


class _H1Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=150)


class H1ProposalsDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidates: list[_H1Draft] = Field(min_length=1, max_length=PROPOSALS)


@dataclass(frozen=True, slots=True)
class PagePlan:
    """One distinct landing URL and the ad groups whose ads point at it."""

    url: str
    slots: tuple[Slot, ...]
    groups: tuple[audit.AdGroupHeadlines, ...]


@dataclass(frozen=True, slots=True)
class Rendered:
    plan: PagePlan
    render: LandingRender
    screenshots: Screenshots
    match: audit.PageMatch
    metric_id: uuid.UUID
    evidence_ids: tuple[uuid.UUID, ...]


class LandingMessageMatchNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="landing_message_match",
        stage="4.5",
        run_stage=RunStage.CREATIVE,
        depends_on=("4.2.3",),
        task_class=TaskClass.COPYWRITE,
        input_model=CombinationCoherenceOutput,
        output_model=LandingMessageMatchOutput,
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        creative = ctx.require_creative()
        constants = creative.constants.landing
        plans = pages(ctx)
        if not plans:
            ctx.scratch[NODE_ID] = []
            return []
        viewports: dict[Device, landing.Viewport] = {
            "mobile": landing.parse_viewport(constants.viewport_mobile.value),
            "desktop": landing.parse_viewport(constants.viewport_desktop.value),
        }
        renders = await landing.render_pages([plan.url for plan in plans], viewports=viewports)
        store = EvidenceStore(ctx.db, ctx.run.workspace_id)
        threshold = constants.message_match_min.value
        rendered: list[Rendered] = []
        for plan, render in zip(plans, renders, strict=True):
            screenshots = await _store_screenshots(ctx, render)
            facts = await store.write(
                landing.evidence_drafts(
                    render, {"mobile": screenshots.mobile, "desktop": screenshots.desktop}
                ),
                project_id=ctx.project.id,
                run_id=ctx.run.id,
            )
            match = audit.page_match(render, plan.groups, threshold)
            metric = await store.write(
                [_metric_draft(plan.url, match, threshold)],
                project_id=ctx.project.id,
                run_id=ctx.run.id,
            )
            (metric_id,) = metric.evidence_ids
            rendered.append(
                Rendered(
                    plan=plan,
                    render=render,
                    screenshots=screenshots,
                    match=match,
                    metric_id=metric_id,
                    evidence_ids=tuple(dict.fromkeys(facts.evidence_ids)),
                )
            )
            await ctx.progress(f"{plan.url}: {_describe(match, threshold)}")
        ctx.scratch[NODE_ID] = rendered
        ids = {item for page in rendered for item in (*page.evidence_ids, page.metric_id)}
        return list(
            (await ctx.db.execute(sa.select(Evidence).where(Evidence.id.in_(ids)))).scalars().all()
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        threshold = creative.constants.landing.message_match_min.value
        rendered: Sequence[Rendered] | None = ctx.scratch.get(NODE_ID)
        if rendered is None:
            raise NodeContractError("4.5.1 reasoned without the renders its gather() made")
        brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
        now = ctx.run.started_at or datetime.now(UTC)
        existing = await _existing_rows(ctx)
        judged: list[tuple[LandingPageMatch, dict[str, Any]]] = []
        for page in rendered:
            proposal: ProposedH1 | None = None
            note: str | None = None
            lint_result: LintResult | None = None
            if page.match.verdict == "fail":
                proposal, lint_result, note = await _propose(
                    ctx, brief, page, threshold=threshold, now=now
                )
            row = existing.get(page.plan.url)
            judged.append(
                (
                    _page(page, row.id if row else uuid.uuid4(), threshold, proposal, note),
                    {"proposed_h1_lint": lint_result.model_dump(mode="json")}
                    if lint_result is not None
                    else {},
                )
            )
        await _write(ctx, judged, existing)
        return LandingMessageMatchOutput(pages=[page for page, _ in judged])


def pages(ctx: RunContext) -> list[PagePlan]:
    """The distinct landing URLs of the brief's Search ad groups, with each
    ad group's final A headlines (4.2.3's selection, by default text)."""
    creative = ctx.require_creative()
    brief = CreativeBrief.model_validate(ctx.output_of("4.1.1"))
    spread = HeadlineSpreadOutput.model_validate(ctx.output_of("4.2.1"))
    coherence = CombinationCoherenceOutput.model_validate(ctx.output_of("4.2.3"))
    slots = {
        (slot.brief.campaign_ref, slot.brief.ad_group_ref): slot
        for slot in search_slots(brief, creative.input.account_structure.campaigns)
    }
    texts = {
        candidate.asset_id: candidate.default_text
        for group in spread.ad_groups
        for candidate in group.candidates
    }
    by_url: dict[str, list[tuple[Slot, audit.AdGroupHeadlines]]] = {}
    for ad in coherence.ads:
        if ad.variant != "A":
            continue
        slot = slots.get((ad.campaign_ref, ad.ad_group_ref))
        if slot is None:
            raise NodeContractError(
                f"4.2.3 judged {ad.campaign_ref} / {ad.ad_group_ref}, which the approved brief "
                "does not brief as a Search ad group"
            )
        missing = [str(asset_id) for asset_id in ad.headlines if asset_id not in texts]
        if missing:
            raise NodeContractError(
                f"4.2.3 selected headline(s) 4.2.1 never wrote: {', '.join(missing)}"
            )
        by_url.setdefault(str(slot.brief.landing_url), []).append(
            (
                slot,
                audit.AdGroupHeadlines(
                    campaign_ref=ad.campaign_ref,
                    ad_group_ref=ad.ad_group_ref,
                    headlines=tuple(texts[asset_id] for asset_id in ad.headlines),
                ),
            )
        )
    return [
        PagePlan(
            url=url,
            slots=tuple(slot for slot, _ in items),
            groups=tuple(group for _, group in items),
        )
        for url, items in by_url.items()
    ]


async def _store_screenshots(ctx: RunContext, render: LandingRender) -> Screenshots:
    """Each device's full-page PNG through `StorageBackend`, keyed by run and URL."""
    storage = get_storage()
    digest = hashlib.sha256(render.url.encode()).hexdigest()[:16]
    keys: dict[str, str | None] = {}
    for device in DEVICES:
        shot = render.device(device).screenshot
        if shot is None:
            keys[device] = None
            continue
        key = f"creative/{ctx.run.id}/landing/{digest}/{device}.png"
        await asyncio.to_thread(storage.put, key, shot, content_type="image/png")
        keys[device] = key
    return Screenshots.model_validate(keys)


def _metric_draft(url: str, match: audit.PageMatch, threshold: float) -> EvidenceDraft:
    return EvidenceDraft(
        source=EvidenceSource.DERIVED,
        kind=METRIC_KIND,
        source_url=url,
        payload={
            "metric": MESSAGE_MATCH_V1,
            "url": url,
            "threshold": threshold,
            "score": float(match.score) if match.score is not None else None,
            "verdict": match.verdict,
            "scores": [score.model_dump(mode="json") for score in match.scores],
        },
        content_text=f"{MESSAGE_MATCH_V1} for {url}: {_describe(match, threshold)}.",
    )


def _describe(match: audit.PageMatch, threshold: float) -> str:
    if match.score is None:
        return "not rendered on either device, so no match was measured"
    relation = ">=" if match.verdict == "pass" else "<"
    return f"H1 match {float(match.score):.2f} {relation} {threshold:.2f} ({match.verdict})"


async def _propose(
    ctx: RunContext,
    brief: CreativeBrief,
    page: Rendered,
    *,
    threshold: float,
    now: datetime,
) -> tuple[ProposedH1 | None, LintResult | None, str | None]:
    """One COPYWRITE call, every candidate linted and scored, the best eligible kept."""
    creative = ctx.require_creative()
    # The claims the pin licenses now — the only facts copy may state (law 34).
    licensed = briefs.licensed_claims(creative.linter.ruleset, now)
    draft = await ctx.complete(
        H1ProposalsDraft,
        system=SYSTEM.format(n=PROPOSALS),
        user=render_prompt(
            {
                "ADS": [
                    {
                        "campaign": group.campaign_ref,
                        "ad_group": group.ad_group_ref,
                        "primary_message": slot.brief.primary_message.text,
                        "headlines": list(group.headlines),
                    }
                    for slot, group in zip(page.plan.slots, page.plan.groups, strict=True)
                ],
                "CURRENT_H1": {device: page.render.device(device).h1 for device in DEVICES},
                "LANGUAGE": sorted({slot.language for slot in page.plan.slots}),
                "PROOF": proof_points(licensed),
                "VOICE": list(brief.non_negotiables.voice_words),
                "NEVER": list(brief.non_negotiables.never_terms),
            }
        ),
        task_class=TaskClass.COPYWRITE,
    )
    contexts = list(
        dict.fromkeys((slot.campaign_type, slot.market, slot.language) for slot in page.plan.slots)
    )
    candidates: list[audit.H1Candidate] = []
    for index, item in enumerate(draft.candidates[:PROPOSALS]):
        text = " ".join(item.text.split())
        if not text:
            continue
        results = [
            creative.linter.lint_candidate(
                LintTarget(
                    ref=f"{NODE_ID}:h1:{index}",
                    surface=SURFACE,
                    campaign_type=campaign_type,
                    market=market,
                    language=language,
                    text=text,
                    generated_by_ai=True,
                ),
                now=now,
            )
            for campaign_type, market, language in contexts
        ]
        # Every market the page serves must pass: the worst verdict stands.
        worst = max(results, key=lambda result: _SEVERITY[result.verdict])
        candidates.append(
            audit.H1Candidate(
                text=text, score=audit.proposal_score(page.plan.groups, text), lint=worst
            )
        )
    chosen = audit.choose_h1(candidates, threshold)
    if chosen is None:
        linted = [c for c in candidates if c.lint.verdict != "fail"]
        note = (
            f"None of the {len(candidates)} proposed H1s passed lint at pin {creative.linter.pin}."
            if not linted
            else f"None of the {len(linted)} proposed H1s that passed lint reaches "
            f"{threshold:.2f}; the best scores {float(max(c.score for c in linted)):.2f}."
        )
        return None, None, note
    return (
        ProposedH1(text=chosen.text, score=float(chosen.score), lint=lint_ref(chosen.lint)),
        chosen.lint,
        None,
    )


def _page(
    page: Rendered,
    audit_id: uuid.UUID,
    threshold: float,
    proposal: ProposedH1 | None,
    note: str | None,
) -> LandingPageMatch:
    render = page.render
    shown = render.desktop if render.desktop.reached else render.mobile
    return LandingPageMatch(
        audit_id=audit_id,
        url=page.plan.url,
        final_url=shown.final_url,
        http_status=shown.http_status,
        reachable=render.mobile.ok and render.desktop.ok,
        ad_group_refs=list(dict.fromkeys(group.ad_group_ref for group in page.plan.groups)),
        h1=DeviceText(mobile=render.mobile.h1, desktop=render.desktop.h1),
        message_match=MessageMatch(
            score=float(page.match.score) if page.match.score is not None else None,
            threshold=threshold,
            verdict=page.match.verdict,
            scores=list(page.match.scores),
            evidence_ids=[page.metric_id],
        ),
        proposed_h1=proposal,
        proposed_h1_note=note,
        screenshots=page.screenshots,
        fold_px=DevicePx(mobile=render.mobile.fold_px, desktop=render.desktop.fold_px),
        obscured_by_overlay=DeviceFlag(
            mobile=render.mobile.obscured_by_overlay, desktop=render.desktop.obscured_by_overlay
        ),
        evidence_ids=list(page.evidence_ids),
    )


def preliminary_verdict(page: LandingPageMatch) -> LandingAuditVerdict:
    """What 4.5.1 alone can say; 4.5.2 replaces it with the page's verdict."""
    if not page.reachable:
        return LandingAuditVerdict.UNREACHABLE
    if page.message_match.verdict == "fail":
        return LandingAuditVerdict.NEEDS_CHANGE
    return LandingAuditVerdict.OK


async def _existing_rows(ctx: RunContext) -> dict[str, LandingPageAudit]:
    rows = (
        (
            await ctx.db.execute(
                sa.select(LandingPageAudit).where(LandingPageAudit.creative_run_id == ctx.run.id)
            )
        )
        .scalars()
        .all()
    )
    return {row.url: row for row in rows}


async def _write(
    ctx: RunContext,
    judged: Sequence[tuple[LandingPageMatch, dict[str, Any]]],
    existing: dict[str, LandingPageAudit],
) -> None:
    """One row per URL, set absolutely; a URL this attempt did not audit is removed."""
    keep = {page.url for page, _ in judged}
    for url, stale in existing.items():
        if url not in keep:
            await ctx.db.delete(stale)
    for page, extra in judged:
        row = existing.get(page.url)
        if row is None:
            row = LandingPageAudit(id=page.audit_id, creative_run_id=ctx.run.id, url=page.url)
            ctx.db.add(row)
        row.final_url = page.final_url
        row.http_status = page.http_status
        row.ad_group_refs = list(page.ad_group_refs)
        row.metrics = {
            "h1": page.h1.model_dump(mode="json"),
            "fold_px": page.fold_px.model_dump(mode="json"),
            "obscured_by_overlay": page.obscured_by_overlay.model_dump(mode="json"),
            "message_match": page.message_match.model_dump(mode="json"),
            "proposed_h1": page.proposed_h1.model_dump(mode="json") if page.proposed_h1 else None,
            "proposed_h1_note": page.proposed_h1_note,
            **extra,
        }
        row.patch = None
        row.verdict = preliminary_verdict(page)
        row.screenshots = page.screenshots.model_dump(mode="json")
        row.evidence_ids = [*page.evidence_ids, *page.message_match.evidence_ids]
    await ctx.db.flush()


LANDING_MESSAGE_MATCH = LandingMessageMatchNode()
