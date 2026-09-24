"""The media routes (PRD §16 "Media", §23.1 items 9–10) against the recorded catalogue.

Every app a test builds reads the catalogue from `tests/media/replay.py` —
recorded bodies over an `httpx.MockTransport`, no network — so `REPLAY.calls`
is exactly what reached "OpenRouter".
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Evidence, GenerationJob, PlanCalc, Project, Workspace
from tests.integration.conftest import ApiClient, make_member
from tests.integration.creative_support import seed_both, seed_plan, seed_published, seed_signoff
from tests.media.openrouter_mock import FLUX, GEMINI, VEO
from tests.media.replay import REPLAY

ALLOWLIST = {
    "image": [
        {"model_id": FLUX, "provider_tag": "black-forest-labs", "enabled": True},
        {"model_id": GEMINI, "provider_tag": None, "enabled": True},
    ],
    "video": [{"model_id": VEO, "provider_tag": None, "enabled": True}],
}
MEDIA_SCOPE = {"images": True, "video": True, "concepts_per_campaign": 2}
SELECTIONS = [
    {"modality": "image", "model_id": FLUX, "provider_tag": "black-forest-labs", "defaults": {}},
    {
        "modality": "video",
        "model_id": VEO,
        "defaults": {"duration": 4, "resolution": "720p", "generate_audio": False},
    },
]


#: A Performance Max spec sheet: three image ratios, three video ratios and a
#: logo (fitted by padding, never generated — so never priced).
SPECS = {
    "pmax": {
        name: {"ratio": ratio, "source": "google", "reviewed_at": "2026-09-24"}
        for name, ratio in (
            ("landscape_image", "1.91:1"),
            ("square_image", "1:1"),
            ("portrait_image", "4:5"),
            ("landscape_video", "16:9"),
            ("portrait_video", "9:16"),
            ("square_video", "1:1"),
            ("logo", "1:1"),
        )
    }
}


async def _seed_media_ready(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """`seed_both`, plus what pricing needs: a campaign type and its spec sheet."""
    await seed_plan(db, workspace_id, project_id, admin_user.id, campaign_type="pmax")
    await seed_published(db, workspace_id, project_id, admin_user.id, asset_specs=SPECS)
    await seed_signoff(db, workspace_id, project_id, admin_user.id)
    await db.commit()


async def _expire_catalogue_cache() -> None:
    """Ten minutes pass: the fresh catalogue keys are gone, last-good stays."""
    from agent.redis_client import get_redis

    redis = get_redis()
    async for key in redis.scan_iter(match="media:catalogue:*"):
        if b"last-good" not in key:
            await redis.delete(key)


async def _allowlist(admin: ApiClient, allowlist: dict[str, Any] = ALLOWLIST) -> None:
    response = await admin.put("/settings/media", json={"media_allowlist": allowlist})
    assert response.status_code == 200, response.text


async def _as(admin: ApiClient, second_client: ApiClient, role: str) -> ApiClient:
    email, password = await make_member(admin, role)
    assert (await second_client.login(email, password)).status_code == 200
    return second_client


# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------


async def test_an_admin_can_put_an_allowlist_and_an_operator_gets_403(
    admin: ApiClient, second_client: ApiClient
) -> None:
    operator = await _as(admin, second_client, "operator")

    denied = await operator.put("/settings/media", json={"media_allowlist": ALLOWLIST})
    await _allowlist(admin)
    read = await operator.get("/settings/media")

    assert denied.status_code == 403
    assert read.status_code == 200
    assert read.json()["media_allowlist"]["video"] == [
        {"model_id": VEO, "provider_tag": None, "enabled": True}
    ]


async def test_nothing_is_allowlisted_by_default(admin: ApiClient) -> None:
    body = (await admin.get("/settings/media")).json()

    assert body["media_allowlist"] == {"image": [], "video": []}


async def test_an_allowlist_entry_must_exist_in_the_live_catalogue(admin: ApiClient) -> None:
    missing = await admin.put(
        "/settings/media",
        json={"media_allowlist": {"image": [{"model_id": "acme/no-such-model"}], "video": []}},
    )
    wrong_tag = await admin.put(
        "/settings/media",
        json={"media_allowlist": {"image": [{"model_id": FLUX, "provider_tag": "nobody"}]}},
    )
    video_pin = await admin.put(
        "/settings/media",
        json={"media_allowlist": {"video": [{"model_id": VEO, "provider_tag": "google-vertex"}]}},
    )

    assert missing.status_code == 422 and missing.json()["code"] == "media_model_unavailable"
    assert wrong_tag.status_code == 422 and wrong_tag.json()["code"] == "media_model_unavailable"
    assert video_pin.status_code == 422 and video_pin.json()["code"] == "media_model_unavailable"


# ---------------------------------------------------------------------------
# models and catalogue
# ---------------------------------------------------------------------------


async def test_media_models_is_the_allowlist_intersected_with_the_live_catalogue(
    admin: ApiClient,
) -> None:
    await _allowlist(admin)
    REPLAY.removed.add(GEMINI)  # dropped from the catalogue after it was allowlisted
    await _expire_catalogue_cache()

    body = (await admin.get("/media/models?modality=image")).json()

    rows = {row["model_id"]: row for row in body["models"]}
    assert rows[FLUX]["available"] is True
    assert rows[FLUX]["provider_tag"] == "black-forest-labs"
    assert rows[FLUX]["capability"]["pricing"][0]["unit"] == "megapixel"
    assert len(rows[FLUX]["capability_hash"]) == 64
    assert rows[GEMINI]["available"] is False
    assert rows[GEMINI]["reason"] == "media_model_unavailable"


async def test_the_full_catalogue_is_for_settings_writers(
    admin: ApiClient, second_client: ApiClient
) -> None:
    operator = await _as(admin, second_client, "operator")

    denied = await operator.get("/media/catalogue?modality=video")
    listed = await admin.get("/media/catalogue?modality=video")

    assert denied.status_code == 403
    body = listed.json()
    assert len(body["models"]) == 29 and len(body["catalogue_hash"]) == 64
    assert body["warning"] is None


async def test_a_catalogue_outage_with_no_snapshot_is_a_503_naming_it(admin: ApiClient) -> None:
    REPLAY.fail_with = 503

    response = await admin.get("/media/catalogue?modality=image")

    assert response.status_code == 503
    assert response.json()["code"] == "catalogue_unavailable"


# ---------------------------------------------------------------------------
# project settings
# ---------------------------------------------------------------------------


async def test_project_media_settings_validate_the_model_and_its_defaults(
    admin: ApiClient, db: AsyncSession, project_id: uuid.UUID
) -> None:
    await _allowlist(admin)
    path = f"/projects/{project_id}/settings/media"

    unlisted = await admin.patch(
        path, json={"media_models": {"video": {"model_id": "x-ai/grok-imagine-video"}}}
    )
    bad_ratio = await admin.patch(
        path,
        json={"media_models": {"video": {"model_id": VEO, "defaults": {"aspect_ratio": "1:1"}}}},
    )
    good = await admin.patch(
        path,
        json={
            "media_models": {"video": {"model_id": VEO, "defaults": {"duration": 4}}},
            "media_references_allowed": True,
            "max_media_cost_usd": "12.50",
        },
    )

    assert unlisted.status_code == 422 and unlisted.json()["code"] == "media_model_not_allowlisted"
    assert bad_ratio.status_code == 422
    assert bad_ratio.json()["code"] == "capability_unsupported"
    assert bad_ratio.json()["field"] == "aspect_ratio"
    assert bad_ratio.json()["supported"] == ["16:9", "9:16"]
    assert good.status_code == 200, good.text
    project = await db.get(Project, project_id)
    assert project is not None
    await db.refresh(project)
    assert project.settings["media_models"] == {
        "video": {"model_id": VEO, "provider_tag": None, "defaults": {"duration": 4}}
    }
    assert project.settings["media_references_allowed"] is True
    assert project.settings["max_media_cost_usd"] == "12.50"


# ---------------------------------------------------------------------------
# the estimate
# ---------------------------------------------------------------------------


async def test_the_estimate_returns_its_breakdown_and_writes_no_generation_job(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    await _seed_media_ready(db, workspace_id, project_id, admin_user)
    await _allowlist(admin)

    response = await admin.post(
        f"/projects/{project_id}/creative/estimate",
        json={"scope": MEDIA_SCOPE, "media_models": SELECTIONS},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) >= {
        "text_usd",
        "image_usd",
        "video_usd",
        "total_usd",
        "confidence",
        "calc_evidence_id",
    }
    assert body["total_usd"] == round(body["text_usd"] + body["image_usd"] + body["video_usd"], 4)
    # Two pmax campaigns. Each: flux at 1:1 (the relaid master) x 2 concepts
    # x 2 candidates = 4 images at $0.014680064; veo-lite 16:9 + 9:16
    # relaid, 1:1 a gap = 2 clips at $0.12.
    assert body["jobs"] == {"image": 8, "video": 4}
    assert body["image_usd"] == 0.1174
    assert body["video_usd"] == 0.48
    assert body["ratio_plan"]["image"]["1.91:1"] == {
        "plan": "crop",
        "from": "16:9",
        "retained": 0.9308,
    }
    assert body["ratio_plan"]["video"]["1:1"]["plan"] == "gap"
    evidence = await db.get(Evidence, uuid.UUID(body["calc_evidence_id"]))
    assert evidence is not None and evidence.kind == "calc_media_cost"
    calcs = (await db.scalars(sa.select(PlanCalc.formula_id))).all()
    assert set(calcs) == {"media.cost_estimate_v1", "media.ratio_plan_v1"}
    assert (await db.scalar(sa.select(sa.func.count()).select_from(GenerationJob))) == 0


async def test_over_a_cap_the_estimate_offers_the_scope_reduction_a_start_request_can_carry(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    await _seed_media_ready(db, workspace_id, project_id, admin_user)
    await _allowlist(admin)
    await _defaults(db, project_id, {}, max_media_cost_usd="0.40")
    scope = {**MEDIA_SCOPE, "campaign_refs": []}

    over = await admin.post(
        f"/projects/{project_id}/creative/estimate",
        json={"scope": scope, "media_models": SELECTIONS},
    )

    assert over.status_code == 200, over.text
    body = over.json()
    assert body["fits"] is False
    # The ladder's own answer leads with a rung no request can say...
    assert body["reduction"]["steps"] == ["candidates", "video"]
    # ...so the dialog's one click is the scope-only walk: drop video.
    offered = body["scope_reduction"]
    assert offered["steps"] == ["video"]
    assert offered["fits"] is True
    assert offered["scope"] == {**scope, "video": False}
    assert offered["media_usd"] == 0.1174

    # Taking it, exactly as offered, is an estimate that fits and offers nothing.
    taken = await admin.post(
        f"/projects/{project_id}/creative/estimate",
        json={
            "scope": offered["scope"],
            "media_models": [s for s in SELECTIONS if s["modality"] == "image"],
        },
    )

    assert taken.status_code == 200, taken.text
    assert taken.json()["fits"] is True
    assert taken.json()["scope_reduction"] is None
    assert taken.json()["total_usd"] == offered["total_usd"]


async def test_an_unsupported_aspect_ratio_is_our_422_and_no_request_reaches_the_mock(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    await _allowlist(admin)
    await admin.get("/media/models?modality=video")  # the catalogue is now cached
    REPLAY.calls.clear()
    selection = {"modality": "video", "model_id": VEO, "defaults": {"aspect_ratio": "1:1"}}

    estimate = await admin.post(
        f"/projects/{project_id}/creative/estimate",
        json={"scope": {**MEDIA_SCOPE, "images": False}, "media_models": [selection]},
    )
    start = await admin.post(
        f"/projects/{project_id}/creative/runs",
        json={"scope": {**MEDIA_SCOPE, "images": False}, "media_models": [selection]},
    )

    for response in (estimate, start):
        assert response.status_code == 422, response.text
        problem = response.json()
        assert problem["code"] == "capability_unsupported"
        assert problem["field"] == "aspect_ratio"
        assert problem["supported"] == ["16:9", "9:16"]
    assert REPLAY.calls == []
    assert (await db.scalar(sa.select(sa.func.count()).select_from(GenerationJob))) == 0


# ---------------------------------------------------------------------------
# eligibility: CR-E8, CR-E9
# ---------------------------------------------------------------------------


async def _eligibility(client: ApiClient, project_id: uuid.UUID) -> dict[str, Any]:
    response = await client.get(f"/projects/{project_id}/creative/eligibility")
    assert response.status_code == 200, response.text
    return dict(response.json())


async def _defaults(
    db: AsyncSession, project_id: uuid.UUID, media: dict[str, Any], **extra: Any
) -> None:
    project = await db.get(Project, project_id)
    assert project is not None
    project.settings = {**(project.settings or {}), "media_models": media, **extra}
    await db.commit()


async def test_a_default_video_model_off_the_allowlist_blocks_naming_video(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    await _allowlist(admin, {"image": ALLOWLIST["image"], "video": []})
    await _defaults(db, project_id, {"video": {"model_id": VEO}})

    body = await _eligibility(admin, project_id)

    blockers = [b for b in body["blockers"] if b["code"] == "media_model_not_allowlisted"]
    assert len(blockers) == 1
    assert "video" in blockers[0]["detail"].lower()
    assert blockers[0]["modality"] == "video"


async def test_a_default_model_the_catalogue_dropped_is_unavailable(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    await _allowlist(admin)
    await _defaults(db, project_id, {"image": {"model_id": GEMINI}})
    REPLAY.removed.add(GEMINI)
    await _expire_catalogue_cache()

    body = await _eligibility(admin, project_id)

    assert {b["code"] for b in body["blockers"]} == {"media_model_unavailable"}
    assert body["blockers"][0]["modality"] == "image"


async def test_an_estimate_over_the_media_cap_blocks_with_the_reduction_that_fits(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    await _seed_media_ready(db, workspace_id, project_id, admin_user)
    await _allowlist(admin)
    await _defaults(
        db,
        project_id,
        {
            "image": {"model_id": FLUX, "provider_tag": "black-forest-labs"},
            "video": {
                "model_id": VEO,
                "defaults": {"duration": 8, "resolution": "1080p", "generate_audio": True},
            },
        },
        max_media_cost_usd="0.01",
    )

    body = await _eligibility(admin, project_id)

    over = [b for b in body["blockers"] if b["code"] == "estimate_exceeds_cap"]
    assert len(over) == 1
    assert over[0]["estimate"]["media_usd"] > 0.01
    assert over[0]["cap"] == {"max_creative_cost_usd": 50.0, "max_media_cost_usd": 0.01}
    assert "reduction" in over[0]
    assert body["estimate"]["total_usd"] > 0


async def test_a_workspace_that_enforces_zdr_blocks_an_allowlisted_video(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    await _allowlist(admin)
    await _defaults(db, project_id, {"video": {"model_id": VEO, "defaults": {"duration": 4}}})
    workspace = await db.get(Workspace, workspace_id)
    assert workspace is not None
    await db.refresh(workspace)
    workspace.settings = {**(workspace.settings or {}), "zdr_enforced": True}
    await db.commit()

    body = await _eligibility(admin, project_id)

    assert {b["code"] for b in body["blockers"]} == {"zdr_blocks_video"}


async def test_an_allowlisted_media_run_is_eligible_and_its_input_pins_the_capability(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    from agent.orchestrator.creative_input import build_creative_input, resolve_media_models
    from agent.schemas.creative_input import CreativeScope
    from tests.media.replay import replay_catalogue

    await _seed_media_ready(db, workspace_id, project_id, admin_user)
    await _allowlist(admin)
    await _defaults(
        db, project_id, {"image": {"model_id": FLUX, "provider_tag": "black-forest-labs"}}
    )

    assert (await _eligibility(admin, project_id))["eligible"] is True
    scope = CreativeScope(images=True, video=False, concepts_per_campaign=2)
    from agent.api.schemas_media import MediaModelSelection

    choices = await resolve_media_models(
        db,
        workspace_id,
        scope,
        [MediaModelSelection(modality="image", model_id=FLUX, provider_tag="black-forest-labs")],
        catalogue=replay_catalogue(),
    )
    built, _ = await build_creative_input(
        db, project_id, scope, choices, workspace_id=workspace_id, creative_run_id=uuid.uuid4()
    )
    (pinned,) = built.media_models
    assert pinned.model_id == FLUX and pinned.capability["pricing"][0]["unit"] == "megapixel"
    assert len(pinned.capability_hash) == 64


async def test_workspace_defaults_apply_under_a_project_choice_where_the_model_takes_them(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID
) -> None:
    from agent.api.schemas_media import MediaModelSelection
    from agent.orchestrator.creative_input import resolve_media_models
    from agent.schemas.creative_input import CreativeScope
    from tests.media.replay import replay_catalogue

    await _allowlist(admin)
    saved = await admin.put(
        "/settings/media",
        json={"media_defaults": {"video": {"resolution": "720p", "size": "640x480"}, "image": {}}},
    )
    assert saved.status_code == 200, saved.text

    (choice,) = await resolve_media_models(
        db,
        workspace_id,
        CreativeScope(images=False, video=True, concepts_per_campaign=2),
        [MediaModelSelection(modality="video", model_id=VEO, defaults={"duration": 4})],
        catalogue=replay_catalogue(),
    )

    # 720p applies; veo-lite has no 640x480, so that workspace default does
    # not apply to it — and the project's own duration wins over neither.
    assert choice.defaults == {"resolution": "720p", "duration": 4}
