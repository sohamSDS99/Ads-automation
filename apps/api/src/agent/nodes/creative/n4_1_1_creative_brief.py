"""4.1.1 `creative_brief` ⛳G7 — the one-page brief (Stage 04 PRD §11, §12.1, §5.3).

`gather()` reads nothing but the run's pinned inputs: the `CreativeInput`
slices, the Evidence those slices cite, and the media plan priced in `calc/`
(`media.cost_estimate_v1`, `media.ratio_plan_v1`), recorded as `derived` rows
so the brief can cite them.

`reason()` asks SYNTHESIZE for words and choices only (`creative/brief.py`
builds the enum-constrained schema), binds them to the facts code owns, renders
and measures the page, hashes it, and writes the `CreativeBrief` row G7 will
approve. The brief then halts on G7, routed to the sign-off matrix's
performance owner; no media is submitted before it is approved with a
matching hash (`media/jobs.py`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel

from agent.calc.derived import DerivedWriter
from agent.calc.media import cost_estimate_v1, ratio_plan_v1
from agent.config import get_settings
from agent.creative import brief as briefs
from agent.db.models import (
    ApprovalRequiredRole,
    Evidence,
    EvidenceSource,
    RunStage,
    Workspace,
)
from agent.db.models import (
    CreativeBrief as CreativeBriefRow,
)
from agent.evidence.store import EvidenceStore
from agent.llm.router import TaskClass
from agent.media.budget import resolve_media_caps
from agent.nodes.base import NodeContractError, NodeSpec, RunContext, collect_evidence_ids
from agent.orchestrator.creative_input import estimate_inputs_for, ratio_inputs_for
from agent.schemas.creative_brief import MAX_RENDERED_WORDS, CreativeBrief
from agent.schemas.creative_input import CreativeInput

NODE_ID = "4.1.1"
G7 = "G7"

SYSTEM = f"""You write the one-page creative brief a paid-search team works from.

Rules, all of them hard:
- Every line you write must cite at least one source key from SOURCES, and only
  what that source actually says. Never state a fact no cited source gives.
- Write no numbers, prices, percentages, dates or deadlines. The brief's figures
  come from the plan and the calculator, not from you.
- Assert nothing about the product except through PROOF POINTS, and pick only
  from the claim ids listed there. If none fit, pick none.
- Brief every ad group in AD GROUPS exactly once. `angle_b` must be a genuinely
  different angle from `primary_message`, not a paraphrase of it.
- Plain, specific language in the brand's voice. No exclamation marks.
- The whole brief renders to at most {MAX_RENDERED_WORDS} words. Aim for 350:
  one line per point, a few words per ad-group field.
"""


class CreativeBriefNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="creative_brief",
        stage="4.1",
        run_stage=RunStage.CREATIVE,
        depends_on=(),
        gate=True,
        gate_key=G7,
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.SYNTHESIZE,
        input_model=CreativeInput,
        output_model=CreativeBrief,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        creative = ctx.require_creative()
        inp = creative.input
        cited = collect_evidence_ids(inp.model_dump(mode="json", by_alias=True))
        rows: list[Evidence] = []
        if cited:
            rows = list(
                (
                    await ctx.db.execute(
                        sa.select(Evidence).where(
                            Evidence.id.in_(cited), Evidence.project_id == ctx.project.id
                        )
                    )
                )
                .scalars()
                .all()
            )
        derived = await self._price(ctx)
        return [*rows, *derived]

    async def _price(self, ctx: RunContext) -> list[Evidence]:
        """The media plan G7 authorises, computed in `calc/` and made citable."""
        creative = ctx.require_creative()
        inp = creative.input
        specs = creative.linter.ruleset.asset_specs.model_dump(mode="json").get("specs") or {}
        workspace = await ctx.db.get(Workspace, ctx.run.workspace_id)
        caps = resolve_media_caps(
            project_settings=ctx.project.settings,
            workspace_settings=workspace.settings if workspace else None,
            defaults=get_settings(),
        )
        constants = creative.constants.media_constants()
        campaigns = list(inp.account_structure.campaigns)
        estimate = cost_estimate_v1(
            **estimate_inputs_for(campaigns, specs, inp.scope, list(inp.media_models), caps),
            constants=constants,
        )
        ratios = ratio_plan_v1(
            **ratio_inputs_for(campaigns, specs, inp.scope, list(inp.media_models)),
            constants=constants,
        )
        writer = DerivedWriter(
            ctx.db,
            store=EvidenceStore(ctx.db, ctx.run.workspace_id),
            project_id=ctx.project.id,
            plan_run_id=ctx.run.id,
        )
        estimate_id = await writer.record(estimate, node_id=NODE_ID)
        ratios_id = await writer.record(ratios, node_id=NODE_ID)
        ctx.scratch[NODE_ID] = {
            "estimate": estimate.result,
            "ratio_plan": ratios.result,
            "calc_evidence_ids": [estimate_id, ratios_id],
        }
        return list(
            (
                await ctx.db.execute(
                    sa.select(Evidence).where(
                        Evidence.id.in_([estimate_id, ratios_id]),
                        Evidence.source == EvidenceSource.DERIVED,
                    )
                )
            )
            .scalars()
            .all()
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        inp = creative.input
        priced = ctx.scratch[NODE_ID]
        menu = briefs.source_menu(inp, {row.id for row in ev})
        # Licensed as of the run's own start, not the wall clock: a retry an
        # hour later must be offered the same claims the first attempt was.
        claims = briefs.licensed_claims(
            creative.linter.ruleset, ctx.run.started_at or datetime.now(UTC)
        )
        slots = briefs.ad_group_slots(inp)
        schema = briefs.draft_model(menu, claims, slots)

        draft = await ctx.complete(
            schema, system=SYSTEM, user=_user_prompt(inp, menu, claims, slots)
        )
        brief = briefs.assemble(
            draft,
            inp=inp,
            menu=menu,
            claims=claims,
            slots=slots,
            non_negotiable=briefs.non_negotiables(inp),
            visual=briefs.visual_constraints(
                inp, briefs.product_depiction(inp, ctx.project.settings)
            ),
            plan=briefs.media_plan(
                inp,
                estimate=priced["estimate"],
                ratio_plan=priced["ratio_plan"],
                calc_evidence_ids=priced["calc_evidence_ids"],
            ),
        )
        await _persist(ctx, brief)
        return brief


async def _persist(ctx: RunContext, brief: CreativeBrief) -> None:
    """Write (or, on a retry, rewrite) the brief G7 is about to be asked about."""
    row = (
        await ctx.db.execute(
            sa.select(CreativeBriefRow).where(CreativeBriefRow.creative_run_id == ctx.run.id)
        )
    ).scalar_one_or_none()
    payload = brief.model_dump(mode="json")
    markdown = briefs.render(brief)
    if row is None:
        ctx.db.add(
            CreativeBriefRow(
                workspace_id=ctx.run.workspace_id,
                project_id=ctx.project.id,
                creative_run_id=ctx.run.id,
                schema_version=brief.schema_version,
                payload=payload,
                markdown=markdown,
                brief_hash=brief.brief_hash,
            )
        )
    elif row.approved_hash is not None:
        # Unreachable through the executor — a decided gate is never re-run —
        # and refused anyway: an approved brief is frozen (trigger
        # creative_brief_approved_freeze), and a new brief is a new approval.
        raise NodeContractError(f"run {ctx.run.id}'s brief was already approved at G7")
    else:
        row.schema_version = brief.schema_version
        row.payload = payload
        row.markdown = markdown
        row.brief_hash = brief.brief_hash
    await ctx.db.flush()


def _user_prompt(
    inp: CreativeInput,
    menu: list[briefs.MenuEntry],
    claims: list[Any],
    slots: list[briefs.AdGroupSlot],
) -> str:
    sections = {
        "SOURCES": [{"key": entry.key, "says": entry.summary} for entry in menu],
        "PROOF POINTS": [
            {"claim_id": str(claim.claim_id), "text": claim.normalized_text} for claim in claims
        ],
        "AD GROUPS": [
            {
                "slot": slot.key,
                "campaign": slot.campaign_ref,
                "ad_group": slot.ad_group_ref,
                "theme": slot.theme,
                "plan_message": slot.primary_message,
                "keywords": list(slot.top_keywords),
                "source_key": f"S2.adgroup.{slot.key}",
            }
            for slot in slots
        ],
        "VOICE": list(inp.creative_context.voice.voice_words),
    }
    return "\n\n".join(
        f"{name}:\n{json.dumps(value, ensure_ascii=False, indent=1)}"
        for name, value in sections.items()
    )


CREATIVE_BRIEF = CreativeBriefNode()
