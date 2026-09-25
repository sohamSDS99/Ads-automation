"""4.4.5 `ai_asset_review` ⛳G8 — every AI-made asset, one at a time (Stage 04 PRD §8.5, §11).

The card lists each image asset (4.4.2's master, 4.4.3's renditions) and each
video asset (4.4.4's renditions) that has at least one file to look at:
`items[]{asset_id, renditions[], disclosure, product_refs[], vision_advisory}`.
Those assets move to `awaiting_review`. G8 goes to the pinned sign-off matrix's
brand owner (`orchestrator.approvals.CREATIVE_GATE_OWNERS`), who decides each
item: approve (the four checks ticked), reject, or regenerate with a note and,
optionally, another allowlisted model and params — revalidated by the approval
route (`creative/review.py`) before anything is recorded.

`not_required` when there is nothing AI-made to look at (media off, or every
concept ended in a gap): a gate with no question is not asked (§8.5).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from pydantic import BaseModel

from agent.creative import review
from agent.db.models import (
    ApprovalRequiredRole,
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    Evidence,
    RunStage,
)
from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, RunContext
from agent.nodes.creative._regenerate import lint_from_script
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_media import ImageMasters, ImageRenditions
from agent.schemas.creative_review import G8, AiAssetReview

NODE_ID = "4.4.5"
MASTERS_NODE = "4.4.2"
RENDITIONS_NODE = "4.4.3"
VIDEO_NODE = "4.4.4"


class AiAssetReviewNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="ai_asset_review",
        stage="4.4",
        run_stage=RunStage.CREATIVE,
        depends_on=(RENDITIONS_NODE, VIDEO_NODE),
        gate=True,
        gate_conditional=True,
        gate_key=G8,
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.VISION,
        input_model=CreativeInput,
        output_model=AiAssetReview,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        creative = ctx.require_creative()
        # 4.4.2 always runs before 4.4.3 (its only input); its output carries
        # each master's VISION notes and the references the request sent.
        masters = ImageMasters.model_validate(ctx.outputs.get(MASTERS_NODE) or {})
        renditions = ImageRenditions.model_validate(ctx.output_of(RENDITIONS_NODE))
        items = [
            *review.image_items(masters.concepts, renditions, creative.input.references),
            *review.video_items(review.production_videos(ctx.output_of(VIDEO_NODE))),
        ]
        if not items:
            return AiAssetReview(
                status="not_required",
                round=1,
                why="Nothing AI-made has a file to review: media is off for this run, or "
                "every concept ended in a recorded gap.",
            )
        await awaiting_review(ctx, [item.asset_id for item in items])
        return AiAssetReview(status="review", round=1, items=items)

    def gate_required(self, ctx: RunContext, output: BaseModel) -> bool:
        return isinstance(output, AiAssetReview) and output.status == "review"


async def awaiting_review(ctx: RunContext, asset_ids: list[uuid.UUID]) -> None:
    """Every asset on the card leaves `draft`/`linted` for `awaiting_review`.
    A video carries its script's lint first (`lint_from_script`): the words it
    says are the script's, and nothing leaves draft unlinted (Law 33)."""
    rows = (
        (
            await ctx.db.execute(
                sa.select(CreativeAsset)
                .where(CreativeAsset.id.in_(asset_ids))
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        if row.kind is CreativeAssetKind.VIDEO and row.lint is None:
            script_id = (row.fields or {}).get("script_asset_id")
            if script_id:
                await lint_from_script(ctx, row.id, uuid.UUID(str(script_id)))
        if row.status in (CreativeAssetStatus.DRAFT, CreativeAssetStatus.LINTED):
            row.status = CreativeAssetStatus.AWAITING_REVIEW
    await ctx.db.flush()


AI_ASSET_REVIEW = AiAssetReviewNode()
