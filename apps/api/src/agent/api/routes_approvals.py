"""The approvals API (PRD §14): the inbox, the decision, the reassignment.

Authorization here is the part worth reading twice. PRD §6.1 Authorization 3
states the rule in two halves — `user.role ∈ {approval.required_role, admin}`
**and**, when `assignee_id` is set, an identity match — and both halves are
enforced on the server. `Permission.APPROVAL_DECIDE` gets a caller through the
door (it excludes `operator` and `viewer` outright); ownership of *this
particular* gate is checked afterwards, because a permission cannot express
"this one is addressed to someone else".

Deciding a gate is also what resumes a paused run, so the route does three
things in one transaction — the decision, the gate node's new state, and the
audit row — and only then re-queues. If the commit fails, nothing was decided
and nothing was queued.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_approvals import (
    ApprovalDecisionRequest,
    ApprovalDecisionResponse,
    ApprovalItem,
    ApprovalListResponse,
    ReassignRequest,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.db.models import (
    Approval,
    ApprovalStatus,
    Project,
    Run,
    RunStatus,
    User,
    UserRole,
    UserStatus,
)
from agent.db.repos import ApprovalRepo, GateDecider, UserRepo
from agent.db.session import get_session
from agent.orchestrator import approvals as gates
from agent.orchestrator.events import EventType, RunEventStream
from agent.orchestrator.registry import RegistryError, get_registry
from agent.orchestrator.state import LockHolder, RunLock, RunStore
from agent.queue import enqueue_run
from agent.redis_client import get_redis

log = structlog.get_logger(__name__)

router = APIRouter(tags=["approvals"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
Decider = Annotated[Principal, Depends(require(Permission.APPROVAL_DECIDE))]

PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 200


# ---------------------------------------------------------------------------
# inbox
# ---------------------------------------------------------------------------


@router.get("/approvals", response_model=ApprovalListResponse, summary="Approval inbox")
async def list_approvals(
    me: AnyMember,
    db: Db,
    run_id: Annotated[uuid.UUID | None, Query(description="Only this run's gates")] = None,
    mine: Annotated[bool, Query(description="Only gates this caller may decide")] = False,
    approval_status: Annotated[
        ApprovalStatus | None, Query(alias="status", description="pending | approved | …")
    ] = None,
    cursor: Annotated[str | None, Query(description="From a previous response")] = None,
    limit: Annotated[int, Query(ge=1, le=PAGE_SIZE_MAX)] = PAGE_SIZE_DEFAULT,
) -> ApprovalListResponse:
    """Every gate this workspace has opened, oldest first.

    Readable by every role: PRD §4.1 gives `viewer` "read reports, evidence, run
    history", and a gate is part of a run's history. Only `?mine=true` narrows
    to what the caller can act on, and only `POST` can change anything.
    """
    offset = _offset(cursor)
    repo = ApprovalRepo(db, me.workspace_id)
    rows = await repo.page(
        run_id=run_id,
        status=approval_status,
        decidable_by=GateDecider(me.user.id, me.role) if mine else None,
        limit=limit + 1,
        offset=offset,
    )
    has_more = len(rows) > limit
    page = rows[:limit]
    return ApprovalListResponse(
        items=await _items(db, page, me),
        next_cursor=str(offset + limit) if has_more else None,
    )


# ---------------------------------------------------------------------------
# decide
# ---------------------------------------------------------------------------


@router.post(
    "/approvals/{approval_id}",
    response_model=ApprovalDecisionResponse,
    summary="Decide an approval gate",
)
async def decide_approval(
    approval_id: uuid.UUID,
    body: ApprovalDecisionRequest,
    me: Decider,
    request: Request,
    db: Db,
) -> ApprovalDecisionResponse:
    if not body.approved and not body.rejected:
        raise problems.unprocessable("`decision` must be `approve` or `reject`.")

    repo = ApprovalRepo(db, me.workspace_id)
    approval = await repo.get(approval_id)
    if approval is None:
        raise problems.not_found(f"No approval {approval_id}.")
    _assert_may_decide(approval, me)

    if approval.status is not ApprovalStatus.PENDING:
        raise problems.conflict(
            f"This gate was already {approval.status.value}.",
            title="Already decided",
            decided_by=str(approval.decided_by) if approval.decided_by else None,
            status=approval.status.value,
        )

    run = await repo.run_for(approval)
    if run is None:  # pragma: no cover — FK is ON DELETE CASCADE
        raise problems.not_found(f"No run for approval {approval_id}.")

    edited = body.edited_proposal if body.approved else None
    if edited is not None:
        _assert_resumable(approval.node_id, edited)

    try:
        decision = await gates.decide(
            db,
            approval=approval,
            run=run,
            approved=body.approved,
            decided_by=me.user.id,
            note=body.note,
            edited_proposal=edited,
        )
    except gates.ApprovalConflict as exc:
        await db.rollback()
        raise problems.conflict(
            f"This gate was already {exc.approval.status.value}.",
            title="Already decided",
            decided_by=str(exc.approval.decided_by) if exc.approval.decided_by else None,
            status=exc.approval.status.value,
        ) from exc

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.APPROVAL_DECIDED,
        target_type=AuditTarget.APPROVAL,
        target_id=approval.id,
        meta={
            "run_id": str(run.id),
            "node_id": approval.node_id,
            "decision": approval.status.value,
            "edited": body.edited_proposal is not None,
            "note": body.note,
        },
        ip=client_ip(request),
    )
    await db.commit()

    resumed = False
    if decision.resume:
        resumed = await _resume(db, run, approval, me)
    await db.refresh(run)
    return ApprovalDecisionResponse(
        approval=(await _items(db, [approval], me))[0],
        run_status=run.status,
        resumed=resumed,
    )


async def _resume(db: AsyncSession, run: Run, approval: Approval, me: Principal) -> bool:
    """Put a paused run back on the queue.

    The project lock is re-acquired first, exactly as `retry-failed` does: the
    executor released it when it parked, so another operator may legitimately
    have launched something in the meantime. Losing that race means the decision
    still stands — it is recorded and committed — and the run stays paused until
    the project is free, which is better than two executors in one project.
    """
    if approval.status is not ApprovalStatus.APPROVED:
        # A rejected gate resumes nothing: the branch is dead and the run has to
        # be closed out here, because no executor is running to notice.
        await _close_rejected(db, run, approval)
        return False

    redis = get_redis()
    lock = RunLock(redis, run.stage)
    holder = await lock.acquire(
        run.project_id, LockHolder(run_id=run.id, user_id=me.user.id, user_name=me.user.name)
    )
    if holder is not None and holder.run_id != run.id:
        log.warning(
            "approval.resume_blocked",
            run_id=str(run.id),
            holder_run_id=str(holder.run_id),
        )
        return False

    run.status = RunStatus.QUEUED
    run.error = None
    run.finished_at = None
    await db.commit()
    await RunEventStream(redis, run.id).publish(
        EventType.RUN_STATUS,
        run_id=str(run.id),
        status=RunStatus.QUEUED,
        resumed_from=approval.node_id,
    )
    # Not unique: this run id has already been queued, and arq would treat a
    # second enqueue under the same job id as a duplicate and drop it.
    await enqueue_run(run.id, unique=False)
    return True


async def _close_rejected(db: AsyncSession, run: Run, approval: Approval) -> None:
    """End a run whose gate was rejected.

    `gates.decide` already moved the node to `failed`. What is left is the
    bookkeeping the executor would have done if it were still running: the
    branch below the gate is recorded `skipped`, the run ends `failed` naming
    the rejection, the lock goes back and the console is told.
    """
    from agent.orchestrator.dag import get_dag

    store = RunStore(db)
    dag = get_dag(run.stage)
    selection = (run.node_filter or {}).get("node_ids")
    selected = set(selection) if selection else set(dag.node_ids)
    downstream = sorted(dag.descendants(approval.node_id) & selected)
    await store.record_skipped(
        run_id=run.id,
        node_ids=downstream,
        reason=f"gate {approval.node_id} was rejected",
    )
    error = {
        "code": "approval_rejected",
        "message": f"gate {approval.node_id} was rejected",
        "node_id": approval.node_id,
        "approval_id": str(approval.id),
    }
    await store.finish_run(run, status=RunStatus.FAILED, error=error)
    await gates.expire_pending(db, run.id)

    redis = get_redis()
    await RunLock(redis, run.stage).release(run.project_id, run.id)
    await RunEventStream(redis, run.id).publish(
        EventType.RUN_COMPLETED,
        run_id=str(run.id),
        status=RunStatus.FAILED,
        cost_usd=str(run.cost_usd),
        error=error,
    )


# ---------------------------------------------------------------------------
# reassign
# ---------------------------------------------------------------------------


@router.patch(
    "/approvals/{approval_id}/assignee",
    response_model=ApprovalItem,
    summary="Reassign a pending gate",
)
async def reassign_approval(
    approval_id: uuid.UUID,
    body: ReassignRequest,
    me: Decider,
    request: Request,
    db: Db,
) -> ApprovalItem:
    """Hand a gate to someone else. Admin, or the person currently holding it.

    An approver reassigning a gate that is not theirs would be a way to take one
    off a colleague's desk, so the check here is narrower than `decide`: role
    alone is not enough unless the gate is unassigned.
    """
    repo = ApprovalRepo(db, me.workspace_id)
    approval = await repo.get(approval_id)
    if approval is None:
        raise problems.not_found(f"No approval {approval_id}.")
    if approval.status is not ApprovalStatus.PENDING:
        raise problems.conflict(f"This gate was already {approval.status.value}.")

    is_admin = me.role is UserRole.ADMIN
    holds_it = approval.assignee_id is not None and approval.assignee_id == me.user.id
    unassigned_and_eligible = approval.assignee_id is None and gates.may_decide(
        me.role, approval.required_role
    )
    if not (is_admin or holds_it or unassigned_and_eligible):
        raise problems.forbidden(missing_permission="approval_decide")

    previous = approval.assignee_id
    if body.assignee_id is not None:
        # Membership, not account: a gate can only be handed to someone who is
        # in *this* workspace, and the role that qualifies them is the one they
        # hold here.
        target = await UserRepo(db, me.workspace_id).get(body.assignee_id)
        if target is None:
            raise problems.not_found(f"No user {body.assignee_id}.")
        if target.status is not UserStatus.ACTIVE:
            raise problems.unprocessable("That user is not active.")
        if not gates.may_decide(target.role, approval.required_role):
            raise problems.unprocessable(
                f"A {target.role.value} cannot decide a gate that requires "
                f"{approval.required_role.value}."
            )

    await gates.reassign(db, approval=approval, assignee_id=body.assignee_id)
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.APPROVAL_REASSIGNED,
        target_type=AuditTarget.APPROVAL,
        target_id=approval.id,
        meta={
            "run_id": str(approval.run_id),
            "node_id": approval.node_id,
            "from": str(previous) if previous else None,
            "to": str(body.assignee_id) if body.assignee_id else None,
        },
        ip=client_ip(request),
    )
    await db.commit()
    return (await _items(db, [approval], me))[0]


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _assert_resumable(node_id: str, edited: dict[str, Any]) -> None:
    """An edited proposal must still be a shape the DAG can consume (PRD §8.4).

    On approval the branch resumes with `edited_proposal` if there is one, and
    the nodes downstream read it as the gate node's output. An approver who
    deletes a required field, or types a target where a list belongs, would
    otherwise take the run down four nodes later with a `KeyError` naming
    neither them nor the field — so the shape is re-validated here, the gate
    stays `pending`, and the 422 says which field.

    A node the registry does not know is not an error here: research runs
    predate this check, and a gate whose node has since been renamed should
    still be decidable. The edit is then passed through unvalidated, which is
    what happened before this function existed.
    """
    try:
        output_model = get_registry().spec(node_id).output_model
    except RegistryError:
        log.warning("approval.edit_unvalidated", node_id=node_id, reason="node not registered")
        return

    try:
        output_model.model_validate(edited)
    except ValidationError as exc:
        fields = [
            ".".join(str(part) for part in error["loc"]) or "(root)" for error in exc.errors()
        ]
        raise problems.unprocessable(
            f"This edit is not a shape node {node_id} can hand on: "
            + "; ".join(
                f"{field} — {error['msg']}"
                for field, error in zip(fields, exc.errors(), strict=True)
            ),
            title="Edited proposal is invalid",
            node_id=node_id,
            fields=fields,
        ) from exc


def _assert_may_decide(approval: Approval, me: Principal) -> None:
    """PRD §6.1 Authorization 3, both halves."""
    if not gates.may_decide(me.role, approval.required_role):
        raise problems.forbidden(missing_permission="approval_decide")
    if (
        approval.assignee_id is not None
        and approval.assignee_id != me.user.id
        and me.role is not UserRole.ADMIN
    ):
        raise problems.Problem(
            status_code=status.HTTP_403_FORBIDDEN,
            title="Assigned to someone else",
            detail="This gate is assigned to another approver. An admin can reassign it.",
            type_=problems.TYPE_FORBIDDEN,
        )


def _can_decide(approval: Approval, me: Principal) -> bool:
    try:
        _assert_may_decide(approval, me)
    except problems.Problem:
        return False
    return True


def _offset(cursor: str | None) -> int:
    """Cursors are opaque offsets, the same shape `GET /evidence` issues."""
    if cursor is None:
        return 0
    try:
        value = int(cursor)
    except ValueError as exc:
        raise problems.unprocessable(
            "That pagination cursor is not one this endpoint issued."
        ) from exc
    if value < 0:
        raise problems.unprocessable("That pagination cursor is not one this endpoint issued.")
    return value


async def _items(db: AsyncSession, rows: list[Approval], me: Principal) -> list[ApprovalItem]:
    """Decorate approvals with the run, the node's name and the assignee's email."""
    if not rows:
        return []
    registry = get_registry()
    runs = {
        run.id: run
        for run in (
            await db.execute(sa.select(Run).where(Run.id.in_([row.run_id for row in rows])))
        )
        .scalars()
        .all()
    }
    assignee_ids = [row.assignee_id for row in rows if row.assignee_id is not None]
    emails: dict[uuid.UUID, str] = {}
    if assignee_ids:
        emails = {
            user.id: user.email
            for user in (await db.execute(sa.select(User).where(User.id.in_(assignee_ids))))
            .scalars()
            .all()
        }

    # The cross-project inbox (PRD §13.4 F) is one row per gate: which project,
    # who is waiting on it, and how long they have. Resolved here, in two
    # queries, because the alternative is the inbox fetching every project and
    # every run itself.
    projects = {
        project.id: project
        for project in (
            await db.execute(
                sa.select(Project).where(Project.id.in_({run.project_id for run in runs.values()}))
            )
        )
        .scalars()
        .all()
    }
    launched_by = await UserRepo(db, me.workspace_id).names(
        {run.triggered_by for run in runs.values() if run.triggered_by is not None}
    )

    items: list[ApprovalItem] = []
    for row in rows:
        run = runs.get(row.run_id)
        spec = registry.spec(row.node_id) if row.node_id in registry else None
        project = projects.get(run.project_id) if run else None
        items.append(
            ApprovalItem(
                id=row.id,
                run_id=row.run_id,
                project_id=run.project_id if run else uuid.UUID(int=0),
                node_id=row.node_id,
                node_name=spec.name if spec else row.node_id,
                gate_key=row.gate_key,
                status=row.status,
                required_role=row.required_role,
                assignee_id=row.assignee_id,
                assignee_email=emails.get(row.assignee_id) if row.assignee_id else None,
                proposal=row.proposal,
                edited_proposal=row.edited_proposal,
                decision_note=row.decision_note,
                decided_by=row.decided_by,
                decided_at=row.decided_at,
                created_at=row.created_at,
                run_status=run.status if run else RunStatus.FAILED,
                project_name=project.name if project else None,
                run_triggered_by_name=(
                    launched_by.get(run.triggered_by) if run and run.triggered_by else None
                ),
                sla_hours=_sla_hours(project, row.node_id),
                can_decide=row.status is ApprovalStatus.PENDING and _can_decide(row, me),
            )
        )
    return items


def _sla_hours(project: Project | None, node_id: str) -> int | None:
    """The gate's allowance, if the project set one.

    One reader, in `orchestrator.approvals`: the inbox's countdown and the
    reminder job must agree about what an admin typed into the wizard, and two
    parsers of the same JSONB key are how they stop agreeing.
    """
    return None if project is None else gates.sla_hours_for(project, node_id)
