"""S4-P13 — a video sent back at G8 is regenerated through 4.4.4's own code
(Stage 04 PRD §11 4.4.6): script, clips, post-production.

On S4-P12's fixture clips and recorded /videos: the regenerated video is a new
asset whose every job is 4.4.6's, round 2; it is shot from the script the
replaced video was made from (no new script is written or paid for); it leaves
draft carrying that script's lint; and it is what G8b asks about.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalStatus,
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    GenerationJob,
    NodeRun,
)
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import execute
from tests.integration.s4p9_support import approve_g7
from tests.integration.s4p11_support import Clock, media_jobs, providers, script_calls
from tests.integration.test_s4p12_video_postprod import FixtureVideos, _start
from tests.openrouter_fake import FakeOpenRouter

TICKED = {"label_ok": True, "product_match_ok": True, "subjects_ok": True, "rights_ok": True}


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


@pytest.fixture
def ids(workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any) -> tuple[Any, ...]:
    return workspace_id, project_id, admin_user.id


async def _execute(run_id: uuid.UUID, clock: Clock) -> None:
    from agent.nodes.creative.n4_1_1_creative_brief import CREATIVE_BRIEF
    from agent.nodes.creative.n4_4_1_creative_concepts import CREATIVE_CONCEPTS
    from agent.nodes.creative.n4_4_2_image_masters import IMAGE_MASTERS
    from agent.nodes.creative.n4_4_3_image_renditions import IMAGE_RENDITIONS
    from agent.nodes.creative.n4_4_4_video_production import VIDEO_PRODUCTION
    from agent.nodes.creative.n4_4_5_ai_asset_review import AI_ASSET_REVIEW
    from agent.nodes.creative.n4_4_6_asset_regeneration import ASSET_REGENERATION
    from agent.nodes.creative.n4_4_7_ai_asset_review_final import AI_ASSET_REVIEW_FINAL
    from agent.orchestrator.dag import Dag
    from agent.orchestrator.registry import NodeRegistry

    registry = NodeRegistry.of(
        [
            CREATIVE_BRIEF,
            CREATIVE_CONCEPTS,
            IMAGE_MASTERS,
            IMAGE_RENDITIONS,
            VIDEO_PRODUCTION,
            AI_ASSET_REVIEW,
            ASSET_REGENERATION,
            AI_ASSET_REVIEW_FINAL,
        ]
    )
    async with httpx.AsyncClient() as client:
        await execute(
            run_id,
            FakeOpenRouter(),
            client=client,
            registry=registry,
            dag=Dag.from_registry(registry),
            media=media_jobs(clock),
        )


async def _gate(db: AsyncSession, run_id: uuid.UUID, gate_key: str) -> Approval:
    row = (
        await db.execute(
            sa.select(Approval)
            .where(Approval.run_id == run_id, Approval.gate_key == gate_key)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    return row


async def _output(db: AsyncSession, run_id: uuid.UUID, node_id: str) -> dict[str, Any]:
    row = (
        (
            await db.execute(
                sa.select(NodeRun)
                .where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
                .order_by(NodeRun.attempt.desc())
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .first()
    )
    assert row is not None and row.output is not None, (node_id, row and row.error)
    return row.output


async def test_a_video_sent_back_at_g8_is_reshot_from_its_script_through_4_4_4(
    admin: ApiClient, db: AsyncSession, storage: LocalStorage, ids: tuple[Any, ...]
) -> None:
    run_id = await _start(admin, db, storage, ids)
    videos = FixtureVideos()
    clock = Clock()
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        chat = providers(router, videos)
        await _execute(run_id, clock)
        await approve_g7(admin, db, run_id)
        await _execute(run_id, clock)

        g8 = await _gate(db, run_id, "G8")
        assert g8.status is ApprovalStatus.PENDING
        (item,) = g8.proposal["items"]
        assert item["kind"] == "video"
        assert {r["ratio"] for r in item["renditions"]} == {"16:9", "9:16"}
        assert all(r["preview_media_id"] and r["poster_media_id"] for r in item["renditions"])
        parent_id = uuid.UUID(item["asset_id"])
        parent = await db.get(CreativeAsset, parent_id, populate_existing=True)
        assert parent is not None and parent.status is CreativeAssetStatus.AWAITING_REVIEW
        script = await db.get(
            CreativeAsset, uuid.UUID(parent.fields["script_asset_id"]), populate_existing=True
        )
        assert script is not None
        # 4.4.4 left the video draft with no lint; G8 carried its script's over.
        assert parent.lint == script.lint and parent.ruleset_version == script.ruleset_version

        decided = await admin.post(
            f"/approvals/{g8.id}",
            json={
                "decision": "approve",
                "edited_proposal": {
                    "items": [
                        {
                            "asset_id": str(parent_id),
                            "decision": "regenerate",
                            "note": "Open on the product sooner.",
                        }
                    ]
                },
            },
        )
        assert decided.status_code == 200, decided.text
        posts_before, scripts_before = len(videos.posts), len(script_calls(chat))
        jobs_before = set((await db.execute(sa.select(GenerationJob.id))).scalars().all())
        await _execute(run_id, clock)

    regenerated = await _output(db, run_id, "4.4.6")
    (made,) = regenerated["items"]
    assert made["status"] == "regenerated", made["gap"]
    child_id = uuid.UUID(made["asset_id"])
    child = await db.get(CreativeAsset, child_id, populate_existing=True)
    assert child is not None and child.kind is CreativeAssetKind.VIDEO
    assert child.lineage["origin"] == "regenerated" and child.lineage["parent_id"] == str(parent_id)
    assert child.status is CreativeAssetStatus.AWAITING_REVIEW

    # Re-shot from the same script: no new script written, no script model call.
    assert made["video"]["script_asset_id"] == str(script.id)
    assert child.fields["script_asset_id"] == str(script.id)
    assert len(script_calls(chat)) == scripts_before
    assert (
        await db.scalar(
            sa.select(sa.func.count(CreativeAsset.id)).where(
                CreativeAsset.creative_run_id == run_id,
                CreativeAsset.kind == CreativeAssetKind.VIDEO_SCRIPT,
            )
        )
        == 1
    )
    assert child.lint == script.lint

    # Every new clip job is 4.4.6's, round 2, of the new asset — and each was POSTed once.
    new_jobs = (
        (await db.execute(sa.select(GenerationJob).where(GenerationJob.id.not_in(jobs_before))))
        .scalars()
        .all()
    )
    assert new_jobs and {(j.node_id, j.asset_id, j.round) for j in new_jobs} == {
        ("4.4.6", child_id, 2)
    }
    assert len(videos.posts) - posts_before == len(new_jobs)
    assert {r["ratio"] for r in made["video"]["renditions"]} == {"16:9", "9:16"}
    assert all(r["verification"]["passed"] for r in made["video"]["renditions"])

    g8b = await _gate(db, run_id, "G8b")
    assert [i["asset_id"] for i in g8b.proposal["items"]] == [str(child_id)]
    approved = await admin.post(
        f"/approvals/{g8b.id}",
        json={
            "decision": "approve",
            "edited_proposal": {
                "items": [{"asset_id": str(child_id), "decision": "approve", "checklist": TICKED}]
            },
        },
    )
    assert approved.status_code == 200, approved.text
    child = await db.get(CreativeAsset, child_id, populate_existing=True)
    assert child is not None and child.status is CreativeAssetStatus.APPROVED
