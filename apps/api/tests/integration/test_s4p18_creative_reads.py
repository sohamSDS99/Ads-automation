"""S4-P18's API half, on a real stack: the reads the Creative Console and the
brief page are built on, and "Check again" (Stage 04 PRD §15.4 C–D, §16).

1. The brief view carries the brief on record, its hash, the server's word
   count and what approving it authorises; G7's `can_decide` stays on the
   approvals surface and is true only for whoever may act.
2. Deciding through the approvals surface is what the view then shows —
   the stamped hash, or an edit re-hashed.
3. Generation jobs list estimate beside actual, and `can_check` says where
   "Check again" would do anything, for whom.
4. "Check again" refuses what it cannot act on, queues what it can, and the
   worker's re-poll takes a timed-out video to `completed` with its cost.
5. The run's two spend meters: spent and reserved, against each cap.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import respx
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import get_settings
from agent.creative import g7
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    GenerationJob,
    GenerationModality,
    GenerationStatus,
    Run,
    RunStage,
)
from agent.media.budget import BudgetCaps, MediaBudget, resolve_media_caps
from agent.redis_client import get_redis
from agent.schemas.creative_brief import MAX_RENDERED_WORDS, CreativeBrief
from agent.schemas.creative_input import CreativeInput
from agent.worker import check_generation_job
from tests.integration.conftest import ApiClient
from tests.integration.creative_support import _run
from tests.integration.runs_support import execute
from tests.integration.test_s4p4_brief_g7 import _approver, _brief_row, _g7, _scripted, _start
from tests.media.openrouter_mock import mock_video_job, video_job_id

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 25, 9, tzinfo=UTC)


async def _halted_on_g7(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    owner: uuid.UUID | None = None,
) -> uuid.UUID:
    run_id = await _start(admin, db, workspace_id, project_id, actor, performance_owner=owner)
    await execute(run_id, _scripted())
    return run_id


def _job(run: Run, status: GenerationStatus, **fields: Any) -> GenerationJob:
    values: dict[str, Any] = {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "creative_run_id": run.id,
        "node_id": "4.4.4",
        "modality": GenerationModality.VIDEO,
        "model_id": "google/veo-3.1",
        "capability_hash": "c" * 64,
        "request": {"model": "google/veo-3.1", "prompt": "A lab bench, slow push in"},
        "idempotency_key": uuid.uuid4().hex,
        "status": status,
        "estimate_usd": Decimal("0.4800"),
        "created_at": NOW,
    }
    return GenerationJob(**{**values, **fields})


# ---------------------------------------------------------------------------
# 1–2. the brief, and G7 through the approvals surface
# ---------------------------------------------------------------------------


async def test_the_brief_view_is_the_brief_on_record_and_what_it_authorises(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    signed_in_as: Any,
) -> None:
    run_id = await _halted_on_g7(admin, db, workspace_id, project_id, admin_user.id)
    viewer = await signed_in_as("viewer")

    seen = await viewer.get(f"/creative-runs/{run_id}/brief")

    assert seen.status_code == 200, seen.text
    view = seen.json()
    row = await _brief_row(db, run_id)
    brief = CreativeBrief.model_validate(row.payload)
    assert view["brief"] == CreativeBrief.model_validate(view["brief"]).model_dump(mode="json")
    assert view["brief_hash"] == row.brief_hash == view["brief"]["brief_hash"]
    assert view["approved_hash"] is None
    assert view["word_count"] == brief.rendered_word_count
    assert view["max_words"] == MAX_RENDERED_WORDS
    run = await db.get(Run, run_id)
    assert run is not None
    authorised = g7.authorises(brief, CreativeInput.model_validate(run.creative_input))
    assert view["authorises"] == {
        "rsas": authorised.rsas,
        "images": authorised.images,
        "videos": authorised.videos,
        "media_usd": str(authorised.media_usd),
    }
    assert view["approval_id"] == str((await _g7(db, run_id)).id)


async def test_only_whoever_may_act_on_g7_is_told_they_can_decide(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    signed_in_as: Any,
) -> None:
    owner, owner_id = await _approver(admin, db, "perf18@example.com")
    other, _ = await _approver(admin, db, "brand18@example.com")
    try:
        run_id = await _halted_on_g7(
            admin, db, workspace_id, project_id, admin_user.id, owner=owner_id
        )
        clients = {
            "performance owner": owner,
            "another approver": other,
            "operator": await signed_in_as("operator"),
            "viewer": await signed_in_as("viewer"),
            "admin": admin,
        }
        seen = {}
        for who, client in clients.items():
            listed = await client.get("/approvals", params={"run_id": str(run_id)})
            assert listed.status_code == 200, listed.text
            (gate,) = [item for item in listed.json()["items"] if item["gate_key"] == "G7"]
            seen[who] = gate["can_decide"]
        # The admin override is the approvals surface's own rule (PRD §6.1
        # Authorization 3), not something the brief page adds or removes.
        assert seen == {
            "performance owner": True,
            "another approver": False,
            "operator": False,
            "viewer": False,
            "admin": True,
        }
    finally:
        await owner.raw.aclose()
        await other.raw.aclose()


async def test_approving_from_the_owner_shows_the_stamped_hash(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    owner, owner_id = await _approver(admin, db, "perf18b@example.com")
    try:
        run_id = await _halted_on_g7(
            admin, db, workspace_id, project_id, admin_user.id, owner=owner_id
        )
        approval = await _g7(db, run_id)
        decided = await owner.post(f"/approvals/{approval.id}", json={"decision": "approve"})
        assert decided.status_code == 200, decided.text

        view = (await owner.get(f"/creative-runs/{run_id}/brief")).json()
        assert view["approved_hash"] == view["brief_hash"] == approval.proposal["brief_hash"]
        assert view["approval_id"] == str(approval.id)
    finally:
        await owner.raw.aclose()


async def test_an_edit_at_decision_is_what_the_view_serves_re_hashed(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id = await _halted_on_g7(admin, db, workspace_id, project_id, admin_user.id)
    approval = await _g7(db, run_id)
    edited = dict(approval.proposal)
    edited["objective"] = {**edited["objective"], "text": "Win audit-ready SDS teams"}

    decided = await admin.post(
        f"/approvals/{approval.id}", json={"decision": "approve", "edited_proposal": edited}
    )
    assert decided.status_code == 200, decided.text

    view = (await admin.get(f"/creative-runs/{run_id}/brief")).json()
    assert view["brief"]["objective"]["text"] == "Win audit-ready SDS teams"
    assert view["brief_hash"] != approval.proposal["brief_hash"]
    assert view["approved_hash"] == view["brief_hash"] == view["brief"]["brief_hash"]


async def test_a_brief_not_written_yet_says_which_node_writes_it(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id = await _start(admin, db, workspace_id, project_id, admin_user.id)

    missing = await admin.get(f"/creative-runs/{run_id}/brief")

    assert missing.status_code == 404, missing.text
    assert missing.json()["title"] == "No brief yet"
    assert "4.1.1" in missing.json()["detail"]


async def test_another_workspace_or_a_made_up_run_is_not_found(admin: ApiClient) -> None:
    for path in ("brief", "assets", "generation-jobs"):
        missing = await admin.get(f"/creative-runs/{uuid.uuid4()}/{path}")
        assert missing.status_code == 404, (path, missing.text)


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------


async def test_assets_carry_their_stored_lint_verdict_and_filter_by_kind(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    signed_in_as: Any,
) -> None:
    run_id = await _halted_on_g7(admin, db, workspace_id, project_id, admin_user.id)
    run = await db.get(Run, run_id)
    assert run is not None
    common: dict[str, Any] = {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "creative_run_id": run.id,
        "node_id": "4.2.1",
        "campaign_ref": "c-sds",
        "ad_group_ref": "sds software",
        "surface": "rsa_headline",
        "generated_by_ai": True,
        "content_hash": "h" * 64,
    }
    db.add_all(
        [
            CreativeAsset(
                **common,
                kind=CreativeAssetKind.HEADLINE,
                text="SDS updates in 24 hours",
                lint={"verdict": "pass_with_warnings", "findings": []},
                # One transaction's rows share `now()`; the list is ordered by
                # `created_at` and then id, so the order under test is made explicit.
                created_at=NOW,
            ),
            CreativeAsset(
                **{**common, "surface": "rsa_description"},
                kind=CreativeAssetKind.DESCRIPTION,
                text="Every sheet current, every audit ready.",
                lint={"verdict": "pass", "findings": []},
                created_at=NOW + timedelta(seconds=1),
            ),
        ]
    )
    await db.commit()
    viewer = await signed_in_as("viewer")

    every = (await viewer.get(f"/creative-runs/{run_id}/assets")).json()["items"]
    headlines = (
        await viewer.get(f"/creative-runs/{run_id}/assets", params={"kind": "headline"})
    ).json()["items"]

    assert [(item["text"], item["lint_verdict"]) for item in every] == [
        ("SDS updates in 24 hours", "pass_with_warnings"),
        ("Every sheet current, every audit ready.", "pass"),
    ]
    assert [item["kind"] for item in headlines] == ["headline"]


# ---------------------------------------------------------------------------
# 3–4. generation jobs and "Check again"
# ---------------------------------------------------------------------------


async def test_jobs_list_estimate_beside_actual_and_who_may_check_again(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    signed_in_as: Any,
) -> None:
    run_id = await _halted_on_g7(admin, db, workspace_id, project_id, admin_user.id)
    run = await db.get(Run, run_id)
    assert run is not None
    done = _job(
        run,
        GenerationStatus.COMPLETED,
        cost_usd=Decimal("0.5200"),
        polls=6,
        submitted_at=NOW,
        completed_at=NOW + timedelta(seconds=94),
    )
    late = _job(
        run,
        GenerationStatus.TIMED_OUT,
        openrouter_job_id=f"vid-{uuid.uuid4().hex[:8]}",
        polls=31,
        submitted_at=NOW,
        created_at=NOW + timedelta(seconds=1),
    )
    lost = _job(run, GenerationStatus.UNKNOWN_SUBMIT_STATE, created_at=NOW + timedelta(seconds=2))
    db.add_all([done, late, lost])
    await db.commit()

    operator = await signed_in_as("operator")
    viewer = await signed_in_as("viewer")
    as_operator = (await operator.get(f"/creative-runs/{run_id}/generation-jobs")).json()["items"]
    as_viewer = (await viewer.get(f"/creative-runs/{run_id}/generation-jobs")).json()["items"]

    assert [(j["status"], j["estimate_usd"], j["cost_usd"], j["polls"]) for j in as_operator] == [
        ("completed", "0.4800", "0.5200", 6),
        ("timed_out", "0.4800", None, 31),
        ("unknown_submit_state", "0.4800", None, 0),
    ]
    # A video whose submit state is unknown is never re-POSTed (law 37).
    assert [j["can_check"] for j in as_operator] == [False, True, False]
    assert [j["can_check"] for j in as_viewer] == [False, False, False]


async def test_check_again_refuses_what_it_cannot_act_on_and_queues_what_it_can(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    signed_in_as: Any,
) -> None:
    run_id = await _halted_on_g7(admin, db, workspace_id, project_id, admin_user.id)
    run = await db.get(Run, run_id)
    assert run is not None
    done = _job(run, GenerationStatus.COMPLETED, cost_usd=Decimal("0.5200"))
    late = _job(run, GenerationStatus.TIMED_OUT, openrouter_job_id=f"vid-{uuid.uuid4().hex[:8]}")
    db.add_all([done, late])
    await db.commit()

    viewer = await signed_in_as("viewer")
    operator = await signed_in_as("operator")

    assert (await viewer.post(f"/generation-jobs/{late.id}/check")).status_code == 403
    refused = await operator.post(f"/generation-jobs/{done.id}/check")
    assert refused.status_code == 409, refused.text
    assert refused.json()["status"] == "completed"
    assert (await operator.post(f"/generation-jobs/{uuid.uuid4()}/check")).status_code == 404

    first = await operator.post(f"/generation-jobs/{late.id}/check")
    again = await operator.post(f"/generation-jobs/{late.id}/check")
    assert first.status_code == again.status_code == 202, (first.text, again.text)
    assert first.json() == {"job_id": str(late.id), "status": "timed_out", "queued": True}
    assert again.json()["queued"] is False, "a double-click queues one check"


async def test_the_worker_re_polls_a_timed_out_video_to_completed_with_its_cost(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id = await _halted_on_g7(admin, db, workspace_id, project_id, admin_user.id)
    run = await db.get(Run, run_id)
    assert run is not None
    late = _job(
        run,
        GenerationStatus.TIMED_OUT,
        openrouter_job_id=video_job_id(),
        submitted_at=datetime.now(UTC) - timedelta(hours=1),
        polls=31,
    )
    db.add(late)
    await db.commit()

    with respx.mock(assert_all_called=False) as router:
        routes = mock_video_job(router, "video_poll_completed.json")
        result = await check_generation_job({}, str(late.id))

    assert result == {"job_id": str(late.id), "status": "completed"}
    assert routes["poll"].called and routes["content"].called
    assert not routes["submit"].called, "Check again never re-submits a video (law 37)"
    row = (
        await db.execute(
            sa.select(GenerationJob)
            .where(GenerationJob.id == late.id)
            .execution_options(populate_existing=True)
        )
    ).scalar_one()
    assert row.status is GenerationStatus.COMPLETED
    assert row.polls == 32 and row.cost_usd is not None and row.completed_at is not None


# ---------------------------------------------------------------------------
# 5. the two spend meters
# ---------------------------------------------------------------------------


async def test_the_run_carries_both_meters_spent_and_reserved_against_each_cap(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    run_id = await _halted_on_g7(admin, db, workspace_id, project_id, admin_user.id)
    run = await db.get(Run, run_id)
    assert run is not None
    job = _job(run, GenerationStatus.COMPLETED, cost_usd=Decimal("0.5200"))
    db.add(job)
    run.cost_usd = Decimal(run.cost_usd or 0) + Decimal("0.5200")
    await db.commit()
    budget = MediaBudget(get_redis())
    await budget.reserve(
        run_id,
        "video",
        Decimal("1.2000"),
        job_id=uuid.uuid4(),
        caps=_caps(),
        text_spent_usd=Decimal(0),
        media_spent_floor_usd=Decimal("0.5200"),
    )

    detail = (await admin.get(f"/runs/{run_id}")).json()

    spend = {
        meter: {key: Decimal(str(value)) for key, value in line.items()}
        for meter, line in detail["creative_spend"].items()
    }
    caps = resolve_media_caps(
        project_settings=None, workspace_settings=None, defaults=get_settings()
    )
    assert spend["media"] == {
        "spent_usd": Decimal("0.52"),
        "reserved_usd": Decimal("1.2"),
        "cap_usd": caps.max_media_cost_usd,
    }
    assert spend["total"] == {
        "spent_usd": Decimal(str(detail["cost_usd"])),
        "reserved_usd": Decimal("1.2"),
        "cap_usd": caps.max_creative_cost_usd,
    }


def _caps() -> BudgetCaps:
    return resolve_media_caps(
        project_settings=None, workspace_settings=None, defaults=get_settings()
    )


async def test_a_run_of_any_other_stage_carries_no_creative_meters(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    research = _run(workspace_id, project_id, admin_user.id, RunStage.RESEARCH)
    db.add(research)
    await db.commit()

    detail = await admin.get(f"/runs/{research.id}")

    assert detail.status_code == 200, detail.text
    assert detail.json()["creative_spend"] is None
