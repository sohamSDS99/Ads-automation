"""4.4.7 `ai_asset_review_final` ⛳G8b — the regenerated assets, once (Stage 04 PRD §8.5, §11).

G8's second round is this static node, not a re-opened gate. It lists what
4.4.6 regenerated — each new asset beside the one it replaces
(`regenerated_from`) — and the brand owner approves or rejects each. There is
no `regenerate` here: the approval route refuses one at G8b, and a rejected
G8b item is `dropped` (`creative/review.py`).

`not_required` when 4.4.6 was, or when every regeneration ended in a gap —
there is then nothing to look at.
"""

from __future__ import annotations

from pydantic import BaseModel

from agent.creative import review
from agent.db.models import ApprovalRequiredRole, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes.base import NodeSpec, RunContext
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_media import ImageRenditions
from agent.schemas.creative_review import G8B, AiAssetReview, AssetRegeneration

NODE_ID = "4.4.7"
REGENERATION_NODE = "4.4.6"


class AiAssetReviewFinalNode:
    spec = NodeSpec(
        id=NODE_ID,
        name="ai_asset_review_final",
        stage="4.4",
        run_stage=RunStage.CREATIVE,
        depends_on=(REGENERATION_NODE,),
        gate=True,
        gate_conditional=True,
        gate_key=G8B,
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.VISION,
        input_model=CreativeInput,
        output_model=AiAssetReview,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        regenerated = AssetRegeneration.model_validate(ctx.output_of(REGENERATION_NODE))
        if regenerated.status == "not_required":
            return AiAssetReview(
                status="not_required", round=2, why="4.4.6 was not required: G8 sent nothing back."
            )
        done = [item for item in regenerated.items if item.status == "regenerated"]
        parents = {item.asset_id: item.parent_asset_id for item in done}
        images = [item for item in done if item.kind == "image" and item.masters is not None]
        items = [
            *review.image_items(
                [item.masters for item in images if item.masters is not None],
                ImageRenditions(renditions=[r for item in images for r in item.renditions]),
                ctx.require_creative().input.references,
                regenerated_from=parents,
            ),
            *review.video_items(
                [item.video for item in done if item.kind == "video" and item.video is not None],
                regenerated_from=parents,
            ),
        ]
        if not items:
            return AiAssetReview(
                status="not_required",
                round=2,
                why="Every regeneration ended in a gap, so there is nothing to look at again: "
                + "; ".join(f"{i.parent_asset_id}: {i.gap}" for i in regenerated.items),
            )
        return AiAssetReview(status="review", round=2, items=items)

    def gate_required(self, ctx: RunContext, output: BaseModel) -> bool:
        return isinstance(output, AiAssetReview) and output.status == "review"


AI_ASSET_REVIEW_FINAL = AiAssetReviewFinalNode()
