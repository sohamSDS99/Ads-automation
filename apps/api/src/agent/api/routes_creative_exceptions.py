"""H3 — legal exceptions, non-delegable (Stage 04 PRD §8.6, §16 H3 + contract rule 4).

    GET  /creative-runs/{id}/exceptions            READ
    POST /creative-runs/{id}/exceptions/clear      CLAIM_SIGN   the named legal owner only
    POST /creative-runs/{id}/exceptions/withdraw-preview   CREATIVE_EXECUTE   S4-P22, no write
    POST /creative-runs/{id}/exceptions/withdraw   CREATIVE_EXECUTE

`clear` is ONE transaction (§8.6 steps 1–5), and the order of its checks is the
contract:

1. **identity** (403) — CLAIM_SIGN, which no admin holds (Law 23), then the
   caller must be both H3's assignee and the project's *current*
   `signoff_matrix.legal_owner`. Checked before anything is spent, so somebody
   who may not sign cannot burn a proof;
2. **the set** — decisions must cover exactly the H3 set (422), and the
   register hash is recomputed from the rows: a mismatch is a 409 with nothing
   written and the step-up token still unspent;
3. **presence** (401) — the step-up token is consumed *before* idempotency, so
   a captured request cannot replay forever (S3-P3);
4. **idempotency** — an H3 already decided with the same decisions returns its
   receipt (200); different decisions are a 409;
5. **the write** — claims + one signature, the other decisions, a MINOR and a
   pin if a claim was cleared, fallbacks for every refusal, the task and 4.6.3
   closed — one commit — then the run is put back on the queue.

`withdraw` reduces scope and never licenses anything: the exceptions end
`withdrawn`, their assets swap to fallbacks, and H3 ends `not_required` once
none remain open.

Both mutating routes lock H3's task row first and the exception rows second,
so a clear and a withdrawal racing each other serialise instead of deadlocking.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Annotated, Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip, user_agent
from agent.api.schemas_creative_exceptions import (
    ClearReceipt,
    ClearRequest,
    ExceptionDecisionIn,
    ExceptionOut,
    ExceptionSet,
    H3TaskRef,
    Swapped,
    WithdrawPreview,
    WithdrawRequest,
    WithdrawResponse,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, get_redis_client, require
from agent.auth.rbac import Permission
from agent.config import Settings, get_settings
from agent.creative import clearance, repin
from agent.creative.clearance import ClearanceError, Swap
from agent.db.models import (
    CreativeException,
    CreativeExceptionKind,
    CreativeExceptionStatus,
    HumanTask,
    HumanTaskStatus,
    NodeRun,
    NodeRunStatus,
    Run,
    RunStage,
    RunStatus,
)
from agent.db.session import get_session
from agent.guidelines import tasks as task_service
from agent.guidelines.signature import ReauthError, ReauthTokens
from agent.orchestrator.creative_input import current_signoff
from agent.orchestrator.events import EventType, RunEventStream
from agent.orchestrator.state import LockHolder, RunLock
from agent.queue import enqueue_run
from agent.redis_client import get_redis
from agent.schemas.creative_qa import H3Decision, LegalExceptionClearance

log = structlog.get_logger(__name__)

router = APIRouter(tags=["creative"])

Db = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis_client)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
#: H3 rides on CLAIM_SIGN (PRD §5.1), narrowed below to one named person.
LegalOwner = Annotated[Principal, Depends(require(Permission.CLAIM_SIGN))]
CreativeOperator = Annotated[Principal, Depends(require(Permission.CREATIVE_EXECUTE))]

NODE_ID = "4.6.3"
TASK_KEY = "H3"


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@router.get(
    "/creative-runs/{run_id}/exceptions",
    response_model=ExceptionSet,
    summary="The run's legal exceptions and the H3 set hash",
)
async def list_exceptions(run_id: uuid.UUID, me: AnyMember, db: Db) -> ExceptionSet:
    run = await _creative_run(db, me, run_id)
    rows = await _rows(db, run.id)
    card = await _card(db, run.id)
    task = await _task(db, run.id)
    in_set = _in_set(rows, card)
    open_rows = [row for row in rows if row.status is CreativeExceptionStatus.OPEN]
    assets = await clearance.load_tied_assets(db, run.id, open_rows)
    if_rejected = {
        row.id: [Swapped(out=s.out, into=s.into) for s in clearance.plan_swaps([row], assets)]
        for row in open_rows
    }
    return ExceptionSet(
        run_id=run.id,
        set_hash=clearance.register_hash([clearance.view(r) for r in in_set]) if in_set else None,
        exceptions=[_out(row, if_rejected.get(row.id)) for row in rows],
        task=(
            H3TaskRef(task_id=task.id, status=task.status.value, assignee_id=task.assignee_id)
            if task is not None
            else None
        ),
    )


# ---------------------------------------------------------------------------
# clear — the named legal owner only
# ---------------------------------------------------------------------------


@router.post(
    "/creative-runs/{run_id}/exceptions/clear",
    response_model=ClearReceipt,
    summary="Clear or reject every legal exception (the named legal owner only)",
)
async def clear_exceptions(
    run_id: uuid.UUID,
    body: ClearRequest,
    me: LegalOwner,
    request: Request,
    db: Db,
    redis: RedisDep,
    settings: SettingsDep,
) -> ClearReceipt:
    run = await _creative_run(db, me, run_id)
    task = await _task(db, run.id, lock=True)
    if task is None:
        raise problems.conflict(
            "This run has not opened H3: 4.6.3 has not run, or found nothing to clear.",
            title="No legal exceptions",
            code="no_h3",
        )

    # -- layer 2: identity, before any proof is spent ------------------------
    matrix = await current_signoff(db, me.workspace_id, run.project_id)
    if task.assignee_id != me.user.id or matrix is None or matrix.legal_owner_id != me.user.id:
        raise problems.Problem(
            status_code=status.HTTP_403_FORBIDDEN,
            title="Not the legal owner",
            detail=(
                "H3 is decided by the project's named legal owner "
                "(signoff_matrix.legal_owner) and nobody else — there is no role "
                "fallback and no administrator override. If the owner has changed, "
                "reassign the task to them first."
            ),
            type_=problems.TYPE_FORBIDDEN,
        )
    if task.status not in task_service.OUTSTANDING and task.status is not HumanTaskStatus.COMPLETED:
        raise problems.conflict(
            f"H3 is `{task.status.value}`, so there is nothing left to clear.",
            title="Nothing to clear",
            code="h3_closed",
        )

    card = await _card(db, run.id)
    rows = _in_set(await _rows(db, run.id, lock=True), card)
    if not rows:
        raise problems.conflict(
            "Every exception of this run was withdrawn; there is nothing to clear.",
            title="Nothing to clear",
            code="h3_closed",
        )
    decisions = _decisions(body.decisions, rows)

    # -- layer 3: the set as it was read -------------------------------------
    views = [clearance.view(row) for row in rows]
    seen = clearance.register_hash(views)
    if seen != body.set_hash:
        raise problems.conflict(
            "The legal exceptions changed after you read them, so nothing was recorded. "
            "Re-read them and decide again.",
            title="Exceptions have moved",
            code="set_hash_mismatch",
        )
    claims = [row for row in rows if row.kind is CreativeExceptionKind.NEW_CLAIM]
    if task.status is not HumanTaskStatus.COMPLETED:
        try:
            await repin.check_register(db, run.project_id, claims)
        except ClearanceError as exc:
            raise problems.conflict(
                exc.detail, title="Claim already registered", code=exc.code
            ) from exc

    # -- layer 4: presence ---------------------------------------------------
    tokens = ReauthTokens(redis, ttl_seconds=settings.signature_reauth_ttl_seconds)
    try:
        token_id = await tokens.consume(me.user.id, body.reauth_token)
    except ReauthError as exc:
        raise problems.Problem(
            status_code=status.HTTP_401_UNAUTHORIZED,
            title="Step-up required",
            detail=f"{exc}. Confirm your password and decide again.",
            type_=problems.TYPE_UNAUTHENTICATED,
        ) from exc

    # -- layer 5: idempotency on the set -------------------------------------
    digest = clearance.decided_hash(views, {key: d.decision for key, d in decisions.items()})
    if task.status is HumanTaskStatus.COMPLETED:
        stored = task.submitted_payload or {}
        if stored.get("decided_hash") == digest:
            return _stored_receipt(run, task, stored)
        raise problems.conflict(
            "H3 was already decided with different decisions. A decision is final; a "
            "claim decision can be revoked in the Stage 03 register.",
            title="Already decided",
            code="already_decided",
        )

    # -- the write: one transaction ------------------------------------------
    now = datetime.now(UTC)
    try:
        signed = await repin.record_claims(
            db,
            run,
            claims,
            {
                row.id: repin.ClaimDecisionIn(
                    decision=decisions[row.id].decision,
                    note=decisions[row.id].note,
                    expires_at=decisions[row.id].expires_at,
                )
                for row in claims
            },
            signer_id=me.user.id,
            statement=body.statement,
            reauth_token_id=token_id,
            ip=client_ip(request),
            user_agent=user_agent(request),
            now=now,
        )
        for row in rows:
            decided = decisions[row.id]
            row.status = (
                CreativeExceptionStatus.CLEARED
                if decided.decision == "cleared"
                else CreativeExceptionStatus.REJECTED
            )
            row.decided_by = me.user.id
            row.decided_at = now
            row.decision_note = decided.note
            row.set_hash = digest
            row.reauth_token_id = token_id
            row.human_task_id = task.id
            if signed is not None and row.id in signed.records:
                row.claim_record_id = signed.records[row.id].id
                row.signature_id = signed.signature.id
        pinned = (
            await repin.repin(db, run, reviewed_by=me.user.id, now=now)
            if signed is not None and signed.approved
            else None
        )
        refused = [row for row in rows if decisions[row.id].decision == "rejected"]
        swaps = await clearance.swap_to_fallbacks(db, run.id, refused, by_user=me.user.id)
    except ClearanceError as exc:
        await db.rollback()
        raise problems.conflict(
            exc.detail, title="Cannot apply the decision", code=exc.code
        ) from exc

    cleared = [row.id for row in rows if decisions[row.id].decision == "cleared"]
    rejected = [row.id for row in refused]
    signature_id = signed.signature.id if signed is not None else None
    decision = H3Decision(
        decided_by=me.user.id,
        decided_at=now,
        decided_hash=digest,
        statement=body.statement,
        signature_id=signature_id,
        ruleset_version=pinned,
        cleared=cleared,
        rejected=rejected,
    )
    payload: dict[str, Any] = {
        **decision.model_dump(mode="json"),
        "register_hash": seen,
        "swapped": [_swap_json(s) for s in swaps],
        "reauth_token_id": token_id,
    }
    await task_service.complete(db, task, by=me.user.id, payload=payload)
    await _close_node(db, run.id, card, status_="decided", decision=decision, now=now)
    if signed is not None:
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.CLAIM_SIGNED,
            target_type=AuditTarget.CLAIM_SIGNATURE,
            target_id=signed.signature.id,
            meta={
                "creative_run_id": str(run.id),
                "set_hash": signed.signature.set_hash,
                "approved": sum(1 for r in signed.records.values() if r.status.value == "approved"),
                "rejected": sum(1 for r in signed.records.values() if r.status.value == "rejected"),
            },
            ip=client_ip(request),
        )
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CREATIVE_EXCEPTIONS_CLEARED,
        target_type=AuditTarget.RUN,
        target_id=run.id,
        meta={
            "decided_hash": digest,
            "cleared": len(cleared),
            "rejected": len(rejected),
            "ruleset_version": pinned,
        },
        ip=client_ip(request),
    )
    await db.commit()
    log.info(
        "h3.cleared",
        run_id=str(run.id),
        cleared=len(cleared),
        rejected=len(rejected),
        ruleset_version=pinned,
    )
    resumed = await _resume(db, run, me)
    return ClearReceipt(
        run_id=run.id,
        set_hash=digest,
        register_hash=seen,
        decided_by=me.user.id,
        decided_at=now,
        statement=body.statement,
        signature_id=signature_id,
        ruleset_version=pinned,
        cleared=cleared,
        rejected=rejected,
        swapped=[Swapped(out=s.out, into=s.into) for s in swaps],
        resumed=resumed,
    )


# ---------------------------------------------------------------------------
# withdraw — reduces scope, licenses nothing
# ---------------------------------------------------------------------------


@router.post(
    "/creative-runs/{run_id}/exceptions/withdraw",
    response_model=WithdrawResponse,
    summary="Withdraw open legal exceptions; their assets swap to fallbacks",
)
async def withdraw_exceptions(
    run_id: uuid.UUID, body: WithdrawRequest, me: CreativeOperator, request: Request, db: Db
) -> WithdrawResponse:
    run = await _creative_run(db, me, run_id)
    task = await _task(db, run.id, lock=True)
    rows = await _rows(db, run.id, lock=True)
    wanted, chosen = _withdrawable(rows, body.exception_ids)

    now = datetime.now(UTC)
    for row in chosen:
        row.status = CreativeExceptionStatus.WITHDRAWN
        row.decided_by = me.user.id
        row.decided_at = now
        row.decision_note = "withdrawn"
    try:
        swaps = await clearance.swap_to_fallbacks(db, run.id, chosen, by_user=me.user.id)
    except ClearanceError as exc:
        await db.rollback()
        raise problems.conflict(exc.detail, title="Cannot withdraw", code=exc.code) from exc

    card = await _card(db, run.id)
    in_set = _in_set(rows, card)
    ended = (
        task is not None
        and task.status in task_service.OUTSTANDING
        and card is not None
        and not in_set
    )
    if ended and task is not None and card is not None:
        # Nothing is left for the legal owner: H3 ends as if 4.6.2 had found
        # nothing. `not_required` is the terminal state for exactly that.
        task.status = HumanTaskStatus.NOT_REQUIRED
        await _close_node(db, run.id, card, status_="not_required", withdrawn=wanted, now=now)
    elif card is not None and card.status == "required":
        await _note_withdrawn(db, run.id, card, wanted)
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.CREATIVE_EXCEPTIONS_WITHDRAWN,
        target_type=AuditTarget.RUN,
        target_id=run.id,
        meta={"withdrawn": [str(key) for key in wanted], "h3_ended": ended},
        ip=client_ip(request),
    )
    await db.commit()
    resumed = await _resume(db, run, me) if ended else False
    return WithdrawResponse(
        run_id=run.id,
        withdrawn=wanted,
        swapped=[Swapped(out=s.out, into=s.into) for s in swaps],
        h3_status="required" if in_set else "not_required",
        set_hash=clearance.register_hash([clearance.view(r) for r in in_set]) if in_set else None,
        resumed=resumed,
    )


@router.post(
    "/creative-runs/{run_id}/exceptions/withdraw-preview",
    response_model=WithdrawPreview,
    summary="What withdrawing these exceptions would swap and drop. No writes",
)
async def preview_withdraw(
    run_id: uuid.UUID, body: WithdrawRequest, me: CreativeOperator, db: Db
) -> WithdrawPreview:
    """§15.4 I and §15.2 rule 8: the withdraw confirmation states its
    consequence in numbers — "swaps 7 assets to fallbacks and drops 1" —
    before anyone confirms. The numbers are the withdrawal's own planner run
    over the same rows (`clearance.plan_swaps`), refused exactly as the
    withdrawal would refuse, so they cannot disagree with what it then does."""
    run = await _creative_run(db, me, run_id)
    task = await _task(db, run.id)
    rows = await _rows(db, run.id)
    wanted, chosen = _withdrawable(rows, body.exception_ids)
    assets = await clearance.load_tied_assets(db, run.id, chosen)
    try:
        clearance.refuse_frozen(assets)
    except ClearanceError as exc:
        raise problems.conflict(exc.detail, title="Cannot withdraw", code=exc.code) from exc
    swaps = clearance.plan_swaps(chosen, assets)
    card = await _card(db, run.id)
    left = [row for row in _in_set(rows, card) if row.id not in set(wanted)]
    return WithdrawPreview(
        run_id=run.id,
        exception_ids=wanted,
        swapped=[Swapped(out=s.out, into=s.into) for s in swaps],
        swaps=sum(1 for s in swaps if s.into is not None),
        drops=sum(1 for s in swaps if s.into is None),
        h3_ends=(
            task is not None
            and task.status in task_service.OUTSTANDING
            and card is not None
            and not left
        ),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _withdrawable(
    rows: Sequence[CreativeException], given: Sequence[uuid.UUID]
) -> tuple[list[uuid.UUID], list[CreativeException]]:
    """The exceptions a withdrawal names, in the order named — every one of
    this run and still open, or the refusal the withdrawal and its preview
    both give."""
    by_id = {row.id: row for row in rows}
    wanted = list(dict.fromkeys(given))
    unknown = [str(key) for key in wanted if key not in by_id]
    if unknown:
        raise problems.unprocessable(
            f"Exception(s) {', '.join(unknown)} are not exceptions of this run.",
            title="Unknown exceptions",
            code="not_in_run",
        )
    closed = [
        f"{key} ({by_id[key].status.value})"
        for key in wanted
        if by_id[key].status is not CreativeExceptionStatus.OPEN
    ]
    if closed:
        raise problems.conflict(
            f"Only open exceptions can be withdrawn; {', '.join(closed)} are not.",
            title="Exception already decided",
            code="exception_not_open",
        )
    return wanted, [by_id[key] for key in wanted]


async def _creative_run(db: AsyncSession, me: Principal, run_id: uuid.UUID) -> Run:
    run = await db.scalar(
        sa.select(Run).where(Run.id == run_id, Run.workspace_id == me.workspace_id)
    )
    if run is None or run.stage is not RunStage.CREATIVE:
        raise problems.not_found(f"No creative run {run_id}.")
    return run


async def _task(db: AsyncSession, run_id: uuid.UUID, *, lock: bool = False) -> HumanTask | None:
    statement = (
        sa.select(HumanTask)
        .where(HumanTask.guideline_run_id == run_id, HumanTask.task_key == TASK_KEY)
        .order_by(HumanTask.created_at.desc())
        .limit(1)
    )
    if lock:
        statement = statement.with_for_update()
    return (
        await db.execute(statement.execution_options(populate_existing=True))
    ).scalar_one_or_none()


async def _rows(
    db: AsyncSession, run_id: uuid.UUID, *, lock: bool = False
) -> list[CreativeException]:
    statement = (
        sa.select(CreativeException)
        .where(CreativeException.creative_run_id == run_id)
        .order_by(CreativeException.occurrences.desc(), CreativeException.created_at)
    )
    if lock:
        statement = statement.with_for_update()
    return list(
        (await db.execute(statement.execution_options(populate_existing=True))).scalars().all()
    )


async def _node(db: AsyncSession, run_id: uuid.UUID) -> NodeRun | None:
    return (
        await db.execute(
            sa.select(NodeRun)
            .where(NodeRun.run_id == run_id, NodeRun.node_id == NODE_ID)
            .order_by(NodeRun.attempt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _card(db: AsyncSession, run_id: uuid.UUID) -> LegalExceptionClearance | None:
    node = await _node(db, run_id)
    if node is None or not node.output:
        return None
    return LegalExceptionClearance.model_validate(node.output)


def _in_set(
    rows: Sequence[CreativeException], card: LegalExceptionClearance | None
) -> list[CreativeException]:
    """H3's set: the rows 4.6.3 put in front of the legal owner, less withdrawals."""
    if card is None:
        return []
    ids = set(card.exception_ids)
    return [
        row for row in rows if row.id in ids and row.status is not CreativeExceptionStatus.WITHDRAWN
    ]


def _decisions(
    given: Sequence[ExceptionDecisionIn], rows: Sequence[CreativeException]
) -> dict[uuid.UUID, ExceptionDecisionIn]:
    decisions: dict[uuid.UUID, ExceptionDecisionIn] = {}
    for item in given:
        if item.exception_id in decisions:
            raise problems.unprocessable(
                f"Exception {item.exception_id} is decided twice. Nothing was recorded.",
                title="Duplicate decision",
                code="duplicate_decision",
            )
        decisions[item.exception_id] = item
    wanted = {row.id for row in rows}
    missing = sorted(str(key) for key in wanted - decisions.keys())
    extra = sorted(str(key) for key in decisions.keys() - wanted)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"no decision for {', '.join(missing)}")
        if extra:
            parts.append(f"{', '.join(extra)} are not in H3's set")
        raise problems.unprocessable(
            f"H3 is decided as a whole: {'; '.join(parts)}. Nothing was recorded.",
            title="Decisions do not cover the set",
            code="decisions_incomplete",
        )
    return decisions


async def _close_node(
    db: AsyncSession,
    run_id: uuid.UUID,
    card: LegalExceptionClearance | None,
    *,
    status_: str,
    now: datetime,
    decision: H3Decision | None = None,
    withdrawn: Sequence[uuid.UUID] = (),
) -> None:
    """4.6.3 is done: `succeeded`, with the decided (or emptied) card as its output.

    The executor treats only `succeeded` as done and re-runs anything left at
    `awaiting_human_task`, which would open a second H3 on resume.
    """
    node = await _node(db, run_id)
    if node is None or card is None:
        return
    base = card.model_dump(mode="json")
    if status_ == "decided":
        why = "The named legal owner decided every exception."
    else:
        why = "Every exception was withdrawn by an operator; nothing was licensed."
    node.output = LegalExceptionClearance.model_validate(
        {
            **base,
            "status": status_,
            "why": why,
            "withdrawn": [*base.get("withdrawn", []), *(str(key) for key in withdrawn)],
            "decision": decision.model_dump(mode="json") if decision is not None else None,
        }
    ).model_dump(mode="json")
    node.status = NodeRunStatus.SUCCEEDED
    node.error = None
    node.finished_at = now


async def _note_withdrawn(
    db: AsyncSession,
    run_id: uuid.UUID,
    card: LegalExceptionClearance,
    withdrawn: Sequence[uuid.UUID],
) -> None:
    node = await _node(db, run_id)
    if node is None:
        return
    base = card.model_dump(mode="json")
    node.output = LegalExceptionClearance.model_validate(
        {**base, "withdrawn": [*base.get("withdrawn", []), *(str(key) for key in withdrawn)]}
    ).model_dump(mode="json")


async def _resume(db: AsyncSession, run: Run, me: Principal) -> bool:
    """Put the paused run back on the queue (§8.6 step 5), as `approvals._resume` does.

    The decision is already committed; losing the project lock to another run
    leaves this one paused with nothing outstanding, exactly as a gate does.
    """
    if run.status is not RunStatus.AWAITING_HUMAN_TASK:
        return False
    redis = get_redis()
    lock = RunLock(redis, run.stage)
    holder = await lock.acquire(
        run.project_id, LockHolder(run_id=run.id, user_id=me.user.id, user_name=me.user.name)
    )
    if holder is not None and holder.run_id != run.id:
        log.warning("h3.resume_blocked", run_id=str(run.id), holder_run_id=str(holder.run_id))
        return False
    run.status = RunStatus.QUEUED
    run.error = None
    run.finished_at = None
    await db.commit()
    await RunEventStream(redis, run.id).publish(
        EventType.RUN_STATUS, run_id=str(run.id), status=RunStatus.QUEUED, resumed_from=NODE_ID
    )
    await enqueue_run(run.id, unique=False)
    return True


def _stored_receipt(run: Run, task: HumanTask, stored: dict[str, Any]) -> ClearReceipt:
    decision = H3Decision.model_validate(
        {key: stored[key] for key in H3Decision.model_fields if key in stored}
    )
    return ClearReceipt(
        run_id=run.id,
        set_hash=decision.decided_hash,
        register_hash=str(stored.get("register_hash") or ""),
        decided_by=decision.decided_by,
        decided_at=decision.decided_at,
        statement=decision.statement,
        signature_id=decision.signature_id,
        ruleset_version=decision.ruleset_version,
        cleared=decision.cleared,
        rejected=decision.rejected,
        swapped=[Swapped.model_validate(item) for item in stored.get("swapped") or []],
        resumed=False,
    )


def _swap_json(swap: Swap) -> dict[str, str | None]:
    return {"out": str(swap.out), "into": str(swap.into) if swap.into is not None else None}


def _out(row: CreativeException, if_rejected: list[Swapped] | None = None) -> ExceptionOut:
    return ExceptionOut(
        exception_id=row.id,
        kind=row.kind.value,
        status=row.status.value,
        subject=row.subject_text,
        asset_ids=list(row.asset_ids or ()),
        occurrences=row.occurrences,
        evidence_ids=list(row.evidence_ids or ()),
        proposed=dict(row.proposed or {}),
        fallback_asset_ids=list(row.fallback_asset_ids or ()),
        decided_by=row.decided_by,
        decided_at=row.decided_at,
        decision_note=row.decision_note,
        claim_record_id=row.claim_record_id,
        signature_id=row.signature_id,
        if_rejected=if_rejected,
    )
