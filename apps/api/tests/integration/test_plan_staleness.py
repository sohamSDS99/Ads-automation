"""§4.4 — what happens to a plan when the research under it is re-accepted.

The column this suite is about, `CampaignPlan.source_superseded`, shipped in
migration 0013 and was read from three places — the freeze blocker, the plan
list and the Plan Viewer's banner — and written from **none** until S2-P7.
Every test that touched it set it by hand with an `UPDATE`, which is why it
looked covered: the flag's *consumers* were all tested against a value no code
path could produce. This suite tests the producer.

The rule is derived rather than latched (see `planning/staleness.py`), so the
interesting cases are the ones that go back down: a withdrawal revives the
acceptance before it, and a plan whose source is current again must stop
claiming otherwise. `test_the_stored_flag_always_equals_the_derived_value`
holds that across every transition in one test, so a future path that changes
acceptance currency without refreshing fails here rather than in production.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction
from agent.db.models import (
    AuditLog,
    CampaignPlan,
    CampaignPlanStatus,
    Run,
    RunStage,
    RunTrigger,
)
from agent.planning import staleness
from tests.integration.conftest import ApiClient
from tests.integration.test_plan_handshake import _seed_research

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _plan(
    db: AsyncSession,
    *,
    seeded: dict[str, Any],
    acceptance_id: uuid.UUID,
    status: CampaignPlanStatus,
    version: int,
    project_id: uuid.UUID | None = None,
    source_run_id: uuid.UUID | None = None,
) -> uuid.UUID:
    """One `CampaignPlan` against a named acceptance. Returns its id.

    The id is returned rather than the row: the caller commits, and a committed
    session expires its instances, so reading `plan.id` afterwards is a lazy
    load in a sync context — `MissingGreenlet`, from wherever the assertion
    happens to be.
    """
    plan_run = Run(
        workspace_id=seeded["workspace_id"],
        project_id=project_id or seeded["project_id"],
        trigger=RunTrigger.MANUAL,
        stage=RunStage.PLAN,
        # `ck_run_plan_has_source`: a plan run without the research run it was
        # planned from is not a plan run, and the database says so.
        source_run_id=source_run_id or seeded["run_id"],
        triggered_by=seeded["user_id"],
    )
    db.add(plan_run)
    await db.flush()
    plan = CampaignPlan(
        workspace_id=seeded["workspace_id"],
        project_id=project_id or seeded["project_id"],
        plan_run_id=plan_run.id,
        acceptance_id=acceptance_id,
        schema_version="1.0",
        version=version,
        status=status,
        payload={"envelope": 10_000},
        markdown=f"# v{version}",
    )
    db.add(plan)
    await db.commit()
    return plan.id


async def _newer_research(db: AsyncSession, seeded: dict[str, Any]) -> uuid.UUID:
    """Another finished research run in the same project. Returns **its id**.

    The id, not the row, and that is the whole point of the helper: `_flag`
    below calls `db.expire_all()` — it has to, because the API mutates through
    a different session — and an expired `Run` re-loads lazily the next time
    something reads `run.id`, from a sync context, which is `MissingGreenlet`
    somewhere far from the cause. Carry plain values across anything that can
    expire the identity map.
    """
    run, _ = await _seed_research(
        db,
        project_id=seeded["project_id"],
        user_id=seeded["user_id"],
        workspace_id=seeded["workspace_id"],
    )
    return run.id


async def _flag(db: AsyncSession, plan_id: uuid.UUID) -> bool:
    """The stored flag, read fresh.

    `expire_all` first: these tests call the API through a second session, so
    this one's identity map is holding the row as it looked before the request.
    """
    db.expire_all()
    row = await db.get(CampaignPlan, plan_id)
    assert row is not None
    return row.source_superseded


async def _accept(admin: ApiClient, run_id: uuid.UUID) -> uuid.UUID:
    response = await admin.post(f"/runs/{run_id}/accept", json={})
    assert response.status_code == 201, response.text
    return uuid.UUID(response.json()["id"])


async def _audit(db: AsyncSession, action: AuditAction) -> list[AuditLog]:
    db.expire_all()
    result = await db.execute(
        sa.select(AuditLog).where(AuditLog.action == action.value).order_by(AuditLog.created_at)
    )
    return list(result.scalars().all())


@pytest_asyncio.fixture
async def seeded(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> dict[str, Any]:
    """A finished research run in a project, plus the ids a test needs."""
    me = (await admin.get("/auth/me")).json()
    user_id = uuid.UUID(me["id"])
    run, report = await _seed_research(
        db, project_id=project.id, user_id=user_id, workspace_id=workspace_id
    )
    return {
        "project_id": project.id,
        "run_id": run.id,
        "report_id": report.id,
        "user_id": user_id,
        "workspace_id": workspace_id,
    }


# ---------------------------------------------------------------------------
# the rule
# ---------------------------------------------------------------------------


async def test_accepting_newer_research_marks_every_plan_built_from_the_older_one(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """§4.4, the headline. A frozen plan and a draft are both marked."""
    first = await _accept(admin, seeded["run_id"])
    frozen = await _plan(
        db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.FROZEN, version=1
    )
    draft = await _plan(
        db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.DRAFT, version=0
    )
    assert await _flag(db, frozen) is False
    assert await _flag(db, draft) is False

    newer_id = await _newer_research(db, seeded)
    await _accept(admin, newer_id)

    assert await _flag(db, frozen) is True
    assert await _flag(db, draft) is True


async def test_a_marked_frozen_plan_keeps_its_status_and_its_payload(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """ "The frozen plan stays valid and downloadable" — §18, and the trigger.

    `source_superseded` is one of the columns migration 0013 deliberately left
    writable on a frozen row. If the refresh ever touched `payload`, `markdown`
    or `version` as well, the guard would raise and accepting research would
    start returning 500s — so this asserts the write went through *and* that it
    changed nothing it was not supposed to.
    """
    first = await _accept(admin, seeded["run_id"])
    frozen = await _plan(
        db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.FROZEN, version=3
    )
    newer_id = await _newer_research(db, seeded)
    await _accept(admin, newer_id)

    db.expire_all()
    row = await db.get(CampaignPlan, frozen)
    assert row is not None
    assert row.source_superseded is True
    assert row.status is CampaignPlanStatus.FROZEN
    assert row.version == 3
    assert row.payload == {"envelope": 10_000}
    assert row.markdown == "# v3"


async def test_withdrawing_the_newer_acceptance_clears_the_flag_again(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """The way back down, which a latch would get wrong.

    Withdraw the newer acceptance, re-accept the original run: `_accept`
    revives the existing row by setting `superseded_by = None`, so the plan's
    source is the current acceptance again and the banner must go away.
    """
    first = await _accept(admin, seeded["run_id"])
    plan = await _plan(
        db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.DRAFT, version=0
    )
    newer_id = await _newer_research(db, seeded)
    await _accept(admin, newer_id)
    assert await _flag(db, plan) is True

    withdrawn = await admin.delete(f"/runs/{newer_id}/accept")
    assert withdrawn.status_code == 200, withdrawn.text
    # Withdrawing the newer one does not by itself restore the older one —
    # nothing is current now, and the plan's source is still not it.
    assert await _flag(db, plan) is True

    await _accept(admin, seeded["run_id"])
    assert await _flag(db, plan) is False


async def test_withdrawing_the_only_acceptance_marks_its_plans(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """A withdrawal is a supersede with nothing taking its place.

    `withdraw_acceptance` writes `superseded_by = id` — the row's own "no
    longer current". The derivation reads exactly that, so one rule covers
    both causes and there is no second code path to forget.
    """
    first = await _accept(admin, seeded["run_id"])
    plan = await _plan(
        db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.DRAFT, version=0
    )
    assert await _flag(db, plan) is False

    withdrawn = await admin.delete(f"/runs/{seeded['run_id']}/accept")
    assert withdrawn.status_code == 200, withdrawn.text
    assert await _flag(db, plan) is True


async def test_plans_in_another_project_are_not_touched(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any], second_project_id: uuid.UUID
) -> None:
    """The refresh is scoped to the project whose acceptance moved."""
    other_run, _ = await _seed_research(
        db,
        project_id=second_project_id,
        user_id=seeded["user_id"],
        workspace_id=seeded["workspace_id"],
    )
    other_run_id = other_run.id  # plain value: `_flag` expires the identity map
    other_acceptance = await _accept(admin, other_run_id)
    other_plan = await _plan(
        db,
        seeded=seeded,
        acceptance_id=other_acceptance,
        status=CampaignPlanStatus.FROZEN,
        version=1,
        project_id=second_project_id,
        source_run_id=other_run_id,
    )

    first = await _accept(admin, seeded["run_id"])
    mine = await _plan(
        db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.DRAFT, version=0
    )
    newer_id = await _newer_research(db, seeded)
    await _accept(admin, newer_id)

    assert await _flag(db, mine) is True
    assert await _flag(db, other_plan) is False


# ---------------------------------------------------------------------------
# the invariant
# ---------------------------------------------------------------------------


async def test_the_stored_flag_always_equals_the_derived_value(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """The column and the acceptance chain agree after every transition.

    This is the test that makes the column safe to read. A future endpoint that
    changes acceptance currency and forgets to refresh fails here, not on
    somebody's screen six weeks later.
    """
    first = await _accept(admin, seeded["run_id"])
    await _plan(db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.FROZEN, version=1)
    await _plan(db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.DRAFT, version=0)
    newer_id = await _newer_research(db, seeded)

    async def agrees() -> None:
        db.expire_all()
        derived = await staleness.derived_flags(
            db, workspace_id=seeded["workspace_id"], project_id=seeded["project_id"]
        )
        stored = {
            row.id: row.source_superseded
            for row in (
                await db.execute(
                    sa.select(CampaignPlan).where(CampaignPlan.project_id == seeded["project_id"])
                )
            )
            .scalars()
            .all()
        }
        assert stored == derived, "source_superseded drifted from the acceptance chain"

    await agrees()
    second = await _accept(admin, newer_id)
    await agrees()
    await _plan(db, seeded=seeded, acceptance_id=second, status=CampaignPlanStatus.DRAFT, version=0)
    await agrees()
    assert (await admin.delete(f"/runs/{newer_id}/accept")).status_code == 200
    await agrees()
    await _accept(admin, seeded["run_id"])
    await agrees()


# ---------------------------------------------------------------------------
# the audit trail (PS4)
# ---------------------------------------------------------------------------


async def test_one_audit_row_per_plan_that_changed_and_none_for_a_no_op(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    first = await _accept(admin, seeded["run_id"])
    assert await _audit(db, AuditAction.PLAN_SOURCE_SUPERSEDED) == []

    frozen = await _plan(
        db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.FROZEN, version=1
    )
    draft = await _plan(
        db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.DRAFT, version=0
    )
    newer_id = await _newer_research(db, seeded)
    await _accept(admin, newer_id)

    marked = await _audit(db, AuditAction.PLAN_SOURCE_SUPERSEDED)
    assert {row.target_id for row in marked} == {frozen, draft}
    assert all(row.actor_id == seeded["user_id"] for row in marked)
    assert all(row.meta["project_id"] == str(seeded["project_id"]) for row in marked)

    # A third acceptance changes nothing for these two — they are already
    # marked — so it writes no rows. "The flag was already right" is not an
    # event, and an audit log that records non-events is one nobody reads.
    third_id = await _newer_research(db, seeded)
    await _accept(admin, third_id)
    assert len(await _audit(db, AuditAction.PLAN_SOURCE_SUPERSEDED)) == 2

    # ...and the way back writes its own action, not a second copy of this one.
    assert (await admin.delete(f"/runs/{third_id}/accept")).status_code == 200
    await _accept(admin, newer_id)
    restored = await _audit(db, AuditAction.PLAN_SOURCE_RESTORED)
    assert restored == []  # these plans point at `first`, which is still not current


# ---------------------------------------------------------------------------
# what the flag is for
# ---------------------------------------------------------------------------


async def test_a_superseded_source_blocks_the_freeze(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """§4.4: "cannot be frozen until it is re-run against the new acceptance".

    The blocker itself is unit-tested; what is new is that a real acceptance
    can now put a plan into the state that raises it.
    """
    first = await _accept(admin, seeded["run_id"])
    plan_run_id = uuid.uuid4()
    run = Run(
        id=plan_run_id,
        workspace_id=seeded["workspace_id"],
        project_id=seeded["project_id"],
        trigger=RunTrigger.MANUAL,
        stage=RunStage.PLAN,
        source_run_id=seeded["run_id"],
        triggered_by=seeded["user_id"],
    )
    db.add(run)
    await db.flush()
    db.add(
        CampaignPlan(
            workspace_id=seeded["workspace_id"],
            project_id=seeded["project_id"],
            plan_run_id=plan_run_id,
            acceptance_id=first,
            schema_version="1.0",
            version=0,
            status=CampaignPlanStatus.READY_TO_FREEZE,
            payload={"envelope": 10_000},
            markdown="# draft",
        )
    )
    await db.commit()

    newer_id = await _newer_research(db, seeded)
    await _accept(admin, newer_id)

    response = await admin.post(f"/plans/{plan_run_id}/freeze", json={"confirm_version": 1})
    assert response.status_code == 409, response.text
    codes = {item["code"] for item in response.json().get("blockers", [])}
    assert "source_superseded" in codes


async def test_the_acceptance_that_a_plan_was_built_from_is_the_one_that_counts(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """A plan built from the *newer* acceptance is not marked by that acceptance.

    The trap in a project-wide refresh is marking everything. The join is on
    `CampaignPlan.acceptance_id`, so a plan started after the re-accept is
    current and stays current.
    """
    first = await _accept(admin, seeded["run_id"])
    old_plan = await _plan(
        db, seeded=seeded, acceptance_id=first, status=CampaignPlanStatus.FROZEN, version=1
    )
    newer_id = await _newer_research(db, seeded)
    second = await _accept(admin, newer_id)
    new_plan = await _plan(
        db, seeded=seeded, acceptance_id=second, status=CampaignPlanStatus.DRAFT, version=0
    )

    assert await _flag(db, old_plan) is True
    assert await _flag(db, new_plan) is False

    third_id = await _newer_research(db, seeded)
    await _accept(admin, third_id)
    assert await _flag(db, new_plan) is True
