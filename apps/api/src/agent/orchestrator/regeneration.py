"""An operator's regeneration before G8 is decided (Stage 04 PRD §16, §9.2, §5.2).

`POST /creative-assets/{id}/regenerate` checks the model, its params and the
budget, commits the new asset (`state=running`) and queues this; the worker
runs it. It is not a node — the run is paused on G8 while it happens — but it
produces the asset through exactly the code 4.4.6 does (`_regenerate.py`),
inside a `RunContext` built the way the executor builds one: the workspace's
OpenRouter key, the run's router, its ledger, its pinned input and linter, the
media gateway, and every succeeded node's output.

When the new asset has something to look at it takes the old one's place on
the pending G8 card — same position, the reviewer's draft for the old one
cleared — and the old one is `dropped` (superseded before review). A G8
decision is refused while a regeneration of one of its items is running
(`creative/review.decide`), so the card never changes under a decision.
Nothing is replaced when the attempt ends in a gap: the original stays on the
card for review.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import get_settings
from agent.creative import review
from agent.credentials import resolve_values
from agent.db.models import (
    Approval,
    ApprovalStatus,
    CreativeAsset,
    CreativeAssetStatus,
    CredentialKind,
    NodeRun,
    NodeRunStatus,
    Project,
    Run,
    Workspace,
)
from agent.db.session import get_sessionmaker
from agent.llm.gateway import build_gateway
from agent.llm.ledger import RunLedger
from agent.llm.router import ModelRouter
from agent.media.http import MediaApi
from agent.media.images import ImageClient
from agent.media.jobs import MediaJobs
from agent.media.videos import VideoClient
from agent.nodes.base import RunContext
from agent.nodes.creative._regenerate import RegenerationRequest, regenerate
from agent.orchestrator import creative_run
from agent.orchestrator.budget import resolve_cost_cap
from agent.orchestrator.state import RunStore
from agent.redis_client import get_redis
from agent.schemas.creative_media import ImageRenditions
from agent.schemas.creative_review import G8, AiAssetReview, RegeneratedAsset
from agent.storage.backend import get_storage

log = structlog.get_logger(__name__)

REVIEW_NODE = "4.4.5"


async def run_regeneration(asset_id: uuid.UUID) -> dict[str, Any]:
    """Produce one queued regeneration. Idempotent: an asset that is no longer
    `running` (done, a gap, or gone) is left as it is."""
    sessions = get_sessionmaker()
    async with sessions() as db:
        child = await db.get(CreativeAsset, asset_id)
        if child is None or (child.fields or {}).get("state") != review.RUNNING:
            return {
                "asset_id": str(asset_id),
                "state": (child.fields or {}).get("state") if child else None,
            }
        request = RegenerationRequest.of(child)
        parent = await db.get(CreativeAsset, request.parent_id)
        loaded = await RunStore(db).load(child.creative_run_id)
        if parent is None or loaded is None:  # pragma: no cover — FKs
            return await _gap(asset_id, "the asset it replaces, or its run, is gone")
        run, project = loaded
        try:
            async with _context(db, run, project) as ctx:
                made = await regenerate(ctx, parent, child, request)
                if made.status == "regenerated":
                    await _on_card(db, run, parent, child.id, made, ctx)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 — every failure is recorded on the asset
            await db.rollback()
            log.warning("creative.regeneration_failed", asset_id=str(asset_id), error=str(exc))
            return await _gap(asset_id, f"{type(exc).__name__}: {str(exc)[:500]}")
        return {"asset_id": str(asset_id), "state": made.status}


@asynccontextmanager
async def _context(db: AsyncSession, run: Run, project: Project) -> AsyncIterator[RunContext]:
    """A `RunContext` for node 4.4.6's code outside the executor — built from
    the same pieces `RunExecutor.execute()` builds a creative run's from. What
    it spends (text and media) is added to the run afterwards, atomically."""
    settings = get_settings()
    values = await resolve_values(db, workspace_id=run.workspace_id, kind=CredentialKind.OPENROUTER)
    gateway, client = build_gateway(api_key=values["api_key"], settings=settings)
    workspace = await db.get(Workspace, run.workspace_id)
    router = ModelRouter.resolve(
        workspace_settings=workspace.settings if workspace else None,
        project_settings=project.settings,
    )
    ledger = RunLedger(
        cap_usd=resolve_cost_cap(
            stage=run.stage,
            project_settings=project.settings,
            workspace_settings=workspace.settings if workspace else None,
            defaults=settings,
            project_id=str(project.id),
        ),
        spent_usd=Decimal(run.cost_usd),
    )
    start = (ledger.spent_usd, ledger.token_in, ledger.token_out)
    try:
        creative = await creative_run.load_resources(db, run, project)
        api = MediaApi(
            client=client,
            api_key=values["api_key"],
            base_url=settings.openrouter_base_url,
            referer=settings.app_base_url,
        )
        media = MediaJobs(
            sessionmaker=get_sessionmaker(),
            redis=get_redis(),
            images=ImageClient(api),
            videos=VideoClient(api),
            storage=get_storage(settings),
            constants=creative.constants.media_constants(),
            defaults=settings,
        )
        latest = await RunStore(db).latest_by_node(run.id)
        yield RunContext(
            run=run,
            project=project,
            db=db,
            llm=gateway,
            router=router,
            ledger=ledger,
            outputs={
                node_id: node_run.output or {}
                for node_id, node_run in latest.items()
                if node_run.status is NodeRunStatus.SUCCEEDED
            },
            node_id=review.REGENERATION_NODE,
            creative=creative,
            media=media,
        )
    finally:
        await client.aclose()
        await _add_spend(run.id, ledger, start)


async def _add_spend(run_id: uuid.UUID, ledger: RunLedger, start: tuple[Decimal, int, int]) -> None:
    """The ledger's delta onto the run, as an increment — never an overwrite of
    whatever the run's own ledger rolled up meanwhile."""
    spent, tokens_in, tokens_out = start
    delta = ledger.spent_usd - spent
    if delta == 0 and ledger.token_in == tokens_in and ledger.token_out == tokens_out:
        return
    async with get_sessionmaker()() as session:
        await session.execute(
            sa.update(Run)
            .where(Run.id == run_id)
            .values(
                cost_usd=Run.cost_usd + delta,
                token_in=Run.token_in + (ledger.token_in - tokens_in),
                token_out=Run.token_out + (ledger.token_out - tokens_out),
            )
        )
        await session.commit()


async def _on_card(
    db: AsyncSession,
    run: Run,
    parent: CreativeAsset,
    child_id: uuid.UUID,
    made: RegeneratedAsset,
    ctx: RunContext,
) -> None:
    """Put the regenerated asset where the old one was on the pending G8 card.

    The approval row is locked first — the same lock `review.decide` takes —
    so a decision and a replacement cannot interleave. If G8 is no longer
    pending, or no longer lists the old asset, the new one is not reviewable
    here and is left a gap saying so."""
    approval = await db.scalar(
        sa.select(Approval)
        .where(
            Approval.run_id == run.id,
            Approval.gate_key == G8,
            Approval.status == ApprovalStatus.PENDING,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    card = AiAssetReview.model_validate(approval.proposal) if approval is not None else None
    position = (
        next((i for i, item in enumerate(card.items) if item.asset_id == parent.id), None)
        if card is not None
        else None
    )
    if approval is None or card is None or position is None:
        await _mark_gap(
            db,
            child_id,
            "G8 was decided, or stopped listing the asset it replaces, "
            "before this regeneration finished.",
        )
        return
    references = ctx.require_creative().input.references
    items = (
        review.image_items(
            [made.masters] if made.masters is not None else [],
            ImageRenditions(renditions=made.renditions),
            references,
            regenerated_from={child_id: parent.id},
        )
        if made.kind == "image"
        else review.video_items(
            [made.video] if made.video is not None else [], regenerated_from={child_id: parent.id}
        )
    )
    if not items:  # pragma: no cover — "regenerated" means it has renditions
        await _mark_gap(db, child_id, "the regeneration produced nothing to review")
        return
    replaced = card.model_copy(
        update={"items": [*card.items[:position], items[0], *card.items[position + 1 :]]}
    )
    approval.proposal = replaced.model_dump(mode="json")
    if approval.draft_state:
        draft = dict(approval.draft_state)
        draft["items"] = [
            item for item in draft.get("items") or [] if item.get("asset_id") != str(parent.id)
        ]
        if draft.get("cursor") == str(parent.id):
            draft["cursor"] = str(child_id)
        approval.draft_state = draft
    await db.execute(
        sa.update(NodeRun)
        .where(
            NodeRun.run_id == run.id,
            NodeRun.node_id == REVIEW_NODE,
            NodeRun.status == NodeRunStatus.AWAITING_APPROVAL,
        )
        .values(output=approval.proposal)
    )
    parent.status = CreativeAssetStatus.DROPPED
    await db.flush()
    log.info(
        "creative.regeneration_on_card", run_id=str(run.id), old=str(parent.id), new=str(child_id)
    )


async def _mark_gap(db: AsyncSession, asset_id: uuid.UUID, why: str) -> None:
    row = await db.get(CreativeAsset, asset_id, populate_existing=True)
    if row is None:  # pragma: no cover
        return
    row.fields = {**(row.fields or {}), "state": review.GAP, "gap": why}
    row.status = CreativeAssetStatus.DRAFT
    await db.flush()


async def _gap(asset_id: uuid.UUID, why: str) -> dict[str, Any]:
    async with get_sessionmaker()() as session:
        row = await session.get(CreativeAsset, asset_id)
        if row is not None:
            row.fields = {
                **(row.fields or {}),
                "state": review.GAP,
                "gap": why,
                "finished_at": datetime.now(UTC).isoformat(),
            }
            row.status = CreativeAssetStatus.DRAFT
            await session.commit()
    return {"asset_id": str(asset_id), "state": review.GAP, "gap": why}
