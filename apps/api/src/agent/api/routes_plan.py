"""The Stage 01 → Stage 02 handshake (Stage 02 PRD §4, §16).

Two manual human acts separate the stages and this module is both of them.
Nothing here chains automatically: a research run finishing does not accept
it, and an acceptance does not start a plan (Stage 02 law 18).

The shape worth noticing is that **eligibility is computed in one place and
read in two**. `GET /plan/eligibility` renders it for a person; `POST
/plan/runs` re-runs the same function before it takes the lock. A UI that
computed its own answer would eventually disagree with the server, and the
disagreement would show up as a button that does nothing.

Ordering inside `accept` is load-bearing. The partial unique index on
`research_acceptance(project_id) WHERE superseded_by IS NULL` is checked at
the end of each *statement*, not at commit, so the previous acceptance is
superseded before the new one becomes current. Reversing those two lines
raises a constraint violation on a transaction that is otherwise correct.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_plan import (
    AcceptedSource,
    AcceptResearchRequest,
    Blocker,
    PlanEligibility,
    PlanRunAccepted,
    PlanVersion,
    PlanVersionList,
    ResearchAcceptanceResponse,
)
from agent.api.throttle import throttle
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.ratelimit import RUN_QUOTA
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.credentials import MissingCredential, resolve_values
from agent.db.models import (
    Approval,
    ApprovalStatus,
    CampaignPlan,
    CampaignPlanStatus,
    CredentialKind,
    Report,
    ResearchAcceptance,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
    UserRole,
)
from agent.db.repos import ProjectRepo, ReportRepo, RunRepo, UserRepo
from agent.db.session import get_session
from agent.orchestrator.launch import LaunchRequest, ProjectBusy, QueueUnavailable, launch
from agent.orchestrator.plan_input import PlanInputError, build_plan_input
from agent.orchestrator.state import RunLock
from agent.redis_client import get_redis

log = structlog.get_logger(__name__)

router = APIRouter(tags=["plan"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
Decider = Annotated[Principal, Depends(require(Permission.APPROVAL_DECIDE))]
PlanOperator = Annotated[Principal, Depends(require(Permission.PLAN_EXECUTE))]


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# accepting research
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{research_run_id}/accept",
    response_model=ResearchAcceptanceResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Accept a finished research report as fit to plan from",
)
async def accept_research(
    research_run_id: uuid.UUID,
    body: AcceptResearchRequest,
    me: Decider,
    request: Request,
    db: Db,
) -> ResearchAcceptanceResponse:
    run = await RunRepo(db, me.workspace_id).get(research_run_id)
    if run is None:
        raise problems.not_found(f"No run {research_run_id}.")
    if run.stage is not RunStage.RESEARCH:
        raise problems.unprocessable(
            "This is a campaign plan run. Acceptance applies to research runs.",
            title="Not a research run",
        )
    if run.status is not RunStatus.SUCCEEDED:
        raise problems.conflict(
            f"Run {research_run_id} is {run.status.value}. Only a run that finished "
            "successfully produces a report worth accepting.",
            title="Run has not finished",
        )

    report = await ReportRepo(db, me.workspace_id).for_run(run.id)
    if report is None:
        raise problems.conflict(
            "This run finished without writing a report, so there is nothing to accept.",
            title="No report",
        )

    undecided = await _undecided_gates(db, run.id)
    if undecided:
        raise problems.conflict(
            "Every research gate must be decided before the report can be accepted. "
            f"Still open: {', '.join(undecided)}.",
            title="Research gates are still open",
            open_gates=undecided,
        )

    readiness = str(report.payload.get("launch_readiness", ""))
    override = (body.override_reason or "").strip() or None
    if readiness == "no_go":
        # E3. Two separate failures, named separately: "you may not do this"
        # and "you may, but you have to say why" are different corrections.
        if me.role is not UserRole.ADMIN and not me.is_superadmin:
            raise problems.conflict(
                "This research says no_go. Only an administrator can accept it, "
                "and only with a written reason.",
                title="Research says no_go",
                required_role=UserRole.ADMIN.value,
            )
        if not override:
            raise problems.conflict(
                "This research says no_go. Accepting it requires a written reason, "
                "which is stored on the acceptance and printed on the plan.",
                title="An override reason is required",
            )
    elif override:
        raise problems.unprocessable(
            f"This research says {readiness or 'go'}, so there is nothing to override. "
            "Leave the reason empty, or put it in the note.",
            title="No override to make",
        )

    acceptance = await _accept(
        db,
        me=me,
        run=run,
        report=report,
        note=body.note,
        override_reason=override,
        readiness=readiness,
        ip=client_ip(request),
    )
    await db.commit()
    return await _acceptance_response(db, me, acceptance)


@router.delete(
    "/runs/{research_run_id}/accept",
    response_model=ResearchAcceptanceResponse,
    summary="Withdraw an acceptance",
)
async def withdraw_acceptance(
    research_run_id: uuid.UUID, me: Decider, request: Request, db: Db
) -> ResearchAcceptanceResponse:
    run = await RunRepo(db, me.workspace_id).get(research_run_id)
    if run is None:
        raise problems.not_found(f"No run {research_run_id}.")

    acceptance = (
        await db.execute(
            sa.select(ResearchAcceptance).where(
                ResearchAcceptance.run_id == run.id,
                ResearchAcceptance.workspace_id == me.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if acceptance is None:
        raise problems.not_found(f"Run {research_run_id} has not been accepted.")
    if not acceptance.is_current:
        raise problems.conflict(
            "That acceptance was already superseded or withdrawn.",
            title="Nothing to withdraw",
        )

    # Withdrawn, not replaced: there is no newer acceptance to point at, so the
    # row points at itself. `superseded_by = id` reads unambiguously as "no
    # longer current, and nothing took its place", and it is what releases the
    # partial unique index so the project can be accepted again.
    acceptance.superseded_by = acceptance.id
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.RESEARCH_ACCEPTANCE_SUPERSEDED,
        target_type=AuditTarget.RESEARCH_ACCEPTANCE,
        target_id=acceptance.id,
        meta=me.audit_meta(
            project_id=str(acceptance.project_id),
            run_id=str(run.id),
            reason="withdrawn",
        ),
        ip=client_ip(request),
    )
    await db.commit()
    return await _acceptance_response(db, me, acceptance)


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/plan/eligibility",
    response_model=PlanEligibility,
    summary="Whether campaign planning can start, and what is stopping it",
)
async def plan_eligibility(project_id: uuid.UUID, me: AnyMember, db: Db) -> PlanEligibility:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")
    return await _eligibility(db, me, project_id)


# ---------------------------------------------------------------------------
# starting a plan run
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/plan/runs",
    response_model=PlanRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start campaign planning",
    # Same reasoning as the research launch: each of these costs dollars and
    # holds a worker. The plan lock allows one per project; this bounds a
    # caller cycling across projects.
    dependencies=[Depends(throttle(RUN_QUOTA))],
)
async def start_plan_run(
    project_id: uuid.UUID, me: PlanOperator, request: Request, db: Db
) -> PlanRunAccepted:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")

    # Re-checked server-side. The Start button reads the same function, but a
    # button is a cache of an answer and this is the answer.
    eligibility = await _eligibility(db, me, project_id)
    if not eligibility.eligible:
        blocking = [item for item in eligibility.blockers if item.severity == "blocker"]
        first = blocking[0]
        raise problems.conflict(
            first.detail,
            title="Campaign planning cannot start yet",
            code=first.code,
            blockers=[item.model_dump() for item in blocking],
        )
    assert eligibility.source is not None  # noqa: S101 — eligible implies a source

    # Before the lock and before the row: an unsupported research schema must
    # cost nothing, not even a lock somebody has to wait two hours to clear
    # (PRD §17 PR3).
    try:
        plan_input = await build_plan_input(
            db, eligibility.source.acceptance_id, workspace_id=me.workspace_id
        )
    except PlanInputError as exc:
        raise problems.unprocessable(
            exc.detail, title="Research cannot be read", code=exc.code
        ) from exc

    try:
        run = await launch(
            db,
            get_redis(),
            LaunchRequest(
                project=project,
                workspace_id=me.workspace_id,
                trigger=RunTrigger.MANUAL,
                actor_id=me.user.id,
                actor_name=me.user.name,
                ip=client_ip(request),
                stage=RunStage.PLAN,
                source_run_id=eligibility.source.research_run_id,
                input_hash=plan_input.content_hash(),
                audit_meta={"acceptance_id": str(eligibility.source.acceptance_id)},
            ),
        )
    except ProjectBusy as busy:
        raise problems.conflict(
            f"{busy.holder.user_name or 'Someone'} is already planning this project.",
            title="A plan run is already in flight",
            code="plan_in_flight",
            holder=busy.holder.as_dict(),
        ) from busy
    except QueueUnavailable as unavailable:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Queue unavailable",
            detail="The plan run was recorded but could not be queued. "
            "Retry it once Redis is back.",
        ) from unavailable

    return PlanRunAccepted(
        run_id=run.id,
        status=run.status,
        source_run_id=eligibility.source.research_run_id,
        input_hash=plan_input.content_hash(),
    )


# ---------------------------------------------------------------------------
# plan history
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/plans",
    response_model=PlanVersionList,
    summary="Plan versions for a project, newest first",
)
async def list_plans(project_id: uuid.UUID, me: AnyMember, db: Db) -> PlanVersionList:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")

    rows = list(
        (
            await db.execute(
                sa.select(CampaignPlan)
                .where(
                    CampaignPlan.project_id == project_id,
                    CampaignPlan.workspace_id == me.workspace_id,
                )
                .order_by(CampaignPlan.version.desc())
            )
        )
        .scalars()
        .all()
    )
    names = await UserRepo(db, me.workspace_id).names(
        [row.frozen_by for row in rows if row.frozen_by is not None]
    )
    return PlanVersionList(
        items=[
            PlanVersion(
                id=row.id,
                plan_run_id=row.plan_run_id,
                version=row.version,
                status=row.status,
                schema_version=row.schema_version,
                source_superseded=row.source_superseded,
                frozen_at=row.frozen_at,
                frozen_by=row.frozen_by,
                frozen_by_name=names.get(row.frozen_by) if row.frozen_by else None,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


# ---------------------------------------------------------------------------
# the checks themselves
# ---------------------------------------------------------------------------


async def _eligibility(db: AsyncSession, me: Principal, project_id: uuid.UUID) -> PlanEligibility:
    """PRD §4.2, E1–E8. Pure read: no writes, no locks taken, no side effects.

    Every check runs even after one has already failed. A person looking at
    this screen wants the whole list of what to fix, not the first item of it.
    """
    settings = get_settings()
    blockers: list[Blocker] = []
    source: AcceptedSource | None = None

    acceptance = await _current_acceptance(db, me.workspace_id, project_id)

    # E1 — something accepted at all.
    if acceptance is None:
        latest = await RunRepo(db, me.workspace_id).latest_succeeded(project_id)
        blockers.append(
            Blocker(
                code="no_accepted_research",
                detail=(
                    f"Research run {str(latest.id)[:8]} finished, but nobody has accepted it yet."
                    if latest is not None
                    else "This project has no finished research run to plan from yet."
                ),
                fix_url=(
                    f"/projects/{project_id}/runs/{latest.id}/report"
                    if latest is not None
                    else f"/projects/{project_id}"
                ),
            )
        )
    else:
        report = await db.get(Report, acceptance.report_id)
        payload = report.payload if report else {}
        age_days = max(0, (_utcnow() - acceptance.accepted_at).days)
        names = await UserRepo(db, me.workspace_id).names([acceptance.accepted_by])
        source = AcceptedSource(
            acceptance_id=acceptance.id,
            research_run_id=acceptance.run_id,
            research_report_id=acceptance.report_id,
            research_schema_version=report.schema_version if report else "",
            accepted_by=acceptance.accepted_by,
            accepted_by_name=names.get(acceptance.accepted_by, "Unknown"),
            accepted_at=acceptance.accepted_at,
            note=acceptance.note,
            override_reason=acceptance.override_reason,
            launch_readiness=_readiness(acceptance, payload),
            degraded_sources=[str(item) for item in payload.get("degraded_sources", [])],
            age_days=age_days,
        )

        # E2 — a schema version this stage can read.
        if report is not None and report.schema_version not in (
            supported := settings.plan_supported_research_schemas
        ):
            blockers.append(
                Blocker(
                    code="research_schema_unsupported",
                    detail=(
                        f"This report is research schema {report.schema_version}; campaign "
                        f"planning reads {', '.join(sorted(supported))}. Re-run research to "
                        "produce a report in a supported version."
                    ),
                    fix_url=f"/projects/{project_id}/runs/{acceptance.run_id}",
                )
            )

        # E3 — a no_go verdict, unless an admin already overrode it at
        # acceptance time. The override lives on the acceptance, so by the
        # time it is here the decision has been made and audited.
        if source.launch_readiness == "no_go" and not acceptance.override_reason:
            blockers.append(
                Blocker(
                    code="research_says_no_go",
                    detail=(
                        "The research verdict is no_go. An administrator can accept it "
                        "again with a written reason, which is printed on the plan."
                    ),
                    fix_url=f"/projects/{project_id}/runs/{acceptance.run_id}/report",
                )
            )

        # E7 — staleness. A warning up to the hard limit, a blocker past it.
        if age_days > settings.plan_source_hard_age_days:
            blockers.append(
                Blocker(
                    code="source_stale",
                    detail=(
                        f"This research was accepted {age_days} days ago, past the "
                        f"{settings.plan_source_hard_age_days}-day limit. A forecast built "
                        "on demand data that old is not evidence. Re-run research."
                    ),
                    fix_url=f"/projects/{project_id}",
                )
            )
        elif age_days > settings.plan_source_max_age_days:
            blockers.append(
                Blocker(
                    code="source_stale",
                    severity="warning",
                    detail=(
                        f"This research was accepted {age_days} days ago. The plan can "
                        "still start; its forecast will be that old too."
                    ),
                    fix_url=f"/projects/{project_id}",
                )
            )

        # E5 — a frozen plan already built from this acceptance.
        frozen = (
            await db.execute(
                sa.select(CampaignPlan.version)
                .where(
                    CampaignPlan.acceptance_id == acceptance.id,
                    CampaignPlan.status == CampaignPlanStatus.FROZEN,
                )
                .order_by(CampaignPlan.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if frozen is not None:
            blockers.append(
                Blocker(
                    code="plan_already_frozen",
                    detail=(
                        f"Plan v{frozen} is frozen against this research. Accept a newer "
                        "research run to plan again."
                    ),
                    fix_url=f"/projects/{project_id}/plan",
                )
            )

    # E4 — a plan run already in flight. Checked whatever else is wrong: it is
    # the one blocker whose fix is "wait", and hiding it behind E1 would be
    # confusing on a project that was just re-accepted.
    holder = await RunLock(get_redis(), RunStage.PLAN).holder(project_id)
    if holder is not None:
        blockers.append(
            Blocker(
                code="plan_in_flight",
                detail=(
                    f"{holder.user_name or 'Someone'} started a plan run that is still "
                    "running. Only one plan per project at a time."
                ),
                fix_url=f"/projects/{project_id}/plan/runs/{holder.run_id}",
            )
        )

    # E6 — a model credential. Checked last because it is the one failure that
    # is about the deployment rather than about this project.
    try:
        await resolve_values(db, workspace_id=me.workspace_id, kind=CredentialKind.OPENROUTER)
    except MissingCredential as exc:
        blockers.append(
            Blocker(
                code="missing_credential",
                detail=exc.detail,
                fix_url="/settings/connections",
            )
        )

    # E8 — the caller. A blocker rather than a 403 here: this endpoint is
    # readable by everyone, and "you can see this and you cannot start it" is
    # the honest thing for a viewer's screen to say. `POST /plan/runs` returns
    # the 403.
    if Permission.PLAN_EXECUTE not in me.permissions:
        blockers.append(
            Blocker(
                code="missing_permission",
                detail=(
                    "Starting a campaign plan needs the plan_execute permission, which "
                    f"the {me.role.value} role does not hold."
                ),
                fix_url="/settings/team",
            )
        )

    return PlanEligibility(
        eligible=not any(item.severity == "blocker" for item in blockers),
        blockers=blockers,
        source=source,
    )


def _readiness(acceptance: ResearchAcceptance, payload: dict[str, object]) -> str:
    """The verdict, preferring what was true when it was accepted.

    `launch_readiness_at_acceptance` is a copy taken deliberately (§7.2): a
    later re-run of research must not retroactively change what somebody
    signed off. The payload is the fallback for rows written before that
    column had a value to hold.
    """
    return acceptance.launch_readiness_at_acceptance or str(payload.get("launch_readiness", ""))


async def _current_acceptance(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> ResearchAcceptance | None:
    return (
        await db.execute(
            sa.select(ResearchAcceptance).where(
                ResearchAcceptance.project_id == project_id,
                ResearchAcceptance.workspace_id == workspace_id,
                ResearchAcceptance.superseded_by.is_(None),
            )
        )
    ).scalar_one_or_none()


async def _undecided_gates(db: AsyncSession, run_id: uuid.UUID) -> list[str]:
    """Gate node ids on this run that nobody approved."""
    result = await db.execute(
        sa.select(Approval.node_id)
        .where(Approval.run_id == run_id, Approval.status != ApprovalStatus.APPROVED)
        .order_by(Approval.node_id)
    )
    return sorted(set(result.scalars().all()))


async def _accept(
    db: AsyncSession,
    *,
    me: Principal,
    run: Run,
    report: Report,
    note: str | None,
    override_reason: str | None,
    readiness: str,
    ip: str | None,
) -> ResearchAcceptance:
    """Supersede whatever was current, then make this one current. In that order."""
    previous = await _current_acceptance(db, me.workspace_id, run.project_id)
    existing = (
        await db.execute(sa.select(ResearchAcceptance).where(ResearchAcceptance.run_id == run.id))
    ).scalar_one_or_none()

    if previous is not None and existing is not None and previous.id == existing.id:
        # Already the current acceptance of this run. Idempotent: update what
        # the caller said and leave the index alone.
        existing.note = note
        existing.override_reason = override_reason
        return existing

    acceptance = existing or ResearchAcceptance(
        workspace_id=me.workspace_id,
        project_id=run.project_id,
        run_id=run.id,
        report_id=report.id,
        accepted_by=me.user.id,
        accepted_at=_utcnow(),
        launch_readiness_at_acceptance=readiness,
    )
    acceptance.note = note
    acceptance.override_reason = override_reason
    if existing is None:
        db.add(acceptance)
    await db.flush()  # the new row needs an id before `previous` can point at it

    if previous is not None:
        previous.superseded_by = acceptance.id
        # Flushed on its own so the partial unique index sees one current row
        # at the end of each statement, which is when it is checked.
        await db.flush()
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.RESEARCH_ACCEPTANCE_SUPERSEDED,
            target_type=AuditTarget.RESEARCH_ACCEPTANCE,
            target_id=previous.id,
            meta=me.audit_meta(
                project_id=str(run.project_id),
                superseded_by=str(acceptance.id),
                reason="replaced",
            ),
            ip=ip,
        )

    if existing is not None:
        # Re-accepting a run whose acceptance had been withdrawn or superseded.
        acceptance.superseded_by = None
        acceptance.accepted_by = me.user.id
        acceptance.accepted_at = _utcnow()
        acceptance.launch_readiness_at_acceptance = readiness

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.RESEARCH_ACCEPTED,
        target_type=AuditTarget.RESEARCH_ACCEPTANCE,
        target_id=acceptance.id,
        meta=me.audit_meta(
            project_id=str(run.project_id),
            run_id=str(run.id),
            report_id=str(report.id),
            launch_readiness=readiness,
            superseded=str(previous.id) if previous is not None else None,
        ),
        ip=ip,
    )
    if override_reason:
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.RESEARCH_NO_GO_OVERRIDDEN,
            target_type=AuditTarget.RESEARCH_ACCEPTANCE,
            target_id=acceptance.id,
            meta=me.audit_meta(
                project_id=str(run.project_id),
                run_id=str(run.id),
                override_reason=override_reason,
            ),
            ip=ip,
        )
    return acceptance


async def _acceptance_response(
    db: AsyncSession, me: Principal, acceptance: ResearchAcceptance
) -> ResearchAcceptanceResponse:
    names = await UserRepo(db, me.workspace_id).names([acceptance.accepted_by])
    return ResearchAcceptanceResponse(
        id=acceptance.id,
        project_id=acceptance.project_id,
        run_id=acceptance.run_id,
        report_id=acceptance.report_id,
        accepted_by=acceptance.accepted_by,
        accepted_by_name=names.get(acceptance.accepted_by, "Unknown"),
        accepted_at=acceptance.accepted_at,
        note=acceptance.note,
        launch_readiness_at_acceptance=acceptance.launch_readiness_at_acceptance,
        override_reason=acceptance.override_reason,
        is_current=acceptance.is_current,
    )
