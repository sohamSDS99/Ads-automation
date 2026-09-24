"""S4-P9 — 4.4.2 `image_masters`, run through the executor (PRD §11, Laws 33, 37, 44).

Exit criteria asserted here, on the wire:
* a candidate that fails image lint is discarded BEFORE ranking — VISION is
  never shown it and it can never be the master;
* every candidate failing ⇒ one retry with strengthened negatives, then a gap;
* a third-party reference is never sent without a cleared H3 `image_right`.

Two concepts per run (`c-sds-us:c1`, `:c2`), two candidates each (`n=2`), taken
from the provider's queue in submit order. Coverage 0.5 fails the pinned
`image.text_coverage.v1` rule (max 0.20); 0.0 passes.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative.concepts import STRENGTHENED_NEGATIVES
from agent.db.models import (
    CreativeAsset,
    CreativeAssetStatus,
    CreativeException,
    CreativeExceptionKind,
    CreativeExceptionStatus,
    MediaArtifact,
    MediaArtifactRole,
    MediaReference,
    MediaReferenceOrigin,
)
from agent.media import references
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.s4p9_support import (
    Provider,
    approve_g7,
    coverage_table,
    image_model,
    output_of,
    png,
    run_until_done,
    seed_reference,
    start_image_run,
)

PASS, FAIL = 0.0, 0.5


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


def _images(*ratios: float) -> list[tuple[bytes, float]]:
    return [(png((10 + 20 * i, 90, 160)), ratio) for i, ratio in enumerate(ratios)]


async def _masters(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    monkeypatch: pytest.MonkeyPatch,
    images: list[tuple[bytes, float]],
    *,
    refs: list[MediaReference] = (),  # type: ignore[assignment]
    allowed: bool = False,
    clear: MediaReference | None = None,
) -> tuple[dict[str, Any], Provider, uuid.UUID]:
    coverage_table(monkeypatch, dict(images))
    run_id = await start_image_run(
        admin, db, workspace_id, project_id, actor,
        capability=image_model(), refs=refs, allowed=allowed,
    )  # fmt: skip
    if clear is not None:
        db.add(
            CreativeException(
                workspace_id=workspace_id,
                project_id=project_id,
                creative_run_id=run_id,
                kind=CreativeExceptionKind.IMAGE_RIGHT,
                subject_text=f"reference {clear.id}",
                proposed=references.image_right_proposal(clear.id, basis="licence on file"),
                status=CreativeExceptionStatus.CLEARED,
                decided_by=actor,
                decided_at=datetime.now(UTC),
            )
        )
        await db.commit()
    provider = Provider([content for content, _ in images])
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        provider.install(router)
        await run_until_done(run_id)
        await approve_g7(admin, db, run_id)
        await run_until_done(run_id)
    return await output_of(db, run_id, "4.4.2"), provider, run_id


def _b64(content: bytes) -> str:
    return base64.b64encode(content).decode()


def _sent_images(call: dict[str, Any]) -> list[str]:
    parts = call["messages"][-1]["content"]
    return [p["image_url"]["url"] for p in parts if p.get("type") == "image_url"]


async def test_a_failing_candidate_is_discarded_before_ranking(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    storage: LocalStorage,
) -> None:
    # c1: both fail, retry → both pass (VISION ranks two). c2: one fails, one passes.
    images = _images(FAIL, FAIL, PASS, PASS, FAIL, PASS)
    out, provider, run_id = await _masters(
        admin, db, workspace_id, project_id, admin_user.id, monkeypatch, images
    )
    c1, c2 = out["concepts"]
    failing = {_b64(content) for content, ratio in images if ratio == FAIL}

    # VISION ran once — for c1's retry — and saw exactly its two survivors.
    (vision,) = provider.vision_calls()
    shown = _sent_images(vision)
    assert shown == [f"data:image/png;base64,{_b64(images[i][0])}" for i in (2, 3)]
    assert not any(b64 in url for url in shown for b64 in failing)

    assert [c["lint"]["verdict"] for c in c1["candidates"]] == ["fail", "fail", "pass", "pass"]
    assert [c["attempt"] for c in c1["candidates"]] == [1, 1, 2, 2]
    assert all(c["vision_advisory"] is None for c in c1["candidates"][:2])
    assert all(c["vision_advisory"] is not None for c in c1["candidates"][2:])
    assert c1["master"]["media_id"] in {c["media_id"] for c in c1["candidates"][2:]}
    assert c1["master"]["why"].startswith("VISION (advisory):")

    # c2: the failing candidate never competes; the survivor is the master.
    assert [c["lint"]["verdict"] for c in c2["candidates"]] == ["fail", "pass"]
    assert c2["master"]["media_id"] == c2["candidates"][1]["media_id"]
    assert c2["master"]["why"] == "The only candidate that passed image lint."
    assert c2["candidates"][0]["vision_advisory"] is None

    # Search negatives on every request; the strengthened ones on c1's retry only.
    prompts = [post["prompt"] for post in provider.image_posts]
    assert len(prompts) == 3
    assert all("no text, no logos, no watermark" in p for p in prompts)
    assert [STRENGTHENED_NEGATIVES[0] in p for p in prompts] == [False, True, False]
    assert all(post["n"] == 2 and post["aspect_ratio"] == "1:1" for post in provider.image_posts)

    # The master leaves draft with its passing LintResult at the pin; the rest stay candidates.
    asset = await db.get(CreativeAsset, uuid.UUID(c1["asset_id"]))
    assert asset is not None
    assert asset.status is CreativeAssetStatus.LINTED
    assert asset.lint is not None and asset.lint["verdict"] == "pass"
    assert asset.fields["master_media_id"] == c1["master"]["media_id"]
    roles = (
        await db.execute(
            sa.select(MediaArtifact.id, MediaArtifact.role).where(
                MediaArtifact.asset_id == asset.id
            )
        )
    ).all()
    assert sorted(role.value for _, role in roles) == ["candidate"] * 3 + ["master"]
    assert {str(i) for i, r in roles if r is MediaArtifactRole.MASTER} == {c1["master"]["media_id"]}


async def test_every_candidate_failing_twice_is_one_retry_then_a_gap(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    storage: LocalStorage,
) -> None:
    images = _images(FAIL, FAIL, FAIL, FAIL, PASS, PASS)
    out, provider, _ = await _masters(
        admin, db, workspace_id, project_id, admin_user.id, monkeypatch, images
    )
    c1, c2 = out["concepts"]

    assert c1["master"] is None
    assert c1["gap"]["reason"] == "all_candidates_failed_lint"
    assert len(c1["candidates"]) == 4  # the first attempt and exactly one retry
    assert provider.vision_calls() and len(provider.vision_calls()) == 1  # c2's ranking only
    asset = await db.get(CreativeAsset, uuid.UUID(c1["asset_id"]))
    assert asset is not None and asset.status is CreativeAssetStatus.DRAFT
    assert c2["master"] is not None


async def test_a_third_party_reference_is_never_sent_without_a_cleared_image_right(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    storage: LocalStorage,
) -> None:
    own = await seed_reference(db, storage, workspace_id, project_id, admin_user.id)
    third = await seed_reference(
        db, storage, workspace_id, project_id, admin_user.id,
        origin=MediaReferenceOrigin.THIRD_PARTY, colour=(200, 10, 10),
    )  # fmt: skip
    out, provider, _ = await _masters(
        admin, db, workspace_id, project_id, admin_user.id, monkeypatch,
        _images(PASS, PASS, PASS, PASS), refs=[own, third], allowed=True,
    )  # fmt: skip

    own_url = f"data:image/png;base64,{_b64(storage.get(own.storage_path))}"
    third_b64 = _b64(storage.get(third.storage_path))
    assert provider.image_posts
    for post in provider.image_posts:
        urls = [part["image_url"]["url"] for part in post.get("input_references", [])]
        assert urls == [own_url]  # reference_guided from OWN; THIRD never rides along
        assert third_b64 not in str(post)
    assert all(c["reference_sha256s"] == [own.sha256] for c in out["concepts"])


async def test_a_cleared_image_right_in_this_run_lets_the_third_party_reference_go(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    storage: LocalStorage,
) -> None:
    third = await seed_reference(
        db, storage, workspace_id, project_id, admin_user.id,
        origin=MediaReferenceOrigin.THIRD_PARTY, colour=(200, 10, 10),
    )  # fmt: skip
    out, provider, _ = await _masters(
        admin, db, workspace_id, project_id, admin_user.id, monkeypatch,
        _images(PASS, PASS, PASS, PASS), refs=[third], allowed=True, clear=third,
    )  # fmt: skip

    third_url = f"data:image/png;base64,{_b64(storage.get(third.storage_path))}"
    for post in provider.image_posts:
        assert [p["image_url"]["url"] for p in post["input_references"]] == [third_url]
    assert (await output_of_depiction(db, out)) == "reference_guided"


async def test_an_uncleared_third_party_reference_alone_sends_nothing(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
    storage: LocalStorage,
) -> None:
    third = await seed_reference(
        db, storage, workspace_id, project_id, admin_user.id,
        origin=MediaReferenceOrigin.THIRD_PARTY, colour=(200, 10, 10),
    )  # fmt: skip
    out, provider, _ = await _masters(
        admin, db, workspace_id, project_id, admin_user.id, monkeypatch,
        _images(PASS, PASS, PASS, PASS), refs=[third], allowed=True,
    )  # fmt: skip

    assert provider.image_posts
    assert all("input_references" not in post for post in provider.image_posts)
    assert all(c["reference_sha256s"] == [] for c in out["concepts"])


async def output_of_depiction(db: AsyncSession, masters_out: dict[str, Any]) -> str:
    asset = await db.get(CreativeAsset, uuid.UUID(masters_out["concepts"][0]["asset_id"]))
    assert asset is not None
    return str(asset.fields["product_depiction"])
