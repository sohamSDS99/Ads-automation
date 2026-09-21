"""PRD §17 P3 acceptance, second half, and the `/approvals` API around it.

    "…halts on 1.1.5, an `approver` (not an `operator`) can resume it, decision
     is audit-logged"

The distinction in that sentence is the whole point of the gate: `operator` is
the role that launches runs and is deliberately the one role that cannot decide
one. So most of this file is about who may act, not about the happy path.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalStatus,
    AuditLog,
    Membership,
    NodeRunStatus,
    Project,
    RunStatus,
    User,
    UserRole,
)
from tests.integration.conftest import ApiClient, make_member
from tests.integration.runs_support import (
    by_output_model,
    execute,
    launch_gate,
    script_gate_run,
)
from tests.openrouter_fake import FakeOpenRouter


async def halted_run(admin: ApiClient, project: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    """Run 1.1.1 → 1.1.5 until the gate stops it. Returns (run, approval)."""
    created = await launch_gate(admin, project.id)
    fake = FakeOpenRouter()
    script_gate_run(fake)
    result = await execute(created["id"], fake)
    assert result.status is RunStatus.AWAITING_APPROVAL, result

    inbox = (await admin.get(f"/approvals?run_id={created['id']}")).json()
    assert len(inbox["items"]) == 1
    return created, inbox["items"][0]


# ---------------------------------------------------------------------------
# the acceptance criterion
# ---------------------------------------------------------------------------


async def test_an_approver_resumes_the_run_and_an_operator_cannot(
    admin: ApiClient, project: Any, signed_in_as: Any, db: AsyncSession
) -> None:
    created, approval = await halted_run(admin, project)
    operator = await signed_in_as("operator")
    approver = await signed_in_as("approver")

    # The role that launched the run is the one role that may not decide it.
    refused = await operator.post(f"/approvals/{approval['id']}", json={"decision": "approve"})
    assert refused.status_code == 403
    assert refused.json()["missing_permission"] == "approval_decide"

    decided = await approver.post(
        f"/approvals/{approval['id']}",
        json={"decision": "approve", "note": "Wording checked against our certificates."},
    )
    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["approval"]["status"] == ApprovalStatus.APPROVED
    assert body["resumed"] is True
    assert body["run_status"] == RunStatus.QUEUED

    # The gate node now holds the approved proposal and the run can finish.
    result = await execute(created["id"], FakeOpenRouter())
    assert result.status is RunStatus.SUCCEEDED
    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert {node["id"]: node["status"] for node in state["nodes"]}["1.1.5"] == (
        NodeRunStatus.SUCCEEDED
    )


async def test_the_decision_is_audit_logged_with_the_deciding_user(
    admin: ApiClient, project: Any, signed_in_as: Any, db: AsyncSession
) -> None:
    created, approval = await halted_run(admin, project)
    approver = await signed_in_as("approver")
    response = await approver.post(
        f"/approvals/{approval['id']}", json={"decision": "approve", "note": "Fine as drafted."}
    )
    assert response.status_code == 200

    rows = (
        (
            await db.execute(
                sa.select(AuditLog)
                .where(AuditLog.action.in_(["approval.requested", "approval.decided"]))
                .order_by(AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )
    actions = [row.action for row in rows]
    assert actions == ["approval.requested", "approval.decided"]

    # Opening the gate has no human actor; deciding it always does.
    assert rows[0].actor_id is None
    assert rows[0].meta["node_id"] == "1.1.5"

    decided = rows[1]
    assert decided.actor_id is not None
    assert decided.target_id == uuid.UUID(approval["id"])
    assert decided.meta["decision"] == "approved"
    assert decided.meta["note"] == "Fine as drafted."
    assert decided.meta["run_id"] == created["id"]

    who = (
        await db.execute(sa.select(Membership).where(Membership.user_id == decided.actor_id))
    ).scalar_one_or_none()
    assert who is not None and who.role is UserRole.APPROVER


async def test_an_edited_proposal_becomes_the_nodes_output(
    admin: ApiClient, project: Any, signed_in_as: Any
) -> None:
    """ "Approve with changes" has to mean something downstream, not be a note."""
    created, approval = await halted_run(admin, project)
    approver = await signed_in_as("approver")
    edited = {**approval["proposal"], "prohibited_claims": ["no guarantees of any kind"]}

    response = await approver.post(
        f"/approvals/{approval['id']}",
        json={"decision": "approve", "edited_proposal": edited, "note": "Tightened."},
    )
    assert response.status_code == 200

    detail = (await admin.get(f"/runs/{created['id']}/nodes/1.1.5")).json()
    assert detail["status"] == NodeRunStatus.SUCCEEDED
    assert detail["output"]["prohibited_claims"] == ["no guarantees of any kind"]


# ---------------------------------------------------------------------------
# rejection
# ---------------------------------------------------------------------------


async def test_a_rejected_gate_ends_the_run_and_does_not_re_ask(
    admin: ApiClient, project: Any, signed_in_as: Any, db: AsyncSession
) -> None:
    created, approval = await halted_run(admin, project)
    approver = await signed_in_as("approver")

    response = await approver.post(
        f"/approvals/{approval['id']}",
        json={"decision": "reject", "note": "The GHS claim is not substantiated."},
    )
    assert response.status_code == 200
    assert response.json()["resumed"] is False

    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert state["status"] == RunStatus.FAILED
    assert state["error"]["code"] == "approval_rejected"
    assert state["error"]["node_id"] == "1.1.5"

    node = (await admin.get(f"/runs/{created['id']}/nodes/1.1.5")).json()
    assert node["status"] == NodeRunStatus.FAILED
    assert node["error"]["code"] == "approval_rejected"

    # Re-entering the executor must not re-run the gate and ask again.
    again = await execute(created["id"], FakeOpenRouter())
    assert again.nodes_executed == 0
    pending = (await db.execute(sa.select(Approval))).scalars().all()
    assert [row.status for row in pending] == [ApprovalStatus.REJECTED]


# ---------------------------------------------------------------------------
# who may decide
# ---------------------------------------------------------------------------


async def test_a_viewer_can_read_the_inbox_but_not_decide(
    admin: ApiClient, project: Any, signed_in_as: Any
) -> None:
    """PRD §4.1 gives every role the run history; a gate is part of it."""
    _, approval = await halted_run(admin, project)
    viewer = await signed_in_as("viewer")

    listed = await viewer.get("/approvals")
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [approval["id"]]
    assert listed.json()["items"][0]["can_decide"] is False

    refused = await viewer.post(f"/approvals/{approval['id']}", json={"decision": "approve"})
    assert refused.status_code == 403


async def test_mine_is_what_i_may_decide_not_what_is_addressed_to_me(
    admin: ApiClient, project: Any, signed_in_as: Any
) -> None:
    """`assignee_id IS NULL` means any holder of the role may decide (PRD §6)."""
    _, approval = await halted_run(admin, project)
    approver = await signed_in_as("approver")
    operator = await signed_in_as("operator")

    assert [
        item["id"] for item in (await approver.get("/approvals?mine=true")).json()["items"]
    ] == [approval["id"]]
    assert (await operator.get("/approvals?mine=true")).json()["items"] == []
    # An admin may decide any gate, so an unassigned one is theirs too.
    assert len((await admin.get("/approvals?mine=true")).json()["items"]) == 1


async def test_an_assigned_gate_is_refused_to_another_approver(
    admin: ApiClient, project: Any, signed_in_as: Any, db: AsyncSession
) -> None:
    """PRD §6.1 Authorization 3: role **and**, when set, an identity match."""
    owner_email, password = await make_member(admin, "approver", email="legal@example.com")
    owner = (await db.execute(sa.select(User).where(User.email == owner_email))).scalar_one()
    await db.execute(
        sa.update(Project)
        .where(Project.id == project.id)
        .values(settings={"gate_assignees": {"1.1.5": str(owner.id)}})
    )
    await db.commit()

    _, approval = await halted_run(admin, project)
    assert approval["assignee_id"] == str(owner.id)
    assert approval["assignee_email"] == owner_email

    other = await signed_in_as("approver")
    refused = await other.post(f"/approvals/{approval['id']}", json={"decision": "approve"})
    assert refused.status_code == 403
    assert "assigned to another approver" in refused.json()["detail"]

    # An admin may still decide it — and reassign it.
    allowed = await admin.post(f"/approvals/{approval['id']}", json={"decision": "approve"})
    assert allowed.status_code == 200


async def test_an_assignee_who_is_no_longer_active_falls_back_to_the_role(
    admin: ApiClient, project: Any, signed_in_as: Any, db: AsyncSession
) -> None:
    """PRD §16: a disabled approver must not leave a gate addressed to nobody."""
    gone_email, _ = await make_member(admin, "approver", email="left@example.com")
    gone = (await db.execute(sa.select(User).where(User.email == gone_email))).scalar_one()
    await db.execute(
        sa.update(Project)
        .where(Project.id == project.id)
        .values(settings={"gate_assignees": {"1.1.5": str(gone.id)}})
    )
    await db.commit()
    disabled = await admin.patch(f"/users/{gone.id}", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text

    _, approval = await halted_run(admin, project)
    assert approval["assignee_id"] is None, "a disabled assignee falls back to the whole role"

    other = await signed_in_as("approver")
    assert (
        await other.post(f"/approvals/{approval['id']}", json={"decision": "approve"})
    ).status_code == 200


# ---------------------------------------------------------------------------
# races and reassignment
# ---------------------------------------------------------------------------


async def test_the_second_decision_loses_and_is_told_who_won(
    admin: ApiClient, project: Any, signed_in_as: Any
) -> None:
    """PRD §6.1 Concurrency: `UPDATE … WHERE status='pending'`; the loser gets 409."""
    _, approval = await halted_run(admin, project)
    approver = await signed_in_as("approver")

    first = await approver.post(f"/approvals/{approval['id']}", json={"decision": "approve"})
    assert first.status_code == 200

    second = await admin.post(f"/approvals/{approval['id']}", json={"decision": "reject"})
    assert second.status_code == 409
    body = second.json()
    assert body["status"] == "approved"
    assert body["decided_by"]


async def test_an_admin_can_reassign_a_pending_gate(
    admin: ApiClient, project: Any, signed_in_as: Any, db: AsyncSession
) -> None:
    _, approval = await halted_run(admin, project)
    target_email, _ = await make_member(admin, "approver", email="data-officer@example.com")
    target = (await db.execute(sa.select(User).where(User.email == target_email))).scalar_one()

    response = await admin.patch(
        f"/approvals/{approval['id']}/assignee", json={"assignee_id": str(target.id)}
    )
    assert response.status_code == 200
    assert response.json()["assignee_id"] == str(target.id)

    rows = (
        (await db.execute(sa.select(AuditLog).where(AuditLog.action == "approval.reassigned")))
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].meta["to"] == str(target.id)


async def test_a_gate_cannot_be_assigned_to_someone_who_could_not_decide_it(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    _, approval = await halted_run(admin, project)
    operator_email, _ = await make_member(admin, "operator")
    operator = (await db.execute(sa.select(User).where(User.email == operator_email))).scalar_one()

    response = await admin.patch(
        f"/approvals/{approval['id']}/assignee", json={"assignee_id": str(operator.id)}
    )
    assert response.status_code == 422
    assert "operator cannot decide" in response.json()["detail"]


# ---------------------------------------------------------------------------
# a gate on a run that will never resume
# ---------------------------------------------------------------------------


async def test_cancelling_a_paused_run_closes_its_gate(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    """A question nobody can act on must not sit in an inbox looking actionable."""
    created, approval = await halted_run(admin, project)

    cancelled = await admin.post(f"/runs/{created['id']}/cancel")
    assert cancelled.status_code == 202
    state = (await admin.get(f"/runs/{created['id']}")).json()
    assert state["status"] == RunStatus.CANCELLED

    rows = (await db.execute(sa.select(Approval))).scalars().all()
    assert [row.status for row in rows] == [ApprovalStatus.EXPIRED]
    assert (await admin.get("/approvals?status=pending")).json()["items"] == []


async def test_a_gate_is_asked_once_even_if_the_job_is_redelivered(
    admin: ApiClient, project: Any, db: AsyncSession
) -> None:
    """Migration 0004's partial unique index, exercised through the executor."""
    created, _ = await halted_run(admin, project)

    again = await execute(created["id"], FakeOpenRouter())
    assert again.status is RunStatus.AWAITING_APPROVAL
    assert again.nodes_executed == 0, "a re-entered run must not pay for the gate twice"

    rows = (await db.execute(sa.select(Approval))).scalars().all()
    assert len(rows) == 1


async def test_a_gate_is_never_served_from_the_reuse_cache(
    admin: ApiClient, project: Any, signed_in_as: Any, db: AsyncSession
) -> None:
    """The cached value is the proposal, not the decision — reuse would skip the human."""
    first, approval = await halted_run(admin, project)
    approver = await signed_in_as("approver")
    assert (
        await approver.post(f"/approvals/{approval['id']}", json={"decision": "approve"})
    ).status_code == 200
    await execute(first["id"], FakeOpenRouter())

    second = await launch_gate(admin, project.id, reuse_cache=True)
    fake = FakeOpenRouter()
    by_output_model(fake)
    result = await execute(second["id"], fake)

    assert result.status is RunStatus.AWAITING_APPROVAL, "the gate must ask again"
    pending = (
        (await db.execute(sa.select(Approval).where(Approval.status == ApprovalStatus.PENDING)))
        .scalars()
        .all()
    )
    assert len(pending) == 1
    assert pending[0].run_id == uuid.UUID(second["id"])


async def test_deciding_a_gate_whose_run_already_failed_records_but_resumes_nothing(
    admin: ApiClient, project: Any, signed_in_as: Any, db: AsyncSession
) -> None:
    created, approval = await halted_run(admin, project)
    await admin.post(f"/runs/{created['id']}/cancel")

    approver = await signed_in_as("approver")
    response = await approver.post(f"/approvals/{approval['id']}", json={"decision": "approve"})
    # Cancelling expired it, so there is nothing pending left to decide.
    assert response.status_code == 409
    assert response.json()["status"] == "expired"
