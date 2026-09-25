"""S4-P13 — G8 (4.4.5), one regeneration round (4.4.6), G8b (4.4.7), run
through the real executor, the real approval route and the real media pipeline
(Stage 04 PRD §8.5, §11 4.4.5–4.4.7, §16).

Exit criteria (binary), each asserted on rows rather than on a response alone:

* `approve` without all four checklist fields ⇒ 422 naming the asset, gate pending;
* `regenerate` with a non-allowlisted model ⇒ 422 naming the asset;
* only `regenerate` items are regenerated — 4.4.6's jobs all belong to the new
  asset, round 2, and nothing is submitted for an approved item;
* G8b offers no regeneration (422);
* a rejected G8b item is `dropped`;
* draft state survives a reload.

Plus: G8/G8b route to the pinned sign-off matrix's brand owner, and the
operator's `POST /creative-assets/{id}/regenerate` checks model and budget
before its 202 and swaps the new asset onto the pending G8 card.

Runs in the worker image (`exiftool`, `tesseract`), like S4-P10.
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
    AssetDecision,
    CreativeAsset,
    CreativeAssetStatus,
    GenerationJob,
    NodeRun,
    NodeRunStatus,
    Project,
    Run,
    User,
)
from agent.schemas.creative_input import CreativeInput
from agent.storage.local import LocalStorage
from tests.integration.conftest import ADMIN_PASSWORD, ApiClient, build_client, make_member
from tests.integration.runs_support import execute
from tests.integration.s4p9_support import approve_g7, image_model, start_image_run
from tests.integration.test_s4p10_image_renditions import SEARCH, _Painter
from tests.openrouter_fake import FakeOpenRouter

TICKED = {"label_ok": True, "product_match_ok": True, "subjects_ok": True, "rights_ok": True}


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> LocalStorage:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    return LocalStorage(str(tmp_path))


# ---------------------------------------------------------------------------
# harness
# ---------------------------------------------------------------------------


async def _execute(run_id: uuid.UUID) -> None:
    """The media chain and its review — 4.1.1 → 4.4.1–4.4.7 — and only it."""
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
        )


async def _member(admin: ApiClient, db: AsyncSession, role: str) -> tuple[ApiClient, uuid.UUID]:
    email, password = await make_member(admin, role)
    api = build_client()
    assert (await api.login(email, password)).status_code == 200
    user_id = await db.scalar(sa.select(User.id).where(User.email == email))
    assert user_id is not None
    return api, user_id


async def _brand_owner_is(db: AsyncSession, run_id: uuid.UUID, owner: uuid.UUID) -> None:
    """The run's pinned sign-off matrix names `owner` as brand owner (the
    performance owner — G7's — stays the admin)."""
    run = await db.get(Run, run_id, populate_existing=True)
    assert run is not None
    stored = CreativeInput.model_validate(run.creative_input)
    moved = stored.model_copy(
        update={
            "signoff_matrix": stored.signoff_matrix.model_copy(update={"brand_owner_id": owner})
        }
    )
    run.creative_input = moved.model_dump(mode="json", by_alias=True)
    run.input_hash = moved.content_hash()
    await db.commit()


async def _gate(db: AsyncSession, run_id: uuid.UUID, gate_key: str) -> Approval | None:
    return (
        await db.execute(
            sa.select(Approval)
            .where(Approval.run_id == run_id, Approval.gate_key == gate_key)
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


async def _node(db: AsyncSession, run_id: uuid.UUID, node_id: str) -> NodeRun:
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
    assert row is not None, f"{node_id} never ran"
    return row


async def _asset(db: AsyncSession, asset_id: uuid.UUID) -> CreativeAsset:
    row = await db.get(CreativeAsset, asset_id, populate_existing=True)
    assert row is not None
    return row


async def _decisions(db: AsyncSession, approval_id: uuid.UUID) -> list[AssetDecision]:
    rows = await db.execute(
        sa.select(AssetDecision)
        .where(AssetDecision.approval_id == approval_id)
        .execution_options(populate_existing=True)
    )
    return list(rows.scalars().all())


async def _to_g8(
    admin: ApiClient,
    db: AsyncSession,
    ids: tuple[uuid.UUID, uuid.UUID, uuid.UUID],
    router: respx.Router,
    *,
    brand_owner: uuid.UUID | None = None,
) -> tuple[uuid.UUID, Approval]:
    workspace_id, project_id, actor = ids
    run_id = await start_image_run(
        admin, db, workspace_id, project_id, actor, capability=image_model(), specs=SEARCH
    )
    if brand_owner is not None:
        await _brand_owner_is(db, run_id, brand_owner)
    _Painter().install(router)
    await _execute(run_id)
    await approve_g7(admin, db, run_id)
    await _execute(run_id)
    approval = await _gate(db, run_id, "G8")
    assert approval is not None and approval.status is ApprovalStatus.PENDING, (
        await _node(db, run_id, "4.4.5")
    ).error
    return run_id, approval


def _item(asset_id: Any, decision: str, **rest: Any) -> dict[str, Any]:
    return {"asset_id": str(asset_id), "decision": decision, **rest}


# ---------------------------------------------------------------------------
# G8 → 4.4.6 → G8b, end to end
# ---------------------------------------------------------------------------


async def test_g8_regenerates_only_what_it_sends_back_and_g8b_drops_a_rejection(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    owner, owner_id = await _member(admin, db, "approver")
    other, _ = await _member(admin, db, "approver")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        run_id, g8 = await _to_g8(
            admin, db, (workspace_id, project_id, admin_user.id), router, brand_owner=owner_id
        )

        # G8 is the brand owner's (the pinned matrix), not the performance owner's.
        assert g8.assignee_id == owner_id
        card = g8.proposal
        assert card["status"] == "review" and card["round"] == 1
        assert len(card["items"]) == 2
        keep, again = (uuid.UUID(item["asset_id"]) for item in card["items"])
        for item in card["items"]:
            assert item["kind"] == "image" and item["renditions"]
            assert item["decision"] is None
            assert (await _asset(db, uuid.UUID(item["asset_id"]))).status is (
                CreativeAssetStatus.AWAITING_REVIEW
            )
        mastered = {c["asset_id"]: c for c in (await _node(db, run_id, "4.4.2")).output["concepts"]}
        assert (
            card["items"][0]["vision_advisory"]["notes"][0]
            == (mastered[str(keep)]["master"]["why"])
        )

        # Another approver cannot decide the brand owner's gate.
        refused = await other.post(
            f"/approvals/{g8.id}",
            json={"decision": "approve", "edited_proposal": {"items": []}},
        )
        assert refused.status_code == 403, refused.text

        # approve without all four ticks ⇒ 422 naming the asset; nothing written.
        missing = await owner.post(
            f"/approvals/{g8.id}",
            json={
                "decision": "approve",
                "edited_proposal": {
                    "items": [
                        _item(keep, "approve", checklist={**TICKED, "rights_ok": False}),
                        _item(again, "reject"),
                    ]
                },
            },
        )
        assert missing.status_code == 422, missing.text
        assert missing.json()["asset_id"] == str(keep)
        assert missing.json()["field"] == "checklist"
        assert missing.json()["missing"] == ["rights_ok"]

        # regenerate with a model not on the allowlist ⇒ 422 naming the asset.
        stranger = await owner.post(
            f"/approvals/{g8.id}",
            json={
                "decision": "approve",
                "edited_proposal": {
                    "items": [
                        _item(keep, "approve", checklist=TICKED),
                        _item(
                            again,
                            "regenerate",
                            note="Warmer light.",
                            model_override="vendor/not-allowlisted",
                        ),
                    ]
                },
            },
        )
        assert stranger.status_code == 422, stranger.text
        assert stranger.json()["asset_id"] == str(again)
        assert stranger.json()["code"] == "media_model_not_allowlisted"

        g8 = await _gate(db, run_id, "G8")  # type: ignore[assignment]
        assert g8 is not None and g8.status is ApprovalStatus.PENDING
        assert await _decisions(db, g8.id) == []

        decided = await owner.post(
            f"/approvals/{g8.id}",
            json={
                "decision": "approve",
                "edited_proposal": {
                    "items": [
                        _item(keep, "approve", checklist=TICKED),
                        _item(again, "regenerate", note="Warmer light, closer framing."),
                    ]
                },
            },
        )
        assert decided.status_code == 200, decided.text
        rows = {row.asset_id: row for row in await _decisions(db, g8.id)}
        assert {a: (r.decision.value, r.round) for a, r in rows.items()} == {
            keep: ("approve", 1),
            again: ("regenerate", 1),
        }
        assert (
            rows[keep].checklist == TICKED and rows[again].note == "Warmer light, closer framing."
        )
        assert rows[keep].decided_by == owner_id
        assert (await _asset(db, keep)).status is CreativeAssetStatus.APPROVED
        assert (await _asset(db, again)).status is CreativeAssetStatus.REJECTED
        g8_output = (await _node(db, run_id, "4.4.5")).output
        chosen = g8_output["items"][1]["decision"]["regeneration_choice"]
        assert chosen["model_id"] == image_model().model_id

        before = set((await db.execute(sa.select(GenerationJob.id))).scalars().all())
        await _execute(run_id)

    # Only the regenerate item was regenerated: every 4.4.6 job is round 2 of
    # the one new asset, whose lineage names the asset it replaces.
    regenerated = (await _node(db, run_id, "4.4.6")).output
    assert regenerated["status"] == "regenerated"
    assert [i["parent_asset_id"] for i in regenerated["items"]] == [str(again)]
    child_id = uuid.UUID(regenerated["items"][0]["asset_id"])
    child = await _asset(db, child_id)
    assert child.lineage == {
        "origin": "regenerated",
        "parent_id": str(again),
        "by_user": str(owner_id),
        "node_id": "4.4.6",
    }
    assert child.status is CreativeAssetStatus.AWAITING_REVIEW
    assert child.fields["regeneration"]["round"] == 2
    new_jobs = (
        (
            await db.execute(
                sa.select(GenerationJob)
                .where(GenerationJob.id.not_in(before))
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    assert new_jobs, "4.4.6 submitted nothing"
    assert {(job.node_id, job.asset_id, job.round) for job in new_jobs} == {("4.4.6", child_id, 2)}

    # G8b: the regenerated asset only, and no regeneration offered.
    g8b = await _gate(db, run_id, "G8b")
    assert g8b is not None and g8b.status is ApprovalStatus.PENDING
    assert g8b.assignee_id == owner_id
    assert [i["asset_id"] for i in g8b.proposal["items"]] == [str(child_id)]
    assert g8b.proposal["items"][0]["regenerated_from"] == str(again)
    second = await owner.post(
        f"/approvals/{g8b.id}",
        json={
            "decision": "approve",
            "edited_proposal": {"items": [_item(child_id, "regenerate", note="Once more.")]},
        },
    )
    assert second.status_code == 422, second.text
    assert (second.json()["asset_id"], second.json()["field"]) == (str(child_id), "decision")

    # A rejected G8b item is dropped.
    final = await owner.post(
        f"/approvals/{g8b.id}",
        json={
            "decision": "approve",
            "edited_proposal": {"items": [_item(child_id, "reject", note="Still off-brief.")]},
        },
    )
    assert final.status_code == 200, final.text
    assert (await _asset(db, child_id)).status is CreativeAssetStatus.DROPPED
    assert [(r.asset_id, r.decision.value, r.round) for r in await _decisions(db, g8b.id)] == [
        (child_id, "reject", 2)
    ]
    assert (await _node(db, run_id, "4.4.7")).status is NodeRunStatus.SUCCEEDED
    for api in (owner, other):
        await api.raw.aclose()


async def test_nothing_sent_back_makes_4_4_6_and_g8b_not_required(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        run_id, g8 = await _to_g8(admin, db, (workspace_id, project_id, admin_user.id), router)
        items = [_item(i["asset_id"], "approve", checklist=TICKED) for i in g8.proposal["items"]]
        items[-1] = _item(items[-1]["asset_id"], "reject", note="Off-brief.")
        decided = await admin.post(
            f"/approvals/{g8.id}", json={"decision": "approve", "edited_proposal": {"items": items}}
        )
        assert decided.status_code == 200, decided.text
        jobs_before = await db.scalar(sa.select(sa.func.count(GenerationJob.id)))
        await _execute(run_id)

    assert (await _node(db, run_id, "4.4.6")).output["status"] == "not_required"
    assert (await _node(db, run_id, "4.4.7")).output["status"] == "not_required"
    assert await _gate(db, run_id, "G8b") is None
    assert await db.scalar(sa.select(sa.func.count(GenerationJob.id))) == jobs_before
    run = await db.get(Run, run_id, populate_existing=True)
    assert run is not None and run.status.value == "succeeded"


# ---------------------------------------------------------------------------
# the draft
# ---------------------------------------------------------------------------


async def test_draft_state_survives_a_reload_and_only_a_decider_may_save_one(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    owner, owner_id = await _member(admin, db, "approver")
    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        run_id, g8 = await _to_g8(
            admin, db, (workspace_id, project_id, admin_user.id), router, brand_owner=owner_id
        )
    first, second = (i["asset_id"] for i in g8.proposal["items"])
    draft = {
        "items": [
            {"asset_id": first, "decision": "approve", "checklist": {**TICKED, "rights_ok": False}},
            {"asset_id": second, "decision": "regenerate", "note": "Brighter."},
        ],
        "cursor": second,
    }
    saved = await owner.put(f"/approvals/{g8.id}/draft", json={"draft_state": draft})
    assert saved.status_code == 200, saved.text

    # A reload: a fresh sign-in, the inbox read again.
    email = await db.scalar(sa.select(User.email).where(User.id == owner_id))
    reloaded = build_client()
    assert (await reloaded.login(str(email), ADMIN_PASSWORD)).status_code == 200
    inbox = await reloaded.get("/approvals", params={"run_id": str(run_id), "status": "pending"})
    assert inbox.status_code == 200, inbox.text
    (card,) = [a for a in inbox.json()["items"] if a["gate_key"] == "G8"]
    kept = card["draft_state"]
    assert kept["items"] == [
        {
            "asset_id": first,
            "decision": "approve",
            "checklist": {**TICKED, "rights_ok": False},
            "note": None,
            "model_override": None,
            "params_override": None,
        },
        {
            "asset_id": second,
            "decision": "regenerate",
            "checklist": dict.fromkeys(TICKED, False),
            "note": "Brighter.",
            "model_override": None,
            "params_override": None,
        },
    ]
    assert kept["cursor"] == second and kept["saved_by"] == str(owner_id)
    stored = await _gate(db, run_id, "G8")
    assert stored is not None and stored.draft_state == kept
    assert stored.status is ApprovalStatus.PENDING and await _decisions(db, g8.id) == []

    # Somebody who may not decide the gate may not save its draft either.
    operator, _ = await _member(admin, db, "operator")
    assert (
        await operator.put(f"/approvals/{g8.id}/draft", json={"draft_state": draft})
    ).status_code == 403
    # A draft naming an asset not on the card is refused, and leaves the old one.
    stray = await owner.put(
        f"/approvals/{g8.id}/draft",
        json={"draft_state": {"items": [{"asset_id": str(uuid.uuid4())}]}},
    )
    assert stray.status_code == 422, stray.text
    assert (await _gate(db, run_id, "G8")).draft_state == kept  # type: ignore[union-attr]
    for api in (owner, operator, reloaded):
        await api.raw.aclose()


# ---------------------------------------------------------------------------
# an operator's regeneration before G8
# ---------------------------------------------------------------------------


async def test_regenerate_before_g8_checks_model_and_budget_then_takes_the_old_ones_place(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    storage: LocalStorage,
) -> None:
    from agent.orchestrator.regeneration import run_regeneration

    with respx.mock(assert_all_mocked=True, assert_all_called=False) as router:
        run_id, g8 = await _to_g8(admin, db, (workspace_id, project_id, admin_user.id), router)
        old = uuid.UUID(g8.proposal["items"][0]["asset_id"])
        url = f"/creative-assets/{old}/regenerate"

        viewer, _ = await _member(admin, db, "viewer")
        assert (await viewer.post(url, json={"note": "x"})).status_code == 403

        stranger = await admin.post(
            url, json={"note": "Try another.", "model_override": "vendor/not-allowlisted"}
        )
        assert stranger.status_code == 422, stranger.text
        assert stranger.json()["asset_id"] == str(old)
        assert stranger.json()["code"] == "media_model_not_allowlisted"

        project = await db.get(Project, project_id, populate_existing=True)
        assert project is not None
        project.settings = {**(project.settings or {}), "max_media_cost_usd": "0.0001"}
        await db.commit()
        over = await admin.post(url, json={"note": "Warmer."})
        assert over.status_code == 409, over.text
        assert over.json()["code"] == "estimate_exceeds_budget"
        project.settings = {k: v for k, v in project.settings.items() if k != "max_media_cost_usd"}
        await db.commit()
        assert (
            await db.scalar(
                sa.select(sa.func.count(CreativeAsset.id)).where(CreativeAsset.node_id == "4.4.6")
            )
            == 0
        )

        accepted = await admin.post(url, json={"note": "Warmer light."})
        assert accepted.status_code == 202, accepted.text
        body = accepted.json()
        new = uuid.UUID(body["asset_id"])
        assert body["parent_asset_id"] == str(old) and float(body["estimate_usd"]) > 0
        child = await _asset(db, new)
        assert child.fields["state"] == "running" and child.fields["regeneration"]["round"] == 1
        assert child.lineage["origin"] == "regenerated" and child.lineage["parent_id"] == str(old)

        # G8 waits for it: the card is about to change.
        items = [_item(i["asset_id"], "reject") for i in g8.proposal["items"]]
        waiting = await admin.post(
            f"/approvals/{g8.id}", json={"decision": "approve", "edited_proposal": {"items": items}}
        )
        assert waiting.status_code == 422, waiting.text
        assert (waiting.json()["asset_id"], waiting.json()["code"]) == (
            str(old),
            "regeneration_in_progress",
        )

        result = await run_regeneration(new)
        assert result["state"] == "regenerated", result

    card = await _gate(db, run_id, "G8")
    assert card is not None and card.status is ApprovalStatus.PENDING
    ids = [i["asset_id"] for i in card.proposal["items"]]
    assert ids[0] == str(new) and str(old) not in ids
    assert card.proposal["items"][0]["regenerated_from"] == str(old)
    assert (await _asset(db, old)).status is CreativeAssetStatus.DROPPED
    assert (await _asset(db, new)).status is CreativeAssetStatus.AWAITING_REVIEW
    assert (await _node(db, run_id, "4.4.5")).output == card.proposal
    jobs = (
        await db.execute(
            sa.select(GenerationJob.node_id, GenerationJob.asset_id, GenerationJob.round).where(
                GenerationJob.node_id == "4.4.6"
            )
        )
    ).all()
    assert jobs and {tuple(j) for j in jobs} == {("4.4.6", new, 1)}

    gone = await admin.post(url, json={"note": "Again."})
    assert gone.status_code == 409 and gone.json()["code"] == "not_on_card"
    await viewer.raw.aclose()
