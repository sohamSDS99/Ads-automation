"""S4-P9 — 4.4.1 `creative_concepts`, run through the executor (PRD §11, §13, Law 38).

`product_depiction` is resolved in code from capability × references × Law 44 —
against live rows, at 4.4.1 — and the model is never asked for it. Every
search-surface prompt carries "no text, no logos, no watermark".
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import respx
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import MediaReferenceOrigin
from agent.storage.local import LocalStorage
from tests.integration.conftest import ApiClient
from tests.integration.s4p9_support import (
    Provider,
    approve_g7,
    image_model,
    output_of,
    png,
    run_until_done,
    seed_reference,
    set_references_allowed,
    start_image_run,
)

PLENTY = [png((200, 200, 200 - i)) for i in range(8)]


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


async def _concepts(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    storage: LocalStorage,
    *,
    origin: MediaReferenceOrigin,
    allowed: bool,
    disallow_after_g7: bool = False,
) -> tuple[dict[str, Any], Provider, uuid.UUID]:
    ref = await seed_reference(db, storage, workspace_id, project_id, actor, origin=origin)
    run_id = await start_image_run(
        admin, db, workspace_id, project_id, actor,
        capability=image_model(), refs=[ref], allowed=allowed,
    )  # fmt: skip
    provider = Provider(PLENTY)
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        provider.install(router)
        await run_until_done(run_id)
        await approve_g7(admin, db, run_id)
        if disallow_after_g7:
            await set_references_allowed(db, project_id, False)
        await run_until_done(run_id)
    return await output_of(db, run_id, "4.4.1"), provider, ref.id


async def test_reference_guided_is_resolved_in_code_and_search_prompts_carry_the_negatives(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    out, _, ref_id = await _concepts(
        admin, db, workspace_id, project_id, admin_user.id, storage,
        origin=MediaReferenceOrigin.OWN, allowed=True,
    )  # fmt: skip

    assert out["product_depiction"] == "reference_guided"
    assert out["reference_refusals"] == {str(ref_id): None}
    (campaign,) = out["campaigns"]
    assert campaign["campaign_ref"] == "c-sds-us"
    assert [c["id"] for c in campaign["concepts"]] == ["c-sds-us:c1", "c-sds-us:c2"]
    for concept in campaign["concepts"]:
        assert concept["product_depiction"] == "reference_guided"
        assert concept["surfaces"] == ["search_image"]
        assert "no text, no logos, no watermark" in concept["prompt"]
        assert concept["negative_constraints"][0] == "no text, no logos, no watermark"
        assert set(concept["composition_by_ratio"]) == {"1:1"}


async def test_an_uncleared_third_party_reference_resolves_composited_real(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    out, _, ref_id = await _concepts(
        admin, db, workspace_id, project_id, admin_user.id, storage,
        origin=MediaReferenceOrigin.THIRD_PARTY, allowed=True,
    )  # fmt: skip

    assert out["product_depiction"] == "composited_real"
    assert out["reference_refusals"] == {str(ref_id): "third_party_without_image_right"}
    for concept in out["campaigns"][0]["concepts"]:
        assert concept["product_depiction"] == "composited_real"
        assert any(
            n.startswith("do not depict the product") for n in concept["negative_constraints"]
        )


async def test_turning_references_off_after_g7_is_honoured_at_4_4_1(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    out, _, ref_id = await _concepts(
        admin, db, workspace_id, project_id, admin_user.id, storage,
        origin=MediaReferenceOrigin.OWN, allowed=True, disallow_after_g7=True,
    )  # fmt: skip

    assert out["product_depiction"] == "composited_real"
    assert out["reference_refusals"] == {str(ref_id): "references_not_allowed"}


async def test_the_model_is_never_asked_for_a_product_depiction(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    _, provider, _ = await _concepts(
        admin, db, workspace_id, project_id, admin_user.id, storage,
        origin=MediaReferenceOrigin.OWN, allowed=True,
    )  # fmt: skip

    concept_calls = [
        c for c in provider.chat
        if "ConceptsDraft" in json.dumps(c["response_format"]["json_schema"])
    ]  # fmt: skip
    assert len(concept_calls) == 1  # one campaign in scope
    assert "product_depiction" not in json.dumps(concept_calls[0]["response_format"])
