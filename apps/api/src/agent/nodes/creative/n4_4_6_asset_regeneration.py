"""4.4.6 `asset_regeneration` — only what G8 sent back (Stage 04 PRD §8.5, §11).

Reads G8's decided card (4.4.5's `NodeRun` output, each item carrying its
decision) and regenerates **only** the `regenerate` items, through the
4.4.2–4.4.4 pipeline functions (`_regenerate.py`), with the model and params
the brand owner chose — validated at decision time and snapshotted onto the
item as `regeneration_choice`, so nothing here re-reads the allowlist or the
catalogue. Every job is round 2 of its asset: new idempotency keys, new seeds.

`not_required` when G8 sent nothing back, which makes G8b `not_required` too.
There is no second regeneration: 4.4.7 offers approve or reject only.
"""

from __future__ import annotations

import sqlalchemy as sa
from pydantic import BaseModel

from agent.creative import review
from agent.creative.review import REGENERATION_NODE
from agent.db.models import Approval, ApprovalStatus, CreativeAsset, Evidence, RunStage
from agent.db.session import get_sessionmaker
from agent.llm.router import TaskClass
from agent.nodes.base import NodeContractError, NodeSpec, RunContext
from agent.nodes.creative._regenerate import RegenerationRequest, open_child, regenerate
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_review import G8, ROUND, AiAssetReview, AssetRegeneration

NODE_ID = REGENERATION_NODE
REVIEW_NODE = "4.4.5"


class AssetRegenerationNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="asset_regeneration",
        stage="4.4",
        run_stage=RunStage.CREATIVE,
        depends_on=(REVIEW_NODE,),
        task_class=TaskClass.VISION,
        input_model=CreativeInput,
        output_model=AssetRegeneration,
        media=("image", "video"),
        lint_required=True,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        decided = AiAssetReview.model_validate(ctx.output_of(REVIEW_NODE))
        wanted = review.regenerate_items(decided)
        if not wanted:
            return AssetRegeneration(
                status="not_required",
                why="G8 approved or rejected every asset; nothing was sent back to regenerate."
                if decided.status == "review"
                else "G8 had nothing to review.",
            )
        reviewer = await ctx.db.scalar(
            sa.select(Approval.decided_by).where(
                Approval.run_id == ctx.run.id,
                Approval.node_id == REVIEW_NODE,
                Approval.status == ApprovalStatus.APPROVED,
            )
        )
        made = []
        for item in wanted:
            decision = item.decision
            if decision is None or decision.regeneration_choice is None:
                raise NodeContractError(
                    f"G8 sent asset {item.asset_id} back without the model it was validated for"
                )
            parent = await ctx.db.get(CreativeAsset, item.asset_id)
            if parent is None:
                raise NodeContractError(f"G8 sent back asset {item.asset_id}, which is gone")
            request = RegenerationRequest(
                parent_id=parent.id,
                choice=decision.regeneration_choice,
                note=decision.note or "",
                by_user=reviewer,
                round=ROUND[G8] + 1,
            )
            child_id = await open_child(get_sessionmaker(), parent, request)
            child = await ctx.db.get(CreativeAsset, child_id, populate_existing=True)
            if child is None:  # pragma: no cover — committed just above
                raise NodeContractError(f"regenerated asset {child_id} vanished")
            await ctx.progress(
                f"Regenerating {item.kind} {parent.id} with {request.choice.model_id}"
            )
            made.append(await regenerate(ctx, parent, child, request))
        return AssetRegeneration(status="regenerated", items=made)


ASSET_REGENERATION = AssetRegenerationNode()
