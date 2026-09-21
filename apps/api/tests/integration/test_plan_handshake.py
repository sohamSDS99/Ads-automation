"""The Stage 01 → Stage 02 handshake, end to end (Stage 02 PRD §4, §16).

The acceptance criteria of S2-P0 are almost all test-shaped, so this file is
the phase: every E1–E8 blocker is reproduced and its named message asserted, a
concurrent double-start is raced for real against Redis, and the database-level
guarantees — the frozen-plan trigger, the `stage`/`source_run_id` CHECK, the
one-current-acceptance index — are proven by trying to violate them rather than
by reading the migration.

Two things are deliberately *not* mocked. The plan lock is a real `SETNX`
against the suite's Redis, because a lock tested against a fake is a lock
tested against an assumption. And the eligibility endpoint is called through
the API rather than the function, because the thing being checked is what a
browser gets.
"""

from __future__ import annotations

import asyncio
import json
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
    Report,
    ResearchAcceptance,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)
from tests.integration.conftest import ApiClient, build_client
from tests.report_support import golden_payload

pytestmark = pytest.mark.anyio


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


async def _seed_research(
    db: AsyncSession,
    *,
    project_id: uuid.UUID,
    user_id: uuid.UUID,
    workspace_id: uuid.UUID,
    readiness: str = "go",
    schema_version: str = "1.0",
    status: RunStatus = RunStatus.SUCCEEDED,
) -> tuple[Run, Report]:
    """A finished research run and the report it wrote."""
    from agent.export.contract import ResearchReport

    run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=user_id,
        trigger=RunTrigger.MANUAL,
        status=status,
        stage=RunStage.RESEARCH,
    )
    db.add(run)
    await db.flush()

    payload = golden_payload()
    payload["project_id"] = str(project_id)
    payload["run_id"] = str(run.id)
    payload["launch_readiness"] = readiness
    parsed = ResearchReport.model_validate(payload)
    report = Report(
        run_id=run.id,
        schema_version=schema_version,
        payload=json.loads(parsed.model_dump_json()),
        markdown="# report",
    )
    db.add(report)
    await db.commit()
    return run, report


@pytest_asyncio.fixture
async def seeded(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> dict[str, Any]:
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


def _codes(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["code"]: item for item in body["blockers"]}


# ---------------------------------------------------------------------------
# E1..E8 — one test per blocker, each asserting the message it renders
# ---------------------------------------------------------------------------


async def test_e1_no_accepted_research_is_the_only_blocker_and_names_the_run(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    """The acceptance criterion, verbatim: exactly one blocker, coded E1."""
    response = await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")
    assert response.status_code == 200, response.text
    body = response.json()

    assert body["eligible"] is False
    assert [item["code"] for item in body["blockers"]] == ["no_accepted_research"]
    blocker = body["blockers"][0]
    assert str(seeded["run_id"])[:8] in blocker["detail"]
    assert "nobody has accepted it yet" in blocker["detail"]
    assert blocker["fix_url"] == f"/projects/{seeded['project_id']}/runs/{seeded['run_id']}/report"
    assert body["source"] is None


async def test_e2_an_unsupported_research_schema_names_both_versions(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    run, _ = await _seed_research(
        db,
        project_id=project.id,
        user_id=uuid.UUID(me["id"]),
        workspace_id=workspace_id,
        schema_version="0.9",
    )
    accepted = await admin.post(f"/runs/{run.id}/accept", json={})
    assert accepted.status_code == 201, accepted.text

    body = (await admin.get(f"/projects/{project.id}/plan/eligibility")).json()
    blocker = _codes(body)["research_schema_unsupported"]
    assert "0.9" in blocker["detail"] and "1.0" in blocker["detail"]
    assert body["eligible"] is False


async def test_e3_a_no_go_verdict_blocks_until_an_admin_writes_a_reason(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    run, _ = await _seed_research(
        db,
        project_id=project.id,
        user_id=uuid.UUID(me["id"]),
        workspace_id=workspace_id,
        readiness="no_go",
    )
    accepted = await admin.post(
        f"/runs/{run.id}/accept", json={"override_reason": "Board accepted the risk in writing."}
    )
    assert accepted.status_code == 201, accepted.text

    body = (await admin.get(f"/projects/{project.id}/plan/eligibility")).json()
    # The override was recorded at acceptance, so E3 is satisfied rather than
    # waived: the reason travels with the plan.
    assert "research_says_no_go" not in _codes(body)
    assert body["eligible"] is True
    assert body["source"]["override_reason"] == "Board accepted the risk in writing."
    assert body["source"]["launch_readiness"] == "no_go"


async def test_e4_a_plan_in_flight_blocks_and_names_the_holder(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text
    started = await admin.post(f"/projects/{seeded['project_id']}/plan/runs")
    assert started.status_code == 202, started.text
    run_id = started.json()["run_id"]

    body = (await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()
    blocker = _codes(body)["plan_in_flight"]
    assert "Admin" in blocker["detail"]
    assert blocker["fix_url"].endswith(f"/plan/runs/{run_id}")
    assert body["eligible"] is False


async def test_e5_a_frozen_plan_against_this_acceptance_blocks(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text
    acceptance_id = uuid.UUID(accepted.json()["id"])

    plan_run = Run(
        workspace_id=seeded["workspace_id"],
        project_id=seeded["project_id"],
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.PLAN,
        source_run_id=seeded["run_id"],
    )
    db.add(plan_run)
    await db.flush()
    db.add(
        CampaignPlan(
            workspace_id=seeded["workspace_id"],
            project_id=seeded["project_id"],
            plan_run_id=plan_run.id,
            acceptance_id=acceptance_id,
            schema_version="1.0",
            version=1,
            status=CampaignPlanStatus.FROZEN,
        )
    )
    await db.commit()

    body = (await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()
    assert "Plan v1 is frozen" in _codes(body)["plan_already_frozen"]["detail"]
    assert body["eligible"] is False


async def test_e6_a_missing_model_credential_blocks_and_names_the_kind(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    from agent.db.models import SourceConnection

    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text
    await db.execute(sa.delete(SourceConnection))
    await db.commit()

    body = (await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()
    blocker = _codes(body)["missing_credential"]
    assert "OpenRouter" in blocker["detail"]
    assert blocker["fix_url"] == "/settings/connections"
    assert body["eligible"] is False


async def test_e7_staleness_is_a_warning_first_and_a_blocker_later(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    from datetime import UTC, datetime, timedelta

    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text
    acceptance_id = uuid.UUID(accepted.json()["id"])

    async def age(days: int) -> dict[str, Any]:
        await db.execute(
            sa.update(ResearchAcceptance)
            .where(ResearchAcceptance.id == acceptance_id)
            .values(accepted_at=datetime.now(UTC) - timedelta(days=days))
        )
        await db.commit()
        return (await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()

    fresh = await age(10)
    assert "source_stale" not in _codes(fresh)
    assert fresh["eligible"] is True

    warned = await age(45)
    assert warned["eligible"] is True, "past 30 days is a warning, not a blocker"
    assert _codes(warned)["source_stale"]["severity"] == "warning"
    assert warned["source"]["age_days"] == 45

    blocked = await age(120)
    assert blocked["eligible"] is False
    assert _codes(blocked)["source_stale"]["severity"] == "blocker"
    assert "90-day limit" in _codes(blocked)["source_stale"]["detail"]


async def test_e8_a_viewer_sees_the_plan_but_is_told_they_cannot_start_it(
    admin: ApiClient, signed_in_as: Any, seeded: dict[str, Any]
) -> None:
    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text

    viewer = await signed_in_as("viewer")
    body = (await viewer.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()
    blocker = _codes(body)["missing_permission"]
    assert "plan_execute" in blocker["detail"]
    assert "viewer" in blocker["detail"]
    assert body["eligible"] is False

    refused = await viewer.post(f"/projects/{seeded['project_id']}/plan/runs")
    assert refused.status_code == 403
    assert refused.json()["missing_permission"] == "plan_execute"


# ---------------------------------------------------------------------------
# accepting research
# ---------------------------------------------------------------------------


async def test_accepting_unlocks_the_tab_without_a_page_reload(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    """The acceptance criterion: eligible flips on the next poll of the same URL."""
    before = (await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()
    assert before["eligible"] is False

    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={"note": "Reads well."})
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["is_current"] is True
    assert accepted.json()["note"] == "Reads well."

    after = (await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()
    assert after["eligible"] is True
    assert after["blockers"] == []
    assert after["source"]["accepted_by_name"] == "Admin"
    assert after["source"]["research_run_id"] == str(seeded["run_id"])
    assert after["source"]["age_days"] == 0


async def test_accepting_a_no_go_without_an_override_is_refused_and_writes_nothing(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    run, _ = await _seed_research(
        db,
        project_id=project.id,
        user_id=uuid.UUID(me["id"]),
        workspace_id=workspace_id,
        readiness="no_go",
    )
    before = (await db.execute(sa.select(sa.func.count()).select_from(AuditLog))).scalar_one()

    refused = await admin.post(f"/runs/{run.id}/accept", json={})
    assert refused.status_code == 409, refused.text
    assert "written reason" in refused.json()["detail"]

    db.expire_all()
    rows = (
        await db.execute(sa.select(sa.func.count()).select_from(ResearchAcceptance))
    ).scalar_one()
    after = (await db.execute(sa.select(sa.func.count()).select_from(AuditLog))).scalar_one()
    assert rows == 0
    assert after == before


async def test_an_approver_cannot_override_a_no_go_however_good_the_reason(
    admin: ApiClient, signed_in_as: Any, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    run, _ = await _seed_research(
        db,
        project_id=project.id,
        user_id=uuid.UUID(me["id"]),
        workspace_id=workspace_id,
        readiness="no_go",
    )
    approver = await signed_in_as("approver")
    refused = await approver.post(
        f"/runs/{run.id}/accept", json={"override_reason": "We discussed it."}
    )
    assert refused.status_code == 409, refused.text
    assert refused.json()["required_role"] == "admin"


async def test_an_admin_override_lands_on_the_row_and_in_the_audit_log(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    run, _ = await _seed_research(
        db,
        project_id=project.id,
        user_id=uuid.UUID(me["id"]),
        workspace_id=workspace_id,
        readiness="no_go",
    )
    reason = "CFO signed off on the compliance gap in writing on 12 March."
    accepted = await admin.post(f"/runs/{run.id}/accept", json={"override_reason": reason})
    assert accepted.status_code == 201, accepted.text
    assert accepted.json()["override_reason"] == reason
    assert accepted.json()["launch_readiness_at_acceptance"] == "no_go"

    rows = (
        (
            await db.execute(
                sa.select(AuditLog).where(
                    AuditLog.action == AuditAction.RESEARCH_NO_GO_OVERRIDDEN.value
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].meta["override_reason"] == reason


async def test_an_operator_cannot_accept_research(
    admin: ApiClient, signed_in_as: Any, seeded: dict[str, Any]
) -> None:
    operator = await signed_in_as("operator")
    refused = await operator.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert refused.status_code == 403
    assert refused.json()["missing_permission"] == "approval_decide"


async def test_an_unfinished_run_cannot_be_accepted(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    me = (await admin.get("/auth/me")).json()
    run, _ = await _seed_research(
        db,
        project_id=project.id,
        user_id=uuid.UUID(me["id"]),
        workspace_id=workspace_id,
        status=RunStatus.FAILED,
    )
    refused = await admin.post(f"/runs/{run.id}/accept", json={})
    assert refused.status_code == 409
    assert "failed" in refused.json()["detail"]


async def test_an_open_research_gate_blocks_acceptance(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    from agent.db.models import Approval, ApprovalRequiredRole, ApprovalStatus

    db.add(
        Approval(
            run_id=seeded["run_id"],
            node_id="1.1.5",
            status=ApprovalStatus.PENDING,
            required_role=ApprovalRequiredRole.APPROVER,
        )
    )
    await db.commit()

    refused = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert refused.status_code == 409, refused.text
    assert refused.json()["open_gates"] == ["1.1.5"]


async def test_a_second_acceptance_supersedes_the_first_rather_than_colliding(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any], workspace_id: uuid.UUID
) -> None:
    """The partial unique index is checked per statement, so ordering matters."""
    first = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert first.status_code == 201, first.text

    newer, _ = await _seed_research(
        db,
        project_id=seeded["project_id"],
        user_id=seeded["user_id"],
        workspace_id=workspace_id,
    )
    newer_id = newer.id
    second = await admin.post(f"/runs/{newer_id}/accept", json={})
    assert second.status_code == 201, second.text

    # Read before expiring: `expire_all` would make `newer.id` a lazy load,
    # and a lazy load in a sync attribute access is a MissingGreenlet.
    db.expire_all()
    rows = (
        (await db.execute(sa.select(ResearchAcceptance).order_by(ResearchAcceptance.created_at)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    current = [row for row in rows if row.superseded_by is None]
    assert len(current) == 1
    assert current[0].run_id == newer_id
    superseded = [row for row in rows if row.superseded_by is not None][0]
    assert superseded.superseded_by == current[0].id


async def test_withdrawing_an_acceptance_relocks_the_tab(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text
    assert (await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()[
        "eligible"
    ] is True

    withdrawn = await admin.delete(f"/runs/{seeded['run_id']}/accept")
    assert withdrawn.status_code == 200, withdrawn.text
    assert withdrawn.json()["is_current"] is False

    body = (await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()
    assert body["eligible"] is False
    assert "no_accepted_research" in _codes(body)

    # And the same run can be accepted again: `run_id` is unique, so the row is
    # revived rather than duplicated.
    again = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert again.status_code == 201, again.text
    assert again.json()["id"] == accepted.json()["id"]


# ---------------------------------------------------------------------------
# starting a plan run
# ---------------------------------------------------------------------------


async def test_starting_a_plan_writes_a_plan_run_with_its_source_and_hash(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text

    started = await admin.post(f"/projects/{seeded['project_id']}/plan/runs")
    assert started.status_code == 202, started.text
    body = started.json()
    assert body["source_run_id"] == str(seeded["run_id"])
    assert len(body["input_hash"]) == 64

    run = await db.get(Run, uuid.UUID(body["run_id"]))
    assert run is not None
    assert run.stage is RunStage.PLAN
    assert run.source_run_id == seeded["run_id"]
    assert run.input_hash == body["input_hash"]

    rows = (
        (
            await db.execute(
                sa.select(AuditLog).where(AuditLog.action == AuditAction.PLAN_STARTED.value)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].meta["stage"] == "plan"
    assert rows[0].meta["input_hash"] == body["input_hash"]


async def test_two_concurrent_starts_create_one_run_and_one_409(
    admin: ApiClient, workspace: Any, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """PRD §4.2 E4, raced against the real lock rather than simulated."""
    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text

    other = build_client()
    async with other.raw:
        signed_in = await other.login(workspace.admin_email, workspace.admin_password)
        assert signed_in.status_code == 200, signed_in.text
        first, second = await asyncio.gather(
            admin.post(f"/projects/{seeded['project_id']}/plan/runs"),
            other.post(f"/projects/{seeded['project_id']}/plan/runs"),
        )

    statuses = sorted([first.status_code, second.status_code])
    assert statuses == [202, 409], f"{first.status_code} / {second.status_code}"
    winner = first if first.status_code == 202 else second
    loser = second if first.status_code == 202 else first

    assert loser.json()["code"] == "plan_in_flight"
    assert loser.json()["holder"]["run_id"] == winner.json()["run_id"]

    db.expire_all()
    count = (
        await db.execute(
            sa.select(sa.func.count()).select_from(Run).where(Run.stage == RunStage.PLAN)
        )
    ).scalar_one()
    assert count == 1


async def test_a_plan_lock_does_not_block_a_research_run(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    """Two pipelines, two keys. A plan in flight must not stop research."""
    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text
    started = await admin.post(f"/projects/{seeded['project_id']}/plan/runs")
    assert started.status_code == 202, started.text

    research = await admin.post(f"/projects/{seeded['project_id']}/runs", json={})
    assert research.status_code == 201, research.text


async def test_an_approver_cannot_start_a_plan_run(
    admin: ApiClient, signed_in_as: Any, seeded: dict[str, Any]
) -> None:
    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text

    approver = await signed_in_as("approver")
    refused = await approver.post(f"/projects/{seeded['project_id']}/plan/runs")
    assert refused.status_code == 403
    assert refused.json()["missing_permission"] == "plan_execute"


async def test_an_unsupported_schema_is_refused_at_trigger_time(
    admin: ApiClient, db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    """PRD §17 PR3: 422 before a run row exists, so zero tokens are spent."""
    me = (await admin.get("/auth/me")).json()
    run, _ = await _seed_research(
        db,
        project_id=project.id,
        user_id=uuid.UUID(me["id"]),
        workspace_id=workspace_id,
        schema_version="0.9",
    )
    accepted = await admin.post(f"/runs/{run.id}/accept", json={})
    assert accepted.status_code == 201, accepted.text

    refused = await admin.post(f"/projects/{project.id}/plan/runs")
    # Eligibility catches it first, which is the same guarantee one step
    # earlier: nothing is written and nothing is locked.
    assert refused.status_code == 409, refused.text
    assert refused.json()["code"] == "research_schema_unsupported"

    db.expire_all()
    plans = (
        await db.execute(
            sa.select(sa.func.count()).select_from(Run).where(Run.stage == RunStage.PLAN)
        )
    ).scalar_one()
    assert plans == 0


async def test_plan_history_is_empty_before_a_plan_exists(
    admin: ApiClient, seeded: dict[str, Any]
) -> None:
    response = await admin.get(f"/projects/{seeded['project_id']}/plans")
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []


# ---------------------------------------------------------------------------
# the guarantees the database makes, not the application
# ---------------------------------------------------------------------------


async def test_a_plan_run_without_a_source_fails_the_check_constraint(
    db: AsyncSession, project: Any, workspace_id: uuid.UUID
) -> None:
    db.add(
        Run(
            workspace_id=workspace_id,
            project_id=project.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.QUEUED,
            stage=RunStage.PLAN,
        )
    )
    with pytest.raises(Exception, match="ck_run_plan_has_source"):
        await db.commit()
    await db.rollback()


async def test_a_research_run_claiming_a_source_fails_the_same_constraint(
    db: AsyncSession, project: Any, workspace_id: uuid.UUID, seeded: dict[str, Any]
) -> None:
    db.add(
        Run(
            workspace_id=workspace_id,
            project_id=project.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.QUEUED,
            stage=RunStage.RESEARCH,
            source_run_id=seeded["run_id"],
        )
    )
    with pytest.raises(Exception, match="ck_run_plan_has_source"):
        await db.commit()
    await db.rollback()


async def test_updating_a_frozen_plan_raises_at_the_database(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """Stage 02 law 17, proven by trying to break it."""
    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text
    acceptance_id = uuid.UUID(accepted.json()["id"])

    plan_run = Run(
        workspace_id=seeded["workspace_id"],
        project_id=seeded["project_id"],
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.PLAN,
        source_run_id=seeded["run_id"],
    )
    db.add(plan_run)
    await db.flush()
    plan = CampaignPlan(
        workspace_id=seeded["workspace_id"],
        project_id=seeded["project_id"],
        plan_run_id=plan_run.id,
        acceptance_id=acceptance_id,
        schema_version="1.0",
        version=1,
        status=CampaignPlanStatus.FROZEN,
        payload={"envelope": 10_000},
        markdown="# v1",
    )
    db.add(plan)
    await db.commit()
    # Read the id out before the rollback below: a rolled-back session expires
    # its instances, and `plan.id` would then be a lazy load in a sync context.
    plan_id = plan.id

    with pytest.raises(Exception, match="is frozen"):
        await db.execute(
            sa.update(CampaignPlan)
            .where(CampaignPlan.id == plan_id)
            .values(payload={"envelope": 99_999})
        )
        await db.commit()
    await db.rollback()

    # The flags a frozen plan still has to be able to change are untouched by
    # the guard: only payload, markdown and version are sealed.
    await db.execute(
        sa.update(CampaignPlan).where(CampaignPlan.id == plan_id).values(source_superseded=True)
    )
    await db.commit()
    db.expire_all()
    refreshed = await db.get(CampaignPlan, plan_id)
    assert refreshed is not None
    assert refreshed.source_superseded is True
    assert refreshed.payload == {"envelope": 10_000}


async def test_two_current_acceptances_for_one_project_hit_the_partial_index(
    db: AsyncSession, seeded: dict[str, Any], workspace_id: uuid.UUID
) -> None:
    newer, newer_report = await _seed_research(
        db,
        project_id=seeded["project_id"],
        user_id=seeded["user_id"],
        workspace_id=workspace_id,
    )
    for run_id, report_id in (
        (seeded["run_id"], seeded["report_id"]),
        (newer.id, newer_report.id),
    ):
        db.add(
            ResearchAcceptance(
                workspace_id=workspace_id,
                project_id=seeded["project_id"],
                run_id=run_id,
                report_id=report_id,
                accepted_by=seeded["user_id"],
                launch_readiness_at_acceptance="go",
            )
        )
    with pytest.raises(Exception, match="uq_research_acceptance_current"):
        await db.commit()
    await db.rollback()


# ---------------------------------------------------------------------------
# the DAG the handshake hands off to
# ---------------------------------------------------------------------------


async def test_the_plan_dag_is_separate_from_the_research_dag() -> None:
    from agent.orchestrator.dag import get_dag

    research = get_dag(RunStage.RESEARCH)
    plan = get_dag(RunStage.PLAN)
    assert set(research.node_ids) & set(plan.node_ids) == set()
    assert plan.waves() == [("2.0.1",), ("2.0.2",)]


async def test_the_two_pipelines_lock_different_keys() -> None:
    from agent.orchestrator.state import lock_key

    project_id = uuid.uuid4()
    assert lock_key(project_id, RunStage.PLAN) == f"project:{project_id}:plan_lock"
    assert lock_key(project_id, RunStage.RESEARCH) == f"run:lock:project:{project_id}"


async def test_a_plan_run_is_executed_by_a_real_arq_worker(
    admin: ApiClient, db: AsyncSession, seeded: dict[str, Any]
) -> None:
    """The queue hop, for the pipeline that did not exist before this phase.

    Every other test here calls the API. This one drains the real queue with a
    real arq worker, because the failure it catches — a plan run that is
    enqueued, picked up, and then executed against the *research* DAG or
    against no DAG at all — passes every test that stops at 202.
    """
    from arq.connections import RedisSettings
    from arq.worker import Worker

    from agent.db.models import NodeRunStatus
    from agent.worker import WorkerSettings
    from tests.integration.conftest import REAL_REDIS_URL

    accepted = await admin.post(f"/runs/{seeded['run_id']}/accept", json={})
    assert accepted.status_code == 201, accepted.text
    started = await admin.post(f"/projects/{seeded['project_id']}/plan/runs")
    assert started.status_code == 202, started.text
    plan_run_id = started.json()["run_id"]

    worker = Worker(
        functions=WorkerSettings.functions,
        redis_settings=RedisSettings.from_dsn(REAL_REDIS_URL),
        burst=True,
        poll_delay=0.01,
        max_jobs=1,
        handle_signals=False,
    )
    try:
        await worker.main()
    finally:
        await worker.close()

    assert worker.jobs_complete == 1, "the plan job was never picked up"
    assert worker.jobs_failed == 0

    state = (await admin.get(f"/runs/{plan_run_id}")).json()
    assert state["status"] == RunStatus.SUCCEEDED
    assert [node["id"] for node in state["nodes"]] == ["2.0.1", "2.0.2"]
    assert [node["status"] for node in state["nodes"]] == [
        NodeRunStatus.SUCCEEDED,
        NodeRunStatus.SUCCEEDED,
    ]

    # The handshake delivered its object: 2.0.1 read the run's input_hash back
    # out, and 2.0.2 saw it. A green run that echoed nothing would only have
    # proved the executor loops.
    first = (await admin.get(f"/runs/{plan_run_id}/nodes/2.0.1")).json()
    assert first["output"]["input_hash"] == started.json()["input_hash"]
    assert first["output"]["research_run_id"] == str(seeded["run_id"])
    assert first["output"]["markets"] == ["US"]
    second = (await admin.get(f"/runs/{plan_run_id}/nodes/2.0.2")).json()
    assert second["output"]["source_confirmed"] is True

    # And the lock came back, so the project can be planned again.
    again = (await admin.get(f"/projects/{seeded['project_id']}/plan/eligibility")).json()
    assert "plan_in_flight" not in _codes(again)
