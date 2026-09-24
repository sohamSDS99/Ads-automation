"""4.4.1 `creative_concepts` — visual concepts per campaign (Stage 04 PRD §11, §13).

`product_depiction` is resolved once, **in code**, from the image model's
capability × the run's references × Law 44 (`media/references.py`), against
live rows: a reference retired since the run started, a project that stopped
allowing references, a clearance granted in this run — each counts now. The
model is never asked what the depiction is and cannot answer it: the schema it
fills has no such field.

For every campaign in scope that needs a picture, SYNTHESIZE writes
`concepts_per_campaign` concepts against an enum-constrained schema built from
the G7-approved brief (its angles, the campaign's ratios, the palette tokens);
`creative/concepts.py` binds them to what code decided — the depiction, the
surfaces and every negative constraint, including "no text, no logos, no
watermark" on a search surface. Nothing is submitted here: concepts are 4.4.2's
and 4.4.4's input.
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from agent.creative import concepts
from agent.db.models import CreativeBrief as CreativeBriefRow
from agent.db.models import Evidence, RunStage
from agent.llm.router import TaskClass
from agent.media import references
from agent.media.types import CapabilityRecord
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.orchestrator.creative_input import ratios_for
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_media import CampaignConcepts, CreativeConcepts, Depiction

NODE_ID = "4.4.1"
BRIEF_NODE = "4.1.1"

_DEPICTION_RULE: dict[Depiction, str] = {
    "reference_guided": "The product appears only as the attached reference image shows it — "
    "never redrawn, restyled or relabelled.",
    "composited_real": "Do not show the product. Paint the scene around a clear, empty space "
    "where the real product photo will be placed afterwards.",
    "none": "Do not show the product, its packaging or anything that could be mistaken for it.",
}

SYSTEM = """You design the visual concepts a paid-media team generates advertising images from.
Rules, all of them hard:
- Build every concept on exactly one angle from ANGLES, and say in the rationale how the
  picture carries that angle.
- Describe one photographic scene: its subject, setting and light. Never text, captions,
  numbers, logos, badges, stamps or user-interface elements.
- Never name or depict a real person, a celebrity, a competitor or another brand.
- Never invent a product, packaging, labels, screenshots or certificates. {depiction}
- Give one composition note per ratio: how the frame holds the subject at that shape.
- Choose palette tokens only from PALETTE. Plain, specific language; no exclamation marks.
- The concepts must be genuinely different pictures, not one scene reworded.
"""


class CreativeConceptsNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="creative_concepts",
        stage="4.4",
        run_stage=RunStage.CREATIVE,
        depends_on=(BRIEF_NODE,),
        task_class=TaskClass.SYNTHESIZE,
        input_model=CreativeInput,
        output_model=CreativeConcepts,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # Everything 4.4.1 reads is pinned (the input) or decided (the approved
        # brief, the reference rows); none of it is new evidence.
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        inp = creative.input
        depiction, refusals = await resolve_depiction(ctx)
        if not (inp.scope.images or inp.scope.video):
            return CreativeConcepts(product_depiction=depiction, reference_refusals=refusals)

        brief = await approved_brief(ctx)
        specs = creative.linter.ruleset.asset_specs.model_dump(mode="json").get("specs") or {}
        palette = list(brief.visual_constraints.palette_tokens)
        forbidden = list(brief.visual_constraints.forbidden_subjects)
        campaigns: list[CampaignConcepts] = []
        for campaign in campaigns_in_scope(inp):
            ref = campaign.campaign_ref or campaign.name
            image_ratios, video_ratios = ratios_for(specs, campaign.type)
            ratios = [
                *(image_ratios if inp.scope.images else []),
                *(r for r in (video_ratios if inp.scope.video else []) if r not in image_ratios),
            ]
            if not ratios:
                continue  # nothing this campaign needs pictured under the pinned specs
            surfaces = concepts.surfaces_for(
                campaign.type, images_in_scope=inp.scope.images, image_ratios=image_ratios
            )
            angles = concepts.brief_angles(brief, ref)
            schema = concepts.draft_model(angles, ratios, palette, inp.scope.concepts_per_campaign)
            draft = await ctx.complete(
                schema,
                system=SYSTEM.format(depiction=_DEPICTION_RULE[depiction]),
                user=_user_prompt(
                    campaign_ref=ref,
                    campaign_type=campaign.type,
                    angles=angles,
                    ratios=ratios,
                    palette=palette,
                    brief=brief,
                    count=inp.scope.concepts_per_campaign,
                ),
            )
            campaigns.append(
                CampaignConcepts(
                    campaign_ref=ref,
                    campaign_type=campaign.type or "unspecified",
                    concepts=concepts.assemble(
                        draft,
                        campaign_ref=ref,
                        angles=angles,
                        ratios=ratios,
                        surfaces=surfaces,
                        depiction=depiction,
                        forbidden_subjects=forbidden,
                    ),
                )
            )
        return CreativeConcepts(
            product_depiction=depiction, reference_refusals=refusals, campaigns=campaigns
        )


async def resolve_depiction(ctx: RunContext) -> tuple[Depiction, dict[str, str | None]]:
    """`product_depiction`, and the Law 44 clause each reference failed (None = may go)."""
    creative = ctx.require_creative()
    inp = creative.input
    choice = next((c for c in inp.media_models if c.modality == "image"), None)
    capability = CapabilityRecord.model_validate(choice.capability) if choice else None
    facts = await references.live_facts(
        ctx.db, project_id=ctx.project.id, reference_ids=[r.reference_id for r in inp.references]
    )
    cleared = await references.cleared_image_rights(ctx.db, ctx.run.id)
    allowed = await references.project_allows_references(ctx.db, ctx.project.id)
    max_bytes = creative.constants.media_constants().reference_max_bytes
    depiction = references.product_depiction(
        facts,
        images_in_scope=inp.scope.images,
        allowed=allowed,
        capability=capability,
        cleared=cleared,
        max_bytes=max_bytes,
    )
    refusals: dict[str, str | None] = {
        str(fact.reference_id): references.refusal(
            fact, allowed=allowed, capability=capability, cleared=cleared, max_bytes=max_bytes
        )
        for fact in facts
    }
    return depiction, refusals


async def approved_brief(ctx: RunContext) -> CreativeBrief:
    """The brief as G7 approved it — edits included (the row is re-hashed on edit)."""
    row = (
        await ctx.db.execute(
            sa.select(CreativeBriefRow).where(CreativeBriefRow.creative_run_id == ctx.run.id)
        )
    ).scalar_one_or_none()
    if row is None or row.approved_hash is None or row.approved_hash != row.brief_hash:
        raise NodeContractError(
            f"run {ctx.run.id} has no G7-approved brief; concepts are built on the approved one"
        )
    return CreativeBrief.model_validate(row.payload)


def campaigns_in_scope(inp: CreativeInput) -> list[Any]:
    wanted = set(inp.scope.campaign_refs)
    return [
        campaign
        for campaign in inp.account_structure.campaigns
        if not wanted or (campaign.campaign_ref or campaign.name) in wanted
    ]


def _user_prompt(
    *,
    campaign_ref: str,
    campaign_type: str,
    angles: list[concepts.Angle],
    ratios: list[str],
    palette: list[str],
    brief: CreativeBrief,
    count: int,
) -> str:
    sections: dict[str, Any] = {
        "CAMPAIGN": {"ref": campaign_ref, "type": campaign_type, "concepts_wanted": count},
        "ANGLES": [{"key": angle.key, "says": angle.text} for angle in angles],
        "AUDIENCE": [line.text for line in brief.audience],
        "RATIOS": ratios,
        "PALETTE": palette,
        "PERMITTED SUBJECTS": list(brief.visual_constraints.permitted_subjects),
        "FORBIDDEN SUBJECTS": list(brief.visual_constraints.forbidden_subjects),
    }
    return "\n\n".join(
        f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=1)}"
        for name, value in sections.items()
    )


CREATIVE_CONCEPTS = CreativeConceptsNode()
