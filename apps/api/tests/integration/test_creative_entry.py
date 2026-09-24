"""Stage 04's gated entry (PRD §4.2, §23 S4-P0).

The headline is `test_gated_by_two_frozen_artifacts`: of the four combinations
of {frozen plan, published ruleset}, only "both" is eligible, and the other
three name the right blocker. Everything else is one test per CR-E code — each
asserting not just that the code appears, but that it lands in the right list.
A warning that shows up under `blockers` is the defect this stage cannot have.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api.routes_creative import media_footprint_bytes, storage_blocker
from agent.db.models import (
    AmendmentChangeKind,
    AmendmentOrigin,
    AmendmentStatus,
    CampaignPlanStatus,
    CreativePackage,
    CreativePackageStatus,
    Evidence,
    EvidenceSource,
    GuidelineStatus,
    HumanTask,
    HumanTaskBlocking,
    HumanTaskStatus,
    PolicyAmendment,
    Run,
    RunStage,
    RunStatus,
    Workspace,
)
from agent.orchestrator.state import LockHolder, RunLock
from agent.redis_client import get_redis
from agent.schemas.creative_input import CreativeScope
from tests.integration.conftest import API_ROOT, ApiClient
from tests.integration.creative_support import (
    TEXT_ONLY,
    seed_both,
    seed_plan,
    seed_published,
    seed_signoff,
)
from tests.integration.runs_support import execute
from tests.openrouter_fake import FakeOpenRouter

pytestmark = pytest.mark.asyncio


async def _eligibility(client: ApiClient, project_id: uuid.UUID) -> dict[str, Any]:
    response = await client.get(f"/projects/{project_id}/creative/eligibility")
    assert response.status_code == 200, response.text
    return dict(response.json())


def _codes(body: dict[str, Any], key: str) -> set[str]:
    return {item["code"] for item in body[key]}


async def _start(client: ApiClient, project_id: uuid.UUID, **body: Any) -> Any:
    return await client.post(
        f"/projects/{project_id}/creative/runs",
        json={"scope": TEXT_ONLY, "media_models": [], **body},
    )


# ---------------------------------------------------------------------------
# THE HEADLINE TEST — law 32
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("plan", "ruleset", "expected"),
    [
        (False, False, {"no_frozen_plan", "no_published_ruleset"}),
        (True, False, {"no_published_ruleset"}),
        (False, True, {"no_frozen_plan"}),
        (True, True, set()),
    ],
    ids=["neither", "frozen-plan-only", "published-ruleset-only", "both"],
)
async def test_gated_by_two_frozen_artifacts(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    plan: bool,
    ruleset: bool,
    expected: set[str],
) -> None:
    """Only "both" is eligible. CR-E1 and CR-E2 are blockers, never warnings."""
    await seed_signoff(db, workspace_id, project_id, admin_user.id)
    if plan:
        await seed_plan(db, workspace_id, project_id, admin_user.id)
    if ruleset:
        await seed_published(db, workspace_id, project_id, admin_user.id)
    await db.commit()

    body = await _eligibility(admin, project_id)

    assert _codes(body, "blockers") == expected
    assert not ({"no_frozen_plan", "no_published_ruleset"} & _codes(body, "warnings"))
    assert body["eligible"] is (plan and ruleset)
    if plan and ruleset:
        assert body["blockers"] == []
        assert {"plan_version", "ruleset_version", "context_hash"} <= set(body["pins"])


# ---------------------------------------------------------------------------
# one test per CR-E code
# ---------------------------------------------------------------------------


async def test_e1_names_the_latest_plans_state(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_plan(
        db, workspace_id, project_id, admin_user.id, status=CampaignPlanStatus.READY_TO_FREEZE
    )
    await db.commit()
    body = await _eligibility(admin, project_id)
    blocker = next(b for b in body["blockers"] if b["code"] == "no_frozen_plan")
    assert "ready_to_freeze" in blocker["detail"] and "nobody has frozen it" in blocker["detail"]


async def test_e1_a_superseded_source_is_not_a_frozen_plan(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_plan(db, workspace_id, project_id, admin_user.id, source_superseded=True)
    await db.commit()
    assert "no_frozen_plan" in _codes(await _eligibility(admin, project_id), "blockers")


async def test_e3_schema_skew_names_both_versions(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_plan(db, workspace_id, project_id, admin_user.id, schema_version="9.9")
    await seed_published(db, workspace_id, project_id, admin_user.id, ruleset_schema="1.0")
    await seed_signoff(db, workspace_id, project_id, admin_user.id)
    await db.commit()
    body = await _eligibility(admin, project_id)
    blocker = next(b for b in body["blockers"] if b["code"] == "schema_unsupported")
    assert "9.9" in blocker["detail"] and "1.0" in blocker["detail"]

    # At trigger time it is a 422, not a 409 — the Stage 02 §4.3 rule.
    response = await _start(admin, project_id)
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "schema_unsupported"


async def test_e4_missing_asset_spec_or_claim_blocks_and_others_warn(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_plan(db, workspace_id, project_id, admin_user.id)
    await seed_published(
        db, workspace_id, project_id, admin_user.id, categories=("claim", "lexicon", "policy")
    )
    await seed_signoff(db, workspace_id, project_id, admin_user.id)
    await db.commit()
    body = await _eligibility(admin, project_id)
    assert _codes(body, "blockers") == {"ruleset_incomplete"}
    missing = [w["detail"] for w in body["warnings"] if w["code"] == "ruleset_category_missing"]
    assert len(missing) == 2
    assert any("image" in d for d in missing) and any("disclosure" in d for d in missing)


async def test_e5_no_signoff_matrix_blocks(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_plan(db, workspace_id, project_id, admin_user.id)
    await seed_published(db, workspace_id, project_id, admin_user.id)
    await db.commit()
    assert _codes(await _eligibility(admin, project_id), "blockers") == {"no_signoff_matrix"}


async def test_e6_a_held_lock_blocks_and_names_the_holder(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    holder_run = uuid.uuid4()
    await RunLock(get_redis(), RunStage.CREATIVE).acquire(
        project_id, LockHolder(run_id=holder_run, user_id=admin_user.id, user_name="Dana")
    )
    assert await get_redis().exists(f"project:{project_id}:creative_lock")
    body = await _eligibility(admin, project_id)
    blocker = next(b for b in body["blockers"] if b["code"] == "creative_in_flight")
    assert "Dana" in blocker["detail"] and str(holder_run) in blocker["detail"]


async def test_the_creative_lock_uses_the_creative_ttl(
    db: AsyncSession, project_id: uuid.UUID, admin_user: Any
) -> None:
    await RunLock(get_redis(), RunStage.CREATIVE).acquire(
        project_id, LockHolder(run_id=uuid.uuid4(), user_id=None, user_name="x")
    )
    ttl = await get_redis().ttl(f"project:{project_id}:creative_lock")
    assert 10_800 - 5 <= ttl <= 10_800


async def test_e7_no_credential_blocks(
    admin: ApiClient,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    monkeypatch.setenv("OPENROUTER_API_KEY", "")
    from agent.config import get_settings

    get_settings.cache_clear()
    assert _codes(await _eligibility(admin, project_id), "blockers") == {"missing_credential"}


async def test_e8_a_media_default_blocks_as_not_configured(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project: Any, admin_user: Any
) -> None:
    await seed_both(db, workspace_id, project.id, admin_user.id)
    project.settings = {**(project.settings or {}), "media_models": {"image": {"model_id": "x"}}}
    await db.commit()
    body = await _eligibility(admin, project.id)
    assert _codes(body, "blockers") == {"media_not_configured"}
    assert "Image" in body["blockers"][0]["detail"]


async def test_e8_a_media_request_is_a_422(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    model = {
        "modality": "image",
        "model_id": "some/image-model",
        "capability": {},
        "capability_hash": "c",
    }
    for body in (
        {"media_models": [model]},
        {"scope": {**TEXT_ONLY, "images": True}},
    ):
        response = await _start(admin, project_id, **body)
        assert response.status_code == 422, response.text
        assert response.json()["code"] == "media_not_configured"
    assert await _creative_runs(db, project_id) == 0


async def test_e10_zdr_blocks_video(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project: Any, admin_user: Any
) -> None:
    await seed_both(db, workspace_id, project.id, admin_user.id)
    project.settings = {**(project.settings or {}), "media_models": {"video": {"model_id": "v"}}}
    workspace = await db.get(Workspace, workspace_id)
    assert workspace is not None
    workspace.settings = {**(workspace.settings or {}), "zdr_enforced": True}
    await db.commit()
    assert _codes(await _eligibility(admin, project.id), "blockers") == {
        "media_not_configured",
        "zdr_blocks_video",
    }


async def test_e11_storage_needs_twice_the_footprint() -> None:
    assert storage_blocker(free_bytes=200, footprint_bytes=100) is None
    blocker = storage_blocker(free_bytes=199, footprint_bytes=100)
    assert blocker is not None and blocker.code == "storage_insufficient"
    assert "199" in blocker.detail and "200" in blocker.detail
    text_only = CreativeScope(images=False, video=False, concepts_per_campaign=2)
    assert media_footprint_bytes(text_only) == 0
    # Not zero: unknown until S4-P1 prices the shot plan.
    assert media_footprint_bytes(text_only.model_copy(update={"images": True})) is None


async def test_e12_guideline_flags_are_warnings_never_blockers(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_plan(db, workspace_id, project_id, admin_user.id)
    guideline, _ = await seed_published(
        db, workspace_id, project_id, admin_user.id, signature_stale=True
    )
    await seed_signoff(db, workspace_id, project_id, admin_user.id)
    db.add(
        PolicyAmendment(
            workspace_id=workspace_id,
            project_id=project_id,
            origin=AmendmentOrigin.MANUAL,
            detected_at=datetime.now(UTC),
            change_kind=AmendmentChangeKind.SUBSTANTIVE,
            status=AmendmentStatus.NEEDS_REVIEW,
        )
    )
    db.add(
        HumanTask(
            workspace_id=workspace_id,
            project_id=project_id,
            guideline_run_id=guideline.guideline_run_id,
            node_id="3.3.2",
            task_key="H2",
            title="Verify the advertiser",
            instructions="Upload the certificate.",
            assignee_id=admin_user.id,
            required_artifacts={},
            status=HumanTaskStatus.PENDING,
            blocking_for=HumanTaskBlocking.LAUNCH,
        )
    )
    await db.commit()
    body = await _eligibility(admin, project_id)
    assert body["eligible"] is True
    assert {
        "claims_unlicensed_stale",
        "unreviewed_amendments",
        "verification_open_blocks_launch",
    } <= _codes(body, "warnings")


async def test_e13_stale_or_missing_offer_data_warns(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    body = await _eligibility(admin, project_id)
    assert "offer_data_stale" in _codes(body, "warnings") and body["eligible"] is True

    offer = {
        "sku": "SDS-PRO",
        "product_set": "plans",
        "list_price": 99.0,
        "current_price": 79.0,
        "currency": "USD",
        "market": "US",
    }
    db.add(
        Evidence(
            project_id=project_id,
            source=EvidenceSource.CSV,
            kind="offer_record",
            payload={**offer, "observed_at": (datetime.now(UTC) - timedelta(days=90)).isoformat()},
            hash="old-offer",
        )
    )
    await db.commit()
    assert "offer_data_stale" in _codes(await _eligibility(admin, project_id), "warnings")

    db.add(
        Evidence(
            project_id=project_id,
            source=EvidenceSource.CSV,
            kind="offer_record",
            payload={**offer, "observed_at": datetime.now(UTC).isoformat()},
            hash="fresh-offer",
        )
    )
    await db.commit()
    assert "offer_data_stale" not in _codes(await _eligibility(admin, project_id), "warnings")


async def test_e14_a_released_package_warns_will_mint_new_version(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    plan, guideline, ruleset = await seed_both(db, workspace_id, project_id, admin_user.id)
    earlier = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger="manual",
        status=RunStatus.SUCCEEDED,
        stage=RunStage.CREATIVE,
        source_run_id=plan.plan_run_id,
    )
    db.add(earlier)
    await db.flush()
    db.add(
        CreativePackage(
            workspace_id=workspace_id,
            project_id=project_id,
            creative_run_id=earlier.id,
            schema_version="1.0",
            version=1,
            status=CreativePackageStatus.RELEASED,
            plan_id=plan.id,
            plan_version=plan.version,
            guideline_id=guideline.id,
            ruleset_version=ruleset.ruleset_version,
        )
    )
    await db.commit()
    body = await _eligibility(admin, project_id)
    assert "will_mint_new_version" in _codes(body, "warnings") and body["eligible"] is True


async def test_e15_every_role_reads_eligibility_and_only_executors_may_start(
    admin: ApiClient,
    signed_in_as: Any,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    for role in ("admin", "operator", "approver", "viewer"):
        caller = await signed_in_as(role)
        body = await _eligibility(caller, project_id)
        missing = "missing_permission" in _codes(body, "blockers")
        assert missing is (role in {"approver", "viewer"}), role
        if role in {"approver", "viewer"}:
            response = await _start(caller, project_id)
            assert response.status_code == 403, role
            assert response.json()["missing_permission"] == "creative_execute"
    assert await _creative_runs(db, project_id) == 0


# ---------------------------------------------------------------------------
# starting a run
# ---------------------------------------------------------------------------


async def _creative_runs(db: AsyncSession, project_id: uuid.UUID) -> int:
    db.expire_all()
    return int(
        (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(Run)
                .where(Run.project_id == project_id, Run.stage == RunStage.CREATIVE)
            )
        ).scalar_one()
    )


async def test_both_starts_a_run_and_the_dummy_dag_reaches_its_terminal_node_over_sse(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    plan, _, ruleset = await seed_both(db, workspace_id, project_id, admin_user.id)
    body = await _eligibility(admin, project_id)
    assert body["eligible"] is True and body["blockers"] == []

    response = await _start(admin, project_id)
    assert response.status_code == 202, response.text
    run_id = uuid.UUID(response.json()["run_id"])

    run = await db.get(Run, run_id)
    assert run is not None
    assert run.stage is RunStage.CREATIVE
    assert run.source_run_id == plan.plan_run_id
    assert run.input_hash == response.json()["input_hash"]
    assert run.pins is not None and len(run.pins) == 1
    assert run.pins[0]["ruleset_version"] == ruleset.ruleset_version
    assert run.pins[0]["reason"] == "start"

    result = await execute(run_id, FakeOpenRouter())
    assert result.status is RunStatus.SUCCEEDED

    stream = await admin.get(f"/runs/{run_id}/events")
    assert stream.status_code == 200
    events = [
        (block.split("event: ", 1)[1].splitlines()[0], block)
        for block in stream.text.split("\n\n")
        if "event: " in block
    ]
    names = [name for name, _ in events]
    completed = [
        json.loads(block.split("data: ", 1)[1])["node_id"]
        for name, block in events
        if name == "node.completed"
    ]
    assert completed == ["4.0.1", "4.0.2"]
    assert names[-1] == "run.completed"
    assert '"succeeded"' in events[-1][1]

    overview = (await admin.get(f"/projects/{project_id}/creative")).json()
    assert [r["run_id"] for r in overview["runs"]] == [str(run_id)]
    assert overview["packages"] == []


async def test_two_concurrent_starts_make_one_run_and_one_409(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    first, second = await asyncio.gather(_start(admin, project_id), _start(admin, project_id))
    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [202, 409], (first.text, second.text)
    refused = first if first.status_code == 409 else second
    accepted = first if first.status_code == 202 else second
    problem = refused.json()
    assert problem["code"] == "creative_in_flight"
    assert accepted.json()["run_id"] in problem["detail"]
    assert await _creative_runs(db, project_id) == 1


async def test_an_unknown_campaign_in_scope_is_a_422(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    await seed_both(db, workspace_id, project_id, admin_user.id)
    response = await _start(admin, project_id, scope={**TEXT_ONLY, "campaign_refs": ["c-nope"]})
    assert response.status_code == 422
    assert response.json()["code"] == "scope_unknown_campaign"
    assert await _creative_runs(db, project_id) == 0


# ---------------------------------------------------------------------------
# the Stage 03 projection
# ---------------------------------------------------------------------------


async def test_creative_context_is_404_when_nothing_is_published(
    admin: ApiClient, project_id: uuid.UUID
) -> None:
    response = await admin.get(
        "/guidelines/published/creative-context", params={"project_id": str(project_id)}
    )
    assert response.status_code == 404


async def test_a_pin_returns_the_historical_context_after_a_newer_publish(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    first, first_ruleset = await seed_published(db, workspace_id, project_id, admin_user.id)
    await db.commit()
    before = await admin.get(
        "/guidelines/published/creative-context", params={"project_id": str(project_id)}
    )
    assert before.status_code == 200

    first.status = GuidelineStatus.SUPERSEDED
    await db.flush()
    await seed_published(db, workspace_id, project_id, admin_user.id, major=2)
    await db.commit()

    current = await admin.get(
        "/guidelines/published/creative-context", params={"project_id": str(project_id)}
    )
    pinned = await admin.get(
        "/guidelines/published/creative-context",
        params={"project_id": str(project_id), "pin": first_ruleset.ruleset_version},
    )
    assert current.json()["guideline_version"] == "2.0"
    assert pinned.status_code == 200
    assert pinned.json() == before.json()
    assert pinned.json()["hash"] != current.json()["hash"]


async def test_the_context_hash_is_identical_across_two_processes(
    admin: ApiClient, db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    guideline, ruleset = await seed_published(db, workspace_id, project_id, admin_user.id)
    await db.commit()
    here = (
        await admin.get(
            "/guidelines/published/creative-context", params={"project_id": str(project_id)}
        )
    ).json()["hash"]

    script = (
        "import asyncio, uuid\n"
        "from agent.db.session import get_sessionmaker\n"
        "from agent.guidelines.projection import build_creative_context\n"
        "async def main():\n"
        "    async with get_sessionmaker()() as db:\n"
        f"        ctx = await build_creative_context(db, uuid.UUID('{guideline.id}'), "
        f"'{ruleset.ruleset_version}', workspace_id=uuid.UUID('{workspace_id}'))\n"
        "        print(ctx.hash)\n"
        "asyncio.run(main())\n"
    )
    # A fresh interpreter with a different hash seed: dict and set ordering in
    # the new process owes nothing to this one.
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-c", script],
        cwd=API_ROOT,
        env={**os.environ, "PYTHONHASHSEED": "12345"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().splitlines()[-1] == here
