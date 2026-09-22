"""The freeze, against a real database (Stage 02 PRD §12.2, §16, law 17).

`tests/test_plan_freeze.py` proves the decisions. This file proves the three
things a stub cannot:

1. **The transaction.** Version minting, the supersede, `frozen_approval_ids`
   and the `AuditLog` row either all happen or none do.
2. **The trigger.** Law 17 is enforced by Postgres, not by a code path, and the
   only honest way to show that is to try the `UPDATE` and be refused.
3. **Migration 0014.** A project holding two unfrozen plans is what the shipped
   `UNIQUE (project_id, version)` made impossible, and what the partial index
   replaced it with makes routine. That is a schema claim; it needs a schema.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction
from agent.db.models import (
    Approval,
    ApprovalRequiredRole,
    ApprovalStatus,
    AuditLog,
    CampaignPlan,
    CampaignPlanStatus,
    Report,
    ResearchAcceptance,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)
from tests import plan_fixture as fixture
from tests.integration.conftest import ApiClient
from tests.report_support import golden_payload

pytestmark = pytest.mark.anyio

GATES = (("G1", "2.1.3"), ("G2", "2.1.4"), ("G3", "2.2.4"), ("G4", "2.3.1"))


async def _seed_plan(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    gate_status: dict[str, ApprovalStatus] | None = None,
    critique_issues: list[dict[str, Any]] | None = None,
    status: CampaignPlanStatus = CampaignPlanStatus.READY_TO_FREEZE,
    version: int = 0,
    source_superseded: bool = False,
    acceptance_id: uuid.UUID | None = None,
) -> dict[str, Any]:
    """A research run, its acceptance, a finished plan run and a plan row.

    `acceptance_id` reuses an existing acceptance instead of creating one, and
    a second plan in the same project **must** pass it: a partial unique index
    allows exactly one current acceptance per project (§7.2). That is also the
    realistic shape — a re-run after a rejected gate plans from the same
    accepted research, it does not re-accept it.
    """
    research = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=user_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.RESEARCH,
    )
    db.add(research)
    await db.flush()

    payload = golden_payload()
    payload["project_id"] = str(project_id)
    payload["run_id"] = str(research.id)
    report = Report(run_id=research.id, schema_version="1.0", payload=payload, markdown="# report")
    db.add(report)
    await db.flush()

    if acceptance_id is None:
        acceptance = ResearchAcceptance(
            workspace_id=workspace_id,
            project_id=project_id,
            run_id=research.id,
            report_id=report.id,
            accepted_by=user_id,
            launch_readiness_at_acceptance="go",
        )
        db.add(acceptance)
        await db.flush()
        acceptance_id = acceptance.id

    plan_run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=user_id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.PLAN,
        source_run_id=research.id,
    )
    db.add(plan_run)
    await db.flush()

    wanted = gate_status or {}
    for key, node_id in GATES:
        db.add(
            Approval(
                run_id=plan_run.id,
                node_id=node_id,
                gate_key=key,
                status=wanted.get(key, ApprovalStatus.APPROVED),
                required_role=ApprovalRequiredRole.APPROVER,
                proposal={},
                decided_by=user_id,
            )
        )

    contract = fixture.plan(plan_status="ready_to_freeze", critique_issues=critique_issues or [])
    plan = CampaignPlan(
        workspace_id=workspace_id,
        project_id=project_id,
        plan_run_id=plan_run.id,
        acceptance_id=acceptance_id,
        schema_version="1.0",
        version=version,
        status=status,
        payload=json.loads(contract.model_dump_json()),
        markdown="# draft plan\n",
        source_superseded=source_superseded,
    )
    db.add(plan)
    await db.commit()
    return {
        "plan": plan,
        "plan_run_id": plan_run.id,
        "project_id": project_id,
        "acceptance_id": acceptance_id,
    }


@pytest_asyncio.fixture
async def seeded(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> dict[str, Any]:
    me = (await admin.get("/auth/me")).json()
    return await _seed_plan(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
    )


# ---------------------------------------------------------------------------
# the happy path
# ---------------------------------------------------------------------------


async def test_a_ready_plan_freezes_at_version_one(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    response = await admin.post(
        f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["status"] == "frozen"
    assert body["already_frozen"] is False
    assert body["frozen_at"] is not None
    assert len(body["frozen_approval_ids"]) == 4
    assert body["superseded"] == []


async def test_the_four_sealed_approval_ids_are_the_four_gates(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    body = (
        await admin.post(f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1})
    ).json()
    rows = (
        (
            await db.execute(
                sa.select(Approval.gate_key).where(
                    Approval.id.in_([uuid.UUID(value) for value in body["frozen_approval_ids"]])
                )
            )
        )
        .scalars()
        .all()
    )
    assert sorted(rows) == ["G1", "G2", "G3", "G4"]


async def test_the_payload_and_the_markdown_are_rewritten_by_the_same_statement(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """The trigger fires on `OLD.status`, which is why this can happen at all."""
    await admin.post(f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1})
    # `_seed_plan` loaded this row into *this* session, and the freeze happened
    # in the API's own. Without expiring, the identity map hands back the
    # pre-freeze copy and the assertion below reads a stale `ready_to_freeze`.
    db.expire_all()
    plan = (
        await db.execute(
            sa.select(CampaignPlan).where(CampaignPlan.plan_run_id == seeded["plan_run_id"])
        )
    ).scalar_one()
    assert plan.status is CampaignPlanStatus.FROZEN
    assert plan.version == 1
    assert plan.payload["plan_status"] == "frozen"
    assert plan.payload["version"] == 1
    assert "DRAFT — NOT APPROVED" not in plan.markdown
    assert "**Status:** Frozen (v1)" in plan.markdown


async def test_the_freeze_writes_an_audit_row_in_the_same_transaction(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """PS4: acceptance, start, each gate, each override and the freeze."""
    await admin.post(f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1})
    row = (
        await db.execute(
            sa.select(AuditLog).where(AuditLog.action == AuditAction.PLAN_FROZEN.value)
        )
    ).scalar_one()
    assert row.meta["version"] == 1
    assert row.meta["plan_run_id"] == str(seeded["plan_run_id"])
    assert len(row.meta["approval_ids"]) == 4
    assert row.actor_id is not None


# ---------------------------------------------------------------------------
# §16 rule 2 — idempotence
# ---------------------------------------------------------------------------


async def test_freezing_twice_at_the_same_version_returns_200_not_an_error(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    first = await admin.post(f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1})
    second = await admin.post(f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1})
    assert first.status_code == 200
    assert second.status_code == 200, second.text
    assert second.json()["already_frozen"] is True
    assert second.json()["version"] == 1


async def test_a_mismatched_confirm_version_is_a_409_naming_both_numbers(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    response = await admin.post(
        f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 7}
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["expected_version"] == 1
    assert body["submitted_version"] == 7
    assert body["blockers"][0]["code"] == "version_race"


# ---------------------------------------------------------------------------
# what a 409 says
# ---------------------------------------------------------------------------


async def test_a_pending_gate_returns_a_409_with_the_blocker_array(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    seeded = await _seed_plan(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
        gate_status={"G3": ApprovalStatus.PENDING},
    )
    response = await admin.post(
        f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1}
    )
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["blockers"][0]["code"] == "gate_not_approved"
    assert "G3 (budget allocation)" in body["blockers"][0]["detail"]
    assert body["blockers"][0]["fix_url"] == "/approvals"


async def test_a_blocking_critique_returns_a_409(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    seeded = await _seed_plan(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
        critique_issues=[
            {
                "severity": "blocking",
                "section": "media_plan.allocation",
                "finding": "The allocation does not sum to the envelope.",
                "fix": "Re-run 2.2.4.",
                "check": "1_allocation_sums",
            }
        ],
    )
    response = await admin.post(
        f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1}
    )
    assert response.status_code == 409
    assert response.json()["blockers"][0]["code"] == "blocking_critique"


async def test_superseded_research_returns_a_409(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """§4.4, set up by its real cause rather than by setting the flag.

    This used to seed `source_superseded=True` by hand on a plan whose
    acceptance was still current — a state the product cannot produce. S2-P7
    made the freeze **recompute** the flag from the acceptance chain before
    reading it, precisely so a stale or hand-written value cannot decide a
    seal, and the hand-set version of this test therefore started passing the
    freeze. Superseding the acceptance is what it was always trying to say.
    """
    me = (await admin.get("/auth/me")).json()
    seeded = await _seed_plan(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
    )
    # The acceptance stops being current. `superseded_by = self` is this
    # column's "withdrawn"; a replacement would point at the newer row.
    await db.execute(
        sa.update(ResearchAcceptance)
        .where(ResearchAcceptance.id == seeded["acceptance_id"])
        .values(superseded_by=seeded["acceptance_id"])
    )
    await db.commit()

    response = await admin.post(
        f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1}
    )
    assert response.status_code == 409
    assert response.json()["blockers"][0]["code"] == "source_superseded"

    # ...and the freeze wrote the correction it computed, rather than deciding
    # on it and throwing it away.
    db.expire_all()
    plan = (
        await db.execute(
            sa.select(CampaignPlan).where(CampaignPlan.plan_run_id == seeded["plan_run_id"])
        )
    ).scalar_one()
    assert plan.source_superseded is True


async def test_a_stale_flag_does_not_decide_the_freeze(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """The other direction: a `True` nobody can justify must not refuse a seal.

    The flag is derived (§4.4), so the freeze recomputes it. A row carrying
    `True` against a current acceptance is a bug somewhere upstream, and the
    right response is to correct it and proceed — not to refuse a plan whose
    research is in fact current.
    """
    me = (await admin.get("/auth/me")).json()
    seeded = await _seed_plan(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=uuid.UUID(me["id"]),
        source_superseded=True,
    )
    response = await admin.post(
        f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1}
    )
    assert response.status_code == 200, response.text

    db.expire_all()
    plan = (
        await db.execute(
            sa.select(CampaignPlan).where(CampaignPlan.plan_run_id == seeded["plan_run_id"])
        )
    ).scalar_one()
    assert plan.source_superseded is False


async def test_a_plan_that_does_not_exist_is_a_404(admin: ApiClient) -> None:
    response = await admin.post(f"/plans/{uuid.uuid4()}/freeze", json={"confirm_version": 1})
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# versions and supersede
# ---------------------------------------------------------------------------


async def test_a_second_freeze_mints_v2_and_supersedes_v1(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])
    first = await _seed_plan(db, project_id=project.id, workspace_id=workspace_id, user_id=user_id)
    await admin.post(f"/plans/{first['plan_run_id']}/freeze", json={"confirm_version": 1})

    second = await _seed_plan(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        # One current acceptance per project (§7.2): a re-run plans from
        # the same accepted research rather than re-accepting it.
        acceptance_id=first["acceptance_id"],
    )
    response = await admin.post(
        f"/plans/{second['plan_run_id']}/freeze", json={"confirm_version": 2}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 2
    assert len(body["superseded"]) == 1

    previous = await db.get(CampaignPlan, uuid.UUID(body["superseded"][0]))
    await db.refresh(previous)
    assert previous.status is CampaignPlanStatus.SUPERSEDED
    # A superseded plan keeps its version and every word it said when signed.
    assert previous.version == 1
    assert previous.payload["plan_status"] == "frozen"


async def test_a_superseded_version_is_never_reissued(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """Why `next_version` counts every row rather than only the frozen ones."""
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])
    acceptance_id: uuid.UUID | None = None
    for expected in (1, 2, 3):
        seeded = await _seed_plan(
            db,
            project_id=project.id,
            workspace_id=workspace_id,
            user_id=user_id,
            acceptance_id=acceptance_id,
        )
        acceptance_id = seeded["acceptance_id"]
        response = await admin.post(
            f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": expected}
        )
        assert response.status_code == 200, response.text
        assert response.json()["version"] == expected

    versions = (
        (
            await db.execute(
                sa.select(CampaignPlan.version).where(CampaignPlan.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert sorted(versions) == [1, 2, 3]


async def test_a_project_may_hold_two_unfrozen_plans(
    db: AsyncSession, admin: ApiClient, project: Any, workspace_id: uuid.UUID
) -> None:
    """Migration 0014's whole reason.

    The shipped `UNIQUE (project_id, version)` made this impossible, and §12.2's
    own lifecycle (`blocked → draft: re-run`) requires it the first time a gate
    is rejected.
    """
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])
    acceptance_id: uuid.UUID | None = None
    for _ in range(3):
        seeded = await _seed_plan(
            db,
            project_id=project.id,
            workspace_id=workspace_id,
            user_id=user_id,
            acceptance_id=acceptance_id,
        )
        acceptance_id = seeded["acceptance_id"]
    count = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(CampaignPlan)
            .where(CampaignPlan.project_id == project.id, CampaignPlan.version == 0)
        )
    ).scalar_one()
    assert count == 3


async def test_two_minted_versions_still_cannot_collide(
    db: AsyncSession, admin: ApiClient, project: Any, workspace_id: uuid.UUID
) -> None:
    """The partial index still enforces uniqueness where it matters."""
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])
    first = await _seed_plan(db, project_id=project.id, workspace_id=workspace_id, user_id=user_id)
    await admin.post(f"/plans/{first['plan_run_id']}/freeze", json={"confirm_version": 1})

    second = await _seed_plan(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        # One current acceptance per project (§7.2): a re-run plans from
        # the same accepted research rather than re-accepting it.
        acceptance_id=first["acceptance_id"],
    )
    plan = second["plan"]
    plan.version = 1
    plan.status = CampaignPlanStatus.FROZEN
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


# ---------------------------------------------------------------------------
# law 17 — the trigger
# ---------------------------------------------------------------------------


async def test_the_database_refuses_to_rewrite_a_frozen_plans_payload(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """PS2, proved by trying it rather than by reading the migration."""
    await admin.post(f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1})
    plan_id = (
        await db.execute(
            sa.select(CampaignPlan.id).where(CampaignPlan.plan_run_id == seeded["plan_run_id"])
        )
    ).scalar_one()

    with pytest.raises(DBAPIError, match="is frozen"):
        await db.execute(
            sa.update(CampaignPlan)
            .where(CampaignPlan.id == plan_id)
            .values(payload={"tampered": True})
        )
        await db.commit()
    await db.rollback()


async def test_a_frozen_plans_status_may_still_change(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """The trigger seals three columns, not the row: supersede has to work."""
    await admin.post(f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1})
    plan_id = (
        await db.execute(
            sa.select(CampaignPlan.id).where(CampaignPlan.plan_run_id == seeded["plan_run_id"])
        )
    ).scalar_one()
    await db.execute(
        sa.update(CampaignPlan)
        .where(CampaignPlan.id == plan_id)
        .values(status=CampaignPlanStatus.SUPERSEDED, source_superseded=True)
    )
    await db.commit()
    refreshed = await db.get(CampaignPlan, plan_id)
    await db.refresh(refreshed)
    assert refreshed.status is CampaignPlanStatus.SUPERSEDED


# ---------------------------------------------------------------------------
# PS3 — authz
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("role", "expected"), [("approver", 200), ("operator", 403), ("viewer", 403)]
)
async def test_only_a_freeze_holder_may_seal_a_plan(
    signed_in_as: Any,
    seeded: dict[str, Any],
    role: str,
    expected: int,
) -> None:
    """`PLAN_FREEZE` is admin and approver. An operator runs plans; it does not
    sign them, and the difference is the whole point of the gate."""
    member = await signed_in_as(role)
    response = await member.post(
        f"/plans/{seeded['plan_run_id']}/freeze", json={"confirm_version": 1}
    )
    assert response.status_code == expected, response.text


# ---------------------------------------------------------------------------
# the history order, against real rows
# ---------------------------------------------------------------------------


async def test_the_history_puts_todays_draft_above_last_weeks_frozen_plan(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """`tests/test_plan_version_order.py` asserts the ORDER BY; this proves it.

    Migration 0014 made every unfrozen plan version 0, so `version DESC` over
    the whole history sorts a v1 frozen last week above a draft created this
    morning. The compare screen takes the first two rows as the newer and
    older side of its diff, so the wrong order renders a budget increase as a
    decrease. Found by the S2-P6c session reading the sign of a number.
    """
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])

    older = await _seed_plan(db, project_id=project.id, workspace_id=workspace_id, user_id=user_id)
    await admin.post(f"/plans/{older['plan_run_id']}/freeze", json={"confirm_version": 1})

    newer = await _seed_plan(
        db,
        project_id=project.id,
        workspace_id=workspace_id,
        user_id=user_id,
        acceptance_id=older["acceptance_id"],
    )

    rows = (await admin.get(f"/projects/{project.id}/plans")).json()["items"]
    assert len(rows) == 2

    # Guard the fixture before asserting on the route. `created_at` defaults to
    # `now()`, which is the *transaction* timestamp and constant inside one — so
    # two plans seeded in a single commit share it, the version tiebreak takes
    # over, and this test fails as though the ordering were wrong. `_seed_plan`
    # commits per call, and this says so out loud. The S2-P6c session lost time
    # to exactly this shape in their own fixture.
    assert rows[0]["created_at"] != rows[1]["created_at"], (
        "both plans were seeded in one transaction, so they share a created_at "
        "and this test is measuring the tiebreak rather than the ordering"
    )

    # The draft is newer, so it is first — even though its version (0) is lower.
    assert rows[0]["plan_run_id"] == str(newer["plan_run_id"])
    assert rows[0]["version"] == 0
    assert rows[1]["version"] == 1
