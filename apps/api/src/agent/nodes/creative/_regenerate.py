"""Regenerate one AI-made asset through the 4.4.2–4.4.4 pipeline (Stage 04 PRD §11 4.4.6).

Used by node 4.4.6 for G8's `regenerate` items (round 2), and by an operator's
`POST /creative-assets/{id}/regenerate` before G8 is decided (round 1). Both go
through the nodes' own code — `_Mastering` then `_Rendering` for an image,
`_Production` (script, clips, post-production) for a video — so a regenerated
asset is held to everything the first generation was: submit once (Law 37),
budget reserved first (Law 43), lint at creation (Law 33), relay out, never
stretch (Law 39), marks placed by code (Law 38).

A regeneration writes a **new** asset — `lineage = {origin: regenerated,
parent_id, by_user, node_id: 4.4.6}` — rather than repainting the old one in
place: the files a reviewer looked at stay exactly what they were, and the
Media Library's lineage tree has both. The new asset records, in
`fields.regeneration`, the model it was made with (§9.2: "recorded in
provenance"), and `fields.state` says where it is:

- `running` — made, being produced. A G8 decision waits for it.
- `done`    — produced; `awaiting_review`.
- `gap`     — nothing to look at (every candidate failed lint, the budget
  refused, the model failed…). It stays `draft`: nothing unlinted leaves draft
  (Law 33), and a failed attempt is not emitted.

The nodes' own leading underscore keeps `registry.discover()` from walking this module.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.calc.derived import DerivedWriter
from agent.calc.media import cost_estimate_v1
from agent.creative.review import DONE, GAP, REGENERATION_NODE, RUNNING
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Run,
)
from agent.evidence.store import EvidenceStore
from agent.media.types import CapabilityRecord
from agent.nodes.base import NodeContractError, RunContext
from agent.nodes.creative import n4_4_4_video_production as n444
from agent.nodes.creative.n4_4_2_image_masters import _Mastering
from agent.nodes.creative.n4_4_3_image_renditions import _Rendering
from agent.orchestrator.creative_input import estimate_inputs_for, ratios_for
from agent.schemas.creative_input import CreativeInput, MediaModelChoice
from agent.schemas.creative_media import CampaignConcepts, Concept, CreativeConcepts
from agent.schemas.creative_review import RegeneratedAsset
from agent.schemas.creative_video import VideoGap

log = structlog.get_logger(__name__)

CONCEPTS_NODE = "4.4.1"

MEDIA_KINDS = frozenset({CreativeAssetKind.IMAGE, CreativeAssetKind.VIDEO})


@dataclass(frozen=True, slots=True)
class RegenerationRequest:
    parent_id: uuid.UUID
    choice: MediaModelChoice
    note: str
    by_user: uuid.UUID | None
    #: 1: an operator's, before G8. 2: G8's regeneration round.
    round: int

    def provenance(self) -> dict[str, Any]:
        return {
            "note": self.note,
            "requested_by": str(self.by_user) if self.by_user else None,
            "round": self.round,
            "choice": self.choice.model_dump(mode="json"),
        }

    @classmethod
    def of(cls, child: CreativeAsset) -> RegenerationRequest:
        """What `open_child` recorded on the new asset — read back by the worker."""
        fields = child.fields or {}
        recorded = fields.get("regeneration") or {}
        by = recorded.get("requested_by")
        return cls(
            parent_id=uuid.UUID(str(fields["regenerated_from"])),
            choice=MediaModelChoice.model_validate(recorded["choice"]),
            note=str(recorded.get("note") or ""),
            by_user=uuid.UUID(str(by)) if by else None,
            round=int(recorded.get("round") or 1),
        )


async def open_child(
    sessions: async_sessionmaker[AsyncSession], parent: CreativeAsset, request: RegenerationRequest
) -> uuid.UUID:
    """The regenerated asset for `parent`, committed on its own session before
    any submit — `generation_job.asset_id` references it from the job layer's
    own transaction (Law 37) — and found again, not duplicated, on a resume.
    An earlier attempt that ended in a gap is not reused: asking again is a
    new attempt."""
    async with sessions() as session:
        existing = await session.scalar(
            sa.select(CreativeAsset.id).where(
                CreativeAsset.creative_run_id == parent.creative_run_id,
                CreativeAsset.node_id == REGENERATION_NODE,
                CreativeAsset.fields["regenerated_from"].astext == str(parent.id),
                CreativeAsset.fields["regeneration"]["round"].astext == str(request.round),
                CreativeAsset.fields["state"].astext != GAP,
            )
        )
        if existing is not None:
            return existing
        parent_fields = parent.fields or {}
        row = CreativeAsset(
            workspace_id=parent.workspace_id,
            project_id=parent.project_id,
            creative_run_id=parent.creative_run_id,
            node_id=REGENERATION_NODE,
            campaign_ref=parent.campaign_ref,
            ad_group_ref=parent.ad_group_ref,
            kind=parent.kind,
            surface=parent.surface,
            generated_by_ai=True,
            status=CreativeAssetStatus.DRAFT,
            fields={
                key: parent_fields[key]
                for key in ("concept_id", "product_depiction")
                if key in parent_fields
            }
            | {
                "regenerated_from": str(parent.id),
                "regeneration": request.provenance(),
                "state": RUNNING,
            },
            lineage={
                "origin": "regenerated",
                "parent_id": str(parent.id),
                "by_user": str(request.by_user) if request.by_user else None,
                "node_id": REGENERATION_NODE,
            },
            content_hash=parent.content_hash,
        )
        session.add(row)
        await session.commit()
        return row.id


async def regenerate(
    ctx: RunContext, parent: CreativeAsset, child: CreativeAsset, request: RegenerationRequest
) -> RegeneratedAsset:
    """Produce `child` through the pipeline, then leave it `awaiting_review`
    (something to look at) or `draft` with `state=gap` (nothing to look at)."""
    if parent.kind is CreativeAssetKind.IMAGE:
        made = await _image(ctx, parent, child, request)
    elif parent.kind is CreativeAssetKind.VIDEO:
        made = await _video(ctx, parent, child, request)
    else:  # pragma: no cover — callers accept media only
        raise NodeContractError(f"asset {parent.id} is {parent.kind.value}, not media")
    fresh = await ctx.db.get(CreativeAsset, child.id, populate_existing=True)
    if fresh is None:  # pragma: no cover — committed by open_child
        raise NodeContractError(f"regenerated asset {child.id} vanished")
    fields = dict(fresh.fields or {})
    if made.status == "regenerated":
        fields["state"] = DONE
        fresh.status = CreativeAssetStatus.AWAITING_REVIEW
    else:
        fields["state"] = GAP
        fields["gap"] = made.gap
        fresh.status = CreativeAssetStatus.DRAFT
    fields["finished_at"] = datetime.now(UTC).isoformat()
    fresh.fields = fields
    await ctx.db.flush()
    log.info(
        "creative.regenerated",
        run_id=str(ctx.run.id),
        parent=str(parent.id),
        asset=str(child.id),
        round=request.round,
        model=request.choice.model_id,
        status=made.status,
    )
    return made


# ---------------------------------------------------------------------------
# image: 4.4.2 then 4.4.3
# ---------------------------------------------------------------------------


async def _image(
    ctx: RunContext, parent: CreativeAsset, child: CreativeAsset, request: RegenerationRequest
) -> RegeneratedAsset:
    creative = ctx.require_creative()
    inp = creative.input
    campaign, concept = _concept(ctx, parent)
    specs = creative.linter.ruleset.asset_specs.model_dump(mode="json").get("specs") or {}
    image_ratios, _ = ratios_for(specs, campaign.campaign_type)
    market, language = _market(inp, campaign.campaign_ref)
    mastering = await _Mastering.start(
        ctx, request.choice, node_id=REGENERATION_NODE, round=request.round
    )
    masters = await mastering.concept(
        concept,
        campaign_type=campaign.campaign_type,
        market=market,
        language=language,
        image_ratios=image_ratios,
        asset=child,
    )
    rendering = await _Rendering.start(
        ctx, request.choice, node_id=REGENERATION_NODE, round=request.round
    )
    rendered = await rendering.one_concept(campaign, concept, masters)
    products = {ref.sha256: ref.product_ref for ref in inp.references}
    base = {
        "parent_asset_id": parent.id,
        "asset_id": child.id,
        "kind": "image",
        "campaign_ref": campaign.campaign_ref,
        "concept_id": concept.id,
        "model_id": request.choice.model_id,
        "masters": masters,
        "renditions": rendered.renditions,
        "product_refs": sorted(
            {ref for sha in masters.reference_sha256s if (ref := products.get(sha))}
        ),
    }
    if rendered.renditions:
        return RegeneratedAsset(status="regenerated", **base)
    why = (
        f"{masters.gap.reason}: {masters.gap.detail}"
        if masters.gap is not None
        else "; ".join(f"{gap.ratio}: {gap.why}" for gap in rendered.gaps)
        or "no rendition could be made"
    )
    return RegeneratedAsset(status="gap", gap=why, **base)


# ---------------------------------------------------------------------------
# video: 4.4.4, planned again for the model it is regenerated with
# ---------------------------------------------------------------------------


async def _video(
    ctx: RunContext, parent: CreativeAsset, child: CreativeAsset, request: RegenerationRequest
) -> RegeneratedAsset:
    creative = ctx.require_creative()
    campaign, concept = _concept(ctx, parent)
    capability = CapabilityRecord.model_validate(request.choice.capability)
    supported = list((capability.video.durations if capability.video else []) or [])
    specs = creative.linter.ruleset.asset_specs.model_dump(mode="json").get("specs") or {}
    writer = DerivedWriter(
        ctx.db,
        store=EvidenceStore(ctx.db, ctx.run.workspace_id),
        project_id=ctx.project.id,
        plan_run_id=ctx.run.id,
    )
    planned = await n444.plan_campaign(
        creative,
        request.choice,
        campaign.model_copy(update={"concepts": [concept]}),
        writer=writer,
        supported=supported,
        specs=specs,
        end_card_s=-(-creative.constants.video.end_card_ms.value // 1000),
        node_id=REGENERATION_NODE,
    )
    base = {
        "parent_asset_id": parent.id,
        "asset_id": child.id,
        "kind": "video",
        "campaign_ref": campaign.campaign_ref,
        "concept_id": concept.id,
        "model_id": request.choice.model_id,
    }
    if planned is None:
        return RegeneratedAsset(
            status="gap", gap="The pinned spec sheet has no video surface here any more.", **base
        )
    if isinstance(planned, VideoGap):
        return RegeneratedAsset(status="gap", gap=f"{planned.reason}: {planned.detail}", **base)
    script_id = (parent.fields or {}).get("script_asset_id")
    parent_script = (
        await ctx.db.get(CreativeAsset, uuid.UUID(str(script_id))) if script_id else None
    )
    production = await n444._Production.start(
        ctx,
        choice=request.choice,
        node_id=REGENERATION_NODE,
        round=request.round,
        targets={
            campaign.campaign_ref: n444.RegenerationTarget(
                parent_id=parent.id, video_id=child.id, parent_script=parent_script
            )
        },
    )
    await production.campaign(planned)
    produced = await production.finish([])
    made = next((v for v in produced.videos if v.asset_id == child.id), None)
    if made is None or not made.renditions:
        why = "; ".join(f"{gap.reason}: {gap.detail}" for gap in produced.gaps)
        return RegeneratedAsset(
            status="gap", gap=why or "no rendition could be made", video=made, **base
        )
    await lint_from_script(ctx, child.id, made.script_asset_id)
    return RegeneratedAsset(status="regenerated", video=made, **base)


async def lint_from_script(ctx: RunContext, video_id: uuid.UUID, script_id: uuid.UUID) -> None:
    """A video says exactly its script — its captions are the script, burned
    in — so the words it carries are linted as the script was, at the pin
    (Law 33). The pictures are checked by post-production's verification."""
    script = await ctx.db.get(CreativeAsset, script_id, populate_existing=True)
    video = await ctx.db.get(CreativeAsset, video_id, populate_existing=True)
    if script is None or video is None:  # pragma: no cover — both committed above
        raise NodeContractError(f"video {video_id} or its script {script_id} vanished")
    video.lint = script.lint
    video.ruleset_version = script.ruleset_version
    await ctx.db.flush()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _concept(ctx: RunContext, parent: CreativeAsset) -> tuple[CampaignConcepts, Concept]:
    concept_id = (parent.fields or {}).get("concept_id")
    concepts = CreativeConcepts.model_validate(ctx.output_of(CONCEPTS_NODE))
    for campaign in concepts.campaigns:
        for concept in campaign.concepts:
            if concept.id == concept_id:
                return campaign, concept
    raise NodeContractError(
        f"asset {parent.id} was made for concept {concept_id!r}, which 4.4.1 no longer has"
    )


def _market(inp: CreativeInput, campaign_ref: str) -> tuple[str, str]:
    for campaign in inp.account_structure.campaigns:
        if (campaign.campaign_ref or campaign.name) == campaign_ref:
            return (getattr(campaign, "market", "") or "*"), (
                getattr(campaign, "language", None) or "en"
            )
    return "*", "en"


def estimate_usd(
    inp: CreativeInput,
    specs: dict[str, Any],
    parent: CreativeAsset,
    choice: MediaModelChoice,
    *,
    caps: Any,
    constants: Any,
) -> Decimal:
    """What regenerating `parent` should cost, priced by `calc/` exactly as the
    Start dialog prices a run (`media.cost_estimate_v1`): one concept of one
    campaign, in the asset's modality only, with no text."""
    image = parent.kind is CreativeAssetKind.IMAGE
    campaigns = [
        c
        for c in inp.account_structure.campaigns
        if (c.campaign_ref or c.name) == parent.campaign_ref
    ]
    scope = inp.scope.model_copy(
        update={"images": image, "video": not image, "campaign_refs": [parent.campaign_ref]}
    )
    inputs = estimate_inputs_for(campaigns, specs, scope, [choice], caps)
    # One concept, not the run's two or three: a regeneration repaints one asset.
    inputs["scope"]["concepts_per_campaign"] = 1
    inputs["text_usd"] = Decimal(0)
    draft = cost_estimate_v1(**inputs, constants=constants)
    return Decimal(str(draft.result["media_usd"]))


async def run_of(db: AsyncSession, asset: CreativeAsset) -> Run:
    run = await db.get(Run, asset.creative_run_id)
    if run is None:  # pragma: no cover — FK
        raise NodeContractError(f"asset {asset.id} has no run")
    return run
