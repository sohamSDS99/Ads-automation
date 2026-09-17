"""Gate mechanics (PRD §7.2 item 5).

A `gate=True` node does not finish when its output validates. It writes an
`Approval(pending, required_role, assignee_id)`, leaves its own `NodeRun` in
`awaiting_approval` holding the proposal, halts **that branch only**, and the
run ends the pass as `awaiting_approval` with every other branch complete. A
`POST /approvals/{id}` by an authorized human resumes it.

Two things in here are less obvious than the flow, and both are about a race.

**Deciding while the run is still moving.** The gate halts in wave 2, but waves
3 and 4 of other branches keep executing, so an attentive approver can decide
before the executor has finished its pass. If the decision and the parking are
not ordered, the run stalls: the decider sees `running` and does not re-queue,
the executor then parks and nobody ever wakes it. `park()` and `decide()` both
take a row lock on `run` for exactly this reason, so whichever commits first is
seen by the other — either the executor's re-check finds the decision and keeps
going, or the decider finds `awaiting_approval` and re-queues.

**Deciding twice.** Two approvers open the same card. The update is
`UPDATE … WHERE status = 'pending'` (PRD §6.1, Approval race), so the second
one changes zero rows and gets a `409` naming who decided, rather than
overwriting a decision that already resumed the run.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalRequiredRole,
    ApprovalStatus,
    NodeRun,
    NodeRunStatus,
    Project,
    Run,
    RunStatus,
    User,
    UserRole,
    UserStatus,
)

log = structlog.get_logger(__name__)

#: Where the setup wizard stores the default approver per gate (PRD §10):
#: `project.settings["gate_assignees"] = {"1.1.5": "<user id>"}`. Absent or
#: unparseable means "any holder of `required_role`", which is the table's own
#: documented meaning for `assignee_id IS NULL`.
GATE_ASSIGNEES = "gate_assignees"

#: The optional per-gate SLA in hours, written by the same wizard step
#: (`agent.gates.SETTINGS_SLA`). It is a *reminder* clock, never an expiry one:
#: PRD §16 is explicit that there is no auto-approve, so passing the SLA emails
#: the assignee again and changes nothing about the gate.
GATE_SLA_HOURS = "gate_sla_hours"


class ApprovalConflict(RuntimeError):
    """Someone decided this gate first."""

    def __init__(self, approval: Approval) -> None:
        super().__init__(f"approval {approval.id} is already {approval.status.value}")
        self.approval = approval


@dataclass(frozen=True, slots=True)
class Decision:
    """The outcome of `decide()`, and whether the run should be re-queued."""

    approval: Approval
    resume: bool


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# opening a gate
# ---------------------------------------------------------------------------


def assignee_for(project: Project, node_id: str) -> uuid.UUID | None:
    """The default approver this project configured for one gate, if any."""
    mapping = (project.settings or {}).get(GATE_ASSIGNEES)
    if not isinstance(mapping, dict):
        return None
    raw = mapping.get(node_id)
    if not raw:
        return None
    try:
        return uuid.UUID(str(raw))
    except ValueError:
        log.warning("approval.bad_assignee_setting", node_id=node_id, value=str(raw))
        return None


async def open_gate(
    db: AsyncSession,
    *,
    run: Run,
    project: Project,
    node_id: str,
    required_role: ApprovalRequiredRole,
    proposal: dict[str, Any],
) -> Approval:
    """Create the pending approval for a gate that has just produced its proposal.

    Idempotent by way of the partial unique index from migration 0004: a
    redelivered job that re-reaches the same gate finds the open question rather
    than asking it twice.
    """
    existing = await pending_for(db, run_id=run.id, node_id=node_id)
    if existing is not None:
        return existing

    assignee_id = await _valid_assignee(db, project, node_id, required_role)
    approval = Approval(
        run_id=run.id,
        node_id=node_id,
        status=ApprovalStatus.PENDING,
        required_role=required_role,
        assignee_id=assignee_id,
        proposal=proposal,
        due_at=due_at_for(project, node_id),
    )
    db.add(approval)
    await db.flush()
    log.info(
        "approval.opened",
        run_id=str(run.id),
        node_id=node_id,
        approval_id=str(approval.id),
        required_role=required_role.value,
        assignee_id=str(assignee_id) if assignee_id else None,
    )
    return approval


def sla_hours_for(project: Project, node_id: str) -> int | None:
    """The SLA an admin set for this gate, or None if they set none.

    Defensive about the stored shape for the same reason `assignee_for` is: this
    is free-form JSONB an admin edits through a form, and a malformed entry
    should cost the reminder, not the gate.
    """
    raw = project.settings.get(GATE_SLA_HOURS) if project.settings else None
    if not isinstance(raw, dict):
        return None
    value = raw.get(node_id)
    if isinstance(value, bool) or not isinstance(value, int | float | str):
        return None
    try:
        hours = int(value)
    except (TypeError, ValueError):
        return None
    return hours if hours > 0 else None


def due_at_for(project: Project, node_id: str) -> datetime | None:
    """When this gate's SLA runs out, or None when it has no SLA.

    Stamped at open time rather than computed on read, so editing the project's
    SLA later does not silently re-date a question somebody has already been
    sitting on for three days.
    """
    hours = sla_hours_for(project, node_id)
    return None if hours is None else utcnow() + timedelta(hours=hours)


async def _valid_assignee(
    db: AsyncSession,
    project: Project,
    node_id: str,
    required_role: ApprovalRequiredRole,
) -> uuid.UUID | None:
    """The configured assignee, but only if they can still decide this gate.

    PRD §16: "Assigned approver disabled or removed → approval falls back to
    `assignee_id=NULL` (any holder of `required_role`)". Checking it here means
    the fallback happens when the gate opens, not when someone finally notices
    the card is addressed to a deactivated account.
    """
    configured = assignee_for(project, node_id)
    if configured is None:
        return None
    user = await db.get(User, configured)
    if user is None or user.status is not UserStatus.ACTIVE:
        log.info("approval.assignee_unavailable", node_id=node_id, user_id=str(configured))
        return None
    if not may_decide(user.role, required_role):
        log.info(
            "approval.assignee_wrong_role",
            node_id=node_id,
            user_id=str(configured),
            role=user.role.value,
        )
        return None
    return configured


def may_decide(role: UserRole, required_role: ApprovalRequiredRole) -> bool:
    """PRD §6.1 Authorization 3: `user.role ∈ {approval.required_role, admin}`."""
    return role is UserRole.ADMIN or role.value == required_role.value


async def notify_targets(
    db: AsyncSession, approval: Approval, workspace_id: uuid.UUID
) -> list[User]:
    """Who to tell about a pending gate.

    The assignee when there is one; otherwise every active holder of the
    required role, because that is precisely the set entitled to decide it.
    Admins are not blanket-notified: they may decide any gate, but a mail to
    every admin on every gate is noise, and the inbox shows it to them anyway.
    """
    if approval.assignee_id is not None:
        user = await db.get(User, approval.assignee_id)
        return [user] if user is not None and user.status is UserStatus.ACTIVE else []
    rows = await db.execute(
        sa.select(User).where(
            User.workspace_id == workspace_id,
            User.role == UserRole(approval.required_role.value),
            User.status == UserStatus.ACTIVE,
        )
    )
    return list(rows.scalars().all())


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


async def pending_for(db: AsyncSession, *, run_id: uuid.UUID, node_id: str) -> Approval | None:
    result = await db.execute(
        sa.select(Approval).where(
            Approval.run_id == run_id,
            Approval.node_id == node_id,
            Approval.status == ApprovalStatus.PENDING,
        )
    )
    return result.scalar_one_or_none()


async def pending_count(db: AsyncSession, run_id: uuid.UUID) -> int:
    result = await db.execute(
        sa.select(sa.func.count())
        .select_from(Approval)
        .where(Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING)
    )
    return int(result.scalar_one())


# ---------------------------------------------------------------------------
# parking and resuming
# ---------------------------------------------------------------------------


async def park(db: AsyncSession, run: Run) -> bool:
    """Try to end this pass as `awaiting_approval`. False means "keep executing".

    The row lock is the whole point: a decision committed while the executor was
    finishing its last wave must be visible here, or the run parks on a gate
    that is no longer pending and nothing will ever wake it.
    """
    await db.execute(sa.select(Run.id).where(Run.id == run.id).with_for_update())
    outstanding = await pending_count(db, run.id)
    if outstanding == 0:
        await db.commit()
        log.info("approval.park_declined", run_id=str(run.id), reason="all gates decided")
        return False
    run.status = RunStatus.AWAITING_APPROVAL
    run.finished_at = None
    run.error = None
    await db.commit()
    log.info("run.awaiting_approval", run_id=str(run.id), pending=outstanding)
    return True


async def decide(
    db: AsyncSession,
    *,
    approval: Approval,
    run: Run,
    approved: bool,
    decided_by: uuid.UUID,
    note: str | None = None,
    edited_proposal: dict[str, Any] | None = None,
) -> Decision:
    """Record a decision and move the gate's `NodeRun` accordingly.

    Does not commit — the caller writes the audit row in the same transaction
    (PRD §6.1 Authorization 5) and commits both together.
    """
    await db.execute(sa.select(Run.id).where(Run.id == run.id).with_for_update())

    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    updated = await db.execute(
        sa.update(Approval)
        .where(Approval.id == approval.id, Approval.status == ApprovalStatus.PENDING)
        .values(
            status=status,
            decided_by=decided_by,
            decided_at=utcnow(),
            decision_note=note,
            edited_proposal=edited_proposal,
        )
        .returning(Approval.id)
    )
    if updated.scalar_one_or_none() is None:
        await db.refresh(approval)
        raise ApprovalConflict(approval)
    await db.refresh(approval)

    node_run = await _latest_node_run(db, run_id=run.id, node_id=approval.node_id)
    if node_run is not None:
        if approved:
            # The approver's edit is the answer, not the model's draft. Taking
            # `edited_proposal` here is what makes "approve with changes" mean
            # something downstream rather than being a note nobody reads.
            node_run.status = NodeRunStatus.SUCCEEDED
            node_run.output = edited_proposal or approval.proposal
            node_run.error = None
        else:
            node_run.status = NodeRunStatus.FAILED
            node_run.error = {
                "code": "approval_rejected",
                "message": note or "the gate was rejected",
                "decided_by": str(decided_by),
            }
        node_run.finished_at = utcnow()

    return Decision(approval=approval, resume=run.status is RunStatus.AWAITING_APPROVAL)


async def expire_pending(db: AsyncSession, run_id: uuid.UUID) -> int:
    """Close the open gates of a run that has reached a terminal state.

    A question nobody can still act on should not sit in an inbox looking
    actionable, so a run that failed, was cancelled or hit its budget cap takes
    its pending approvals down with it. Commits, because the caller has already
    committed the run's own terminal status.
    """
    result = await db.execute(
        sa.update(Approval)
        .where(Approval.run_id == run_id, Approval.status == ApprovalStatus.PENDING)
        .values(status=ApprovalStatus.EXPIRED, decided_at=utcnow())
        .returning(Approval.id)
    )
    expired = len(result.all())
    if expired:
        await db.commit()
        log.info("approval.expired", run_id=str(run_id), count=expired)
    return expired


async def reassign(
    db: AsyncSession, *, approval: Approval, assignee_id: uuid.UUID | None
) -> Approval:
    """Move a pending gate to a different approver. Does not commit."""
    approval.assignee_id = assignee_id
    return approval


async def _latest_node_run(db: AsyncSession, *, run_id: uuid.UUID, node_id: str) -> NodeRun | None:
    result = await db.execute(
        sa.select(NodeRun)
        .where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
        .order_by(NodeRun.attempt.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()
