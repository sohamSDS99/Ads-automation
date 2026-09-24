"""S4-P10 — "Check again" on a relay (PRD §9.1 item 7, §18; Law 37).

A 4.4.3 relay's request carries the run's own master as its input image, and
the job row keeps it by sha256 only. An image left in `unknown_submit_state`
is POSTed again under the same key, so its bytes must come back from this
run's master — not from the uploaded references, which never held it — and
bytes that no longer hash to what was recorded are never sent.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
import respx
from PIL import Image
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    GenerationStatus,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
)
from agent.media.types import ReferenceImage
from tests.integration.test_media_jobs import (
    BASE,
    IMAGE,
    SimulatedCrash,
    World,
    _approve_brief,
    _creative_run,
    choice,
    flux,
)
from tests.media.openrouter_mock import response


@pytest.fixture
async def world(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    tmp_path: Path,
) -> World:
    run_id = await _creative_run(db, workspace_id, project_id, admin_user.id)
    await _approve_brief(db, workspace_id, project_id, run_id)
    return World(tmp_path, run_id)


def _png(colour: tuple[int, int, int]) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (48, 48), colour).save(buffer, format="PNG")
    return buffer.getvalue()


async def _master(db: AsyncSession, world: World, workspace_id: uuid.UUID, content: bytes) -> Any:
    from agent.db.models import Run

    run = await db.get(Run, world.run_id)
    assert run is not None
    asset = CreativeAsset(
        workspace_id=workspace_id,
        project_id=run.project_id,
        creative_run_id=world.run_id,
        node_id="4.4.2",
        campaign_ref="c-sds-us",
        kind=CreativeAssetKind.IMAGE,
        surface="pmax_image",
        generated_by_ai=True,
        content_hash=hashlib.sha256(content).hexdigest(),
    )
    db.add(asset)
    await db.flush()
    key = f"creative/{world.run_id}/media/{asset.id}/master-0.png"
    world.storage.put(key, content, content_type="image/png")
    db.add(
        MediaArtifact(
            workspace_id=workspace_id,
            asset_id=asset.id,
            role=MediaArtifactRole.MASTER,
            storage_path=key,
            media_type="image/png",
            width=48,
            height=48,
            bytes=len(content),
            sha256=hashlib.sha256(content).hexdigest(),
            aspect_ratio="1:1",
            derivation=MediaArtifactDerivation.NATIVE,
        )
    )
    await db.commit()
    return asset, key


async def _stuck_relay(world: World, asset_id: uuid.UUID, content: bytes) -> Any:
    submit: dict[str, Any] = dict(
        run_id=world.run_id,
        node_id="4.4.3",
        asset_id=asset_id,
        round=1,
        request=IMAGE.model_copy(
            update={
                "input_references": [
                    ReferenceImage(
                        sha256=hashlib.sha256(content).hexdigest(),
                        media_type="image/png",
                        data=content,
                    )
                ]
            }
        ),
        choice=choice(flux()),
        estimate_usd=Decimal("0.03"),
    )
    with pytest.raises(SimulatedCrash):
        await world.jobs(crash_at="submitting_committed").submit_or_resume(**submit)
    stuck = await world.jobs().submit_or_resume(**submit)
    assert stuck.status == GenerationStatus.UNKNOWN_SUBMIT_STATE
    return stuck


async def test_check_again_re_reads_the_runs_master_and_posts_it(
    db: AsyncSession, world: World, workspace_id: uuid.UUID
) -> None:
    content = _png((30, 60, 90))
    asset, _ = await _master(db, world, workspace_id, content)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        route = router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))
        stuck = await _stuck_relay(world, asset.id, content)

        checked = await world.jobs().check(stuck.id, choice=choice(flux()))

    assert checked.status == GenerationStatus.COMPLETED
    assert checked.idempotency_key == stuck.idempotency_key
    assert route.call_count == 1
    sent = json.loads(route.calls[0].request.content)["input_references"]
    encoded = base64.b64encode(content).decode()
    assert sent == [{"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}}]


async def test_a_master_whose_bytes_changed_is_never_sent_under_the_old_key(
    db: AsyncSession, world: World, workspace_id: uuid.UUID
) -> None:
    content = _png((30, 60, 90))
    asset, key = await _master(db, world, workspace_id, content)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        route = router.post(f"{BASE}/images").mock(return_value=response("image_generate.json"))
        stuck = await _stuck_relay(world, asset.id, content)
        world.storage.put(key, _png((200, 10, 10)), content_type="image/png")

        checked = await world.jobs().check(stuck.id, choice=choice(flux()))

    assert checked.status == GenerationStatus.UNKNOWN_SUBMIT_STATE
    assert route.call_count == 0
