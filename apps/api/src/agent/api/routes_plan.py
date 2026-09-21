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
from typing import Annotated, Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_diff import PlanDiffResponse, plan_to_response
from agent.api.schemas_plan import (
    AcceptedSource,
    AcceptResearchRequest,
    Blocker,
    PlanCalcList,
    PlanCalcRow,
    PlanCritique,
    PlanDetail,
    PlanEligibility,
    PlanGateDecision,
    PlanRunAccepted,
    PlanStructureAdGroup,
    PlanStructureCampaign,
    PlanStructureKeyword,
    PlanStructurePage,
    PlanStructureTotals,
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
    NodeRun,
    PlanCalc,
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
from agent.planning.diff import diff_plans, flatten_structure
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
                # `created_at` first, and `version` only as the tiebreak.
                #
                # §15.3 A asks for "newest first", and after migration 0014 every
                # unfrozen plan sits at version 0 — so ordering by version would
                # put a v1 frozen last week *above* a draft created this
                # morning, which is the opposite of newest first. For frozen
                # plans the two orders coincide, because versions are minted in
                # time order; for drafts only this one is right.
                #
                # The compare screen depends on it: it takes the first two rows
                # as the newer and older side of the diff, so a wrong order here
                # renders every delta backwards.
                .order_by(CampaignPlan.created_at.desc(), CampaignPlan.version.desc())
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


@router.get(
    "/plans/{plan_run_id}/calcs",
    response_model=PlanCalcList,
    summary="The calculations behind a plan run's numbers",
)
async def list_plan_calcs(
    plan_run_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    node_id: str | None = None,
) -> PlanCalcList:
    """`GET /plans/{plan_run_id}/calcs?node_id=` — PRD §16.

    The read side of law 14. `node_id` is the console's filter: the Calc tab
    asks for one node at a time, and the Plan Viewer asks for the lot.

    Scoped through the run rather than off `plan_calc` directly — the table has
    no `workspace_id` of its own, so the run it belongs to is what decides who
    may read it. A research run simply has no calc rows and answers `[]`.
    """
    run = await RunRepo(db, me.workspace_id).get(plan_run_id)
    if run is None:
        raise problems.not_found(f"No run {plan_run_id}.")

    query = sa.select(PlanCalc).where(PlanCalc.plan_run_id == plan_run_id)
    if node_id is not None:
        query = query.where(PlanCalc.node_id == node_id)
    # Oldest first: the order they were computed in is the order that reads as
    # an argument, and a node's later figures are usually built on its earlier
    # ones.
    #
    # `created_at` alone is not an order. It defaults to `now()`, which in
    # Postgres is the *transaction* timestamp, so every row a node writes in one
    # commit shares it and the sort is then whatever the heap felt like. The
    # remaining three columns are the table's unique key, so this is total.
    rows = list(
        (
            await db.execute(
                query.order_by(
                    PlanCalc.created_at,
                    PlanCalc.node_id,
                    PlanCalc.formula_id,
                    PlanCalc.inputs_hash,
                )
            )
        )
        .scalars()
        .all()
    )

    return PlanCalcList(
        items=[
            PlanCalcRow(
                id=row.id,
                node_id=row.node_id,
                formula_id=row.formula_id,
                calc_version=row.calc_version,
                inputs=row.inputs,
                result=row.result,
                evidence_id=row.evidence_id,
                created_at=row.created_at,
            )
            for row in rows
        ]
    )


# ---------------------------------------------------------------------------
# the plan — the read side (PRD §15.3 D, F; §16 "The plan")
# ---------------------------------------------------------------------------

#: Law 16's four, in the order the plan decides them. Used for the freeze
#: dialog's checklist, so a gate that never fired is still a named row.
PLAN_GATES: tuple[str, ...] = ("G1", "G2", "G3", "G4")

#: The node whose output is the critique verdict §12.2 asserts on.
CRITIQUE_NODE = "2.6.2"

#: Campaigns per page of `GET /plans/{id}/structure`. §16 rule 4 forbids
#: returning a 4,000-keyword plan in one payload; a campaign carries its whole
#: subtree, so the page size is in campaigns and this is the cap that keeps a
#: page to roughly a hundred keywords.
STRUCTURE_PAGE = 10
MAX_STRUCTURE_PAGE = 40


@router.get(
    "/plans/{plan_run_id}",
    response_model=PlanDetail,
    summary="One plan version, whole",
)
async def get_plan(plan_run_id: uuid.UUID, me: AnyMember, db: Db) -> PlanDetail:
    """`GET /plans/{plan_run_id}` — PRD §16, the Plan Viewer's one fetch.

    Everything §15.3 D and E need arrives in this response: the payload, the
    source acceptance, the four gate decisions, the critique verdict and the
    tree's totals. That is deliberate. The freeze dialog has to state the
    campaign and keyword counts and name who decided each gate *before* anyone
    types a version, and a dialog that opened four requests to say so would
    render in pieces — or worse, render a count taken from whichever page of
    the structure happened to be in cache.
    """
    plan = await _plan_or_404(db, me, plan_run_id)
    return await _plan_detail(db, me, plan)


@router.get(
    "/plans/{plan_run_id}/structure",
    response_model=PlanStructurePage,
    summary="The account structure, paginated by campaign",
)
async def get_plan_structure(
    plan_run_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    cursor: str | None = None,
    limit: int = STRUCTURE_PAGE,
) -> PlanStructurePage:
    """`GET /plans/{plan_run_id}/structure?cursor=&limit=` — PRD §16 rule 4.

    Cursor-paginated by campaign. The cursor is the identity of the next
    campaign — `ref@market`, because 2.4.2 emits one campaign per ref per
    market and a ref alone names two of them in a two-market plan. An identity
    that is no longer in the payload answers `400` rather than silently
    restarting at the top: a reader paging through 40 campaigns should be told
    the plan was rewritten underneath them, not shown page one again as if it
    were page five.

    `totals` counts the whole plan, never the page. A reader on page one still
    needs to know how much tree there is, and the freeze dialog quotes the same
    numbers from `GET /plans/{id}`.
    """
    if limit < 1 or limit > MAX_STRUCTURE_PAGE:
        raise problems.unprocessable(
            f"`limit` must be between 1 and {MAX_STRUCTURE_PAGE}; got {limit}.",
        )

    plan = await _plan_or_404(db, me, plan_run_id)
    structure = plan.payload.get("account_structure") if isinstance(plan.payload, dict) else None
    structure = structure if isinstance(structure, dict) else {}

    campaigns = [row for row in structure.get("campaigns", []) if isinstance(row, dict)]
    identities = [_campaign_identity(row) for row in campaigns]

    start = 0
    if cursor is not None:
        if cursor not in identities:
            raise problems.unprocessable(
                f"No campaign {cursor!r} in this plan. The structure has been rewritten since "
                "that page was read; start again from the first page.",
            )
        start = identities.index(cursor)

    page = campaigns[start : start + limit]
    checks = _volume_checks(plan.payload)
    # `None` when the key is absent, an empty set when it is an empty list. The
    # difference is the whole point: `plan_contract.py` does not declare
    # `invalid_names`, so a payload that omits it has told us nothing about any
    # name, and a green tick on all forty would be this screen asserting
    # something nobody checked. An empty list *is* an answer — nothing failed.
    declared = structure.get("invalid_names")
    invalid = {str(name) for name in declared if name} if isinstance(declared, list) else None
    duplicates = structure.get("duplicate_terms")
    convention = structure.get("naming_convention")
    regex = _validator_regex(plan.payload)
    flat = flatten_structure(structure)

    return PlanStructurePage(
        plan_run_id=plan.plan_run_id,
        version=plan.version,
        status=plan.status,
        totals=PlanStructureTotals(
            campaigns=len(flat["campaigns"]),
            ad_groups=len(flat["ad_groups"]),
            keywords=len(flat["keywords"]),
        ),
        campaigns=[_structure_campaign(row, checks, invalid) for row in page],
        next_cursor=(identities[start + limit] if start + limit < len(identities) else None),
        validator_regex=regex,
        collision_check=(
            _text(convention.get("collision_check")) if isinstance(convention, dict) else None
        ),
        # `if invalid is not None`, never `if invalid`: an empty set is 2.4.2
        # saying every name passed, and `[]` has to survive as `[]` rather than
        # collapsing into the `None` that means nobody checked.
        invalid_names=sorted(invalid) if invalid is not None else None,
        duplicate_terms=(
            sorted(str(term) for term in duplicates if term)
            if isinstance(duplicates, list)
            else None
        ),
        account_negatives=[str(term) for term in structure.get("account_negatives", [])],
        orphan_terms=[str(term) for term in structure.get("orphan_terms", [])],
    )


@router.get(
    "/plans/{plan_run_id}/diff",
    response_model=PlanDiffResponse,
    summary="What changed between two plan versions",
)
async def diff_plan(
    plan_run_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    against: uuid.UUID,
) -> PlanDiffResponse:
    """`GET /plans/{plan_run_id}/diff?against={plan_run_id}` — PRD §15.3 F.

    `against` is the *other* plan's `plan_run_id`, not its row id — one
    identifier for the whole Stage 02 frontend, matching the `/calcs` route
    S2-P6a shipped.

    Both plans must belong to the same project. Comparing two projects' plans
    would produce a diff in which every section changed, which is true and
    useless; refusing says the caller mixed up two versions, which is the
    actual mistake.
    """
    plan = await _plan_or_404(db, me, plan_run_id)
    other = await _plan_or_404(db, me, against)

    if plan.project_id != other.project_id:
        raise problems.unprocessable(
            "Those two plans belong to different projects. A plan can only be compared with "
            "another version of itself.",
        )
    if plan.id == other.id:
        raise problems.unprocessable("A plan is identical to itself; pick two versions.")

    result = diff_plans(
        plan.payload if isinstance(plan.payload, dict) else {},
        other.payload if isinstance(other.payload, dict) else {},
        plan_run_id=plan.plan_run_id,
        against_plan_run_id=other.plan_run_id,
        version=plan.version,
        against_version=other.version,
    )
    return plan_to_response(result)


async def _plan_or_404(db: AsyncSession, me: Principal, plan_run_id: uuid.UUID) -> CampaignPlan:
    """The plan a run produced, scoped to the caller's workspace.

    Scoped on `campaign_plan.workspace_id` directly — unlike `plan_calc`, this
    table carries one. A plan run that has not reached 2.6.1 yet has no plan
    row at all and answers 404, which is the same answer as a plan in another
    workspace and deliberately so.
    """
    plan = (
        await db.execute(
            sa.select(CampaignPlan).where(
                CampaignPlan.plan_run_id == plan_run_id,
                CampaignPlan.workspace_id == me.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if plan is None:
        raise problems.not_found(
            f"No campaign plan for run {plan_run_id}. A plan exists once the run has "
            "synthesised one."
        )
    return plan


async def _plan_detail(db: AsyncSession, me: Principal, plan: CampaignPlan) -> PlanDetail:
    payload = plan.payload if isinstance(plan.payload, dict) else {}
    flat = flatten_structure(payload.get("account_structure"))

    acceptance = await db.get(ResearchAcceptance, plan.acceptance_id)
    source = None
    if acceptance is not None:
        report = await db.get(Report, acceptance.report_id)
        source = await _accepted_source(db, me.workspace_id, acceptance, report)

    names = await UserRepo(db, me.workspace_id).names(
        [plan.frozen_by] if plan.frozen_by is not None else []
    )

    return PlanDetail(
        id=plan.id,
        project_id=plan.project_id,
        plan_run_id=plan.plan_run_id,
        version=plan.version,
        next_version=await _next_version(db, me.workspace_id, plan.project_id),
        status=plan.status,
        schema_version=plan.schema_version,
        source_superseded=plan.source_superseded,
        payload=payload,
        markdown=plan.markdown,
        frozen_at=plan.frozen_at,
        frozen_by=plan.frozen_by,
        frozen_by_name=names.get(plan.frozen_by) if plan.frozen_by else None,
        frozen_approval_ids=list(plan.frozen_approval_ids or []),
        created_at=plan.created_at,
        updated_at=plan.updated_at,
        source=source,
        gates=await _gate_decisions(db, me.workspace_id, plan.plan_run_id),
        critique=await _critique(db, plan.plan_run_id, payload),
        totals=PlanStructureTotals(
            campaigns=len(flat["campaigns"]),
            ad_groups=len(flat["ad_groups"]),
            keywords=len(flat["keywords"]),
        ),
    )


async def _next_version(db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID) -> int:
    """`max(version) + 1` over every plan in the project — §12.2 verbatim.

    No status predicate, and that is the point. A superseded plan is no longer
    `frozen`, so counting only frozen rows would reissue the version a
    superseded plan already holds — and `version` is what a signed-off plan is
    referred to by for the rest of its life. Agreed with S2-P5b, whose freeze
    transaction mints the same number from the same rule, so what the dialog
    asks the approver to type is what the server is about to mint.

    Unfrozen plans all sit at version 0 (migration 0014), so the first freeze in
    a project mints 1.
    """
    highest = (
        await db.execute(
            sa.select(sa.func.max(CampaignPlan.version)).where(
                CampaignPlan.project_id == project_id,
                CampaignPlan.workspace_id == workspace_id,
            )
        )
    ).scalar()
    return int(highest or 0) + 1


async def _gate_decisions(
    db: AsyncSession, workspace_id: uuid.UUID, plan_run_id: uuid.UUID
) -> list[PlanGateDecision]:
    """The four gates, in gate order, decided or not.

    Ordered by `PLAN_GATES` rather than by `decided_at`: §15.3 E is a checklist
    of four known things, and a checklist that re-orders itself as decisions
    land is one a reader has to re-read every time.
    """
    rows = list(
        (
            await db.execute(
                sa.select(Approval).where(
                    Approval.run_id == plan_run_id,
                    Approval.gate_key.in_(PLAN_GATES),
                )
            )
        )
        .scalars()
        .all()
    )
    # Deciders come off the rows already loaded rather than from a second
    # query, and `names()` answers for ids outside the workspace too — a gate
    # decided by someone whose membership was later revoked still has to render
    # who decided it.
    names = await UserRepo(db, workspace_id).names(
        [row.decided_by for row in rows if row.decided_by is not None]
    )

    by_gate = {row.gate_key: row for row in rows}
    decisions: list[PlanGateDecision] = []
    for gate in PLAN_GATES:
        row = by_gate.get(gate)
        if row is None:
            decisions.append(PlanGateDecision(gate_key=gate, node_id="", status="not_reached"))
            continue
        decisions.append(
            PlanGateDecision(
                gate_key=gate,
                node_id=row.node_id,
                status=row.status.value,
                decided_by=row.decided_by,
                decided_by_name=names.get(row.decided_by) if row.decided_by else None,
                decided_at=row.decided_at,
                note=row.decision_note,
                edited=row.edited_proposal is not None,
            )
        )
    return decisions


async def _critique(
    db: AsyncSession, plan_run_id: uuid.UUID, payload: dict[str, Any]
) -> PlanCritique | None:
    """Node 2.6.2's verdict — from the payload first, then the node run.

    **The payload is authoritative and the node run is the fallback.** S2-P5b
    writes `critique_issues[]` onto the payload precisely so an exported PDF
    carries its own review: a document that says "ready to freeze" while the
    critique that said otherwise lives somewhere else is how a blocked plan
    gets circulated as approved. The node run still answers for a run that
    reached 2.6.2 but whose payload predates it.

    Getting this wrong is the worst failure available to this screen. An earlier
    version of this function read only the node output, in shapes 2.6.2 does not
    emit, so the freeze dialog would have reported "no critique recorded" on a
    plan with blocking issues.
    """
    issues = payload.get("critique_issues")
    if isinstance(issues, list) and issues:
        blocking = _findings(issues, ("blocking",))
        return PlanCritique(
            # No `verdict` field on the payload: the verdict *is* whether
            # anything blocking survived, and deriving it here keeps one answer
            # rather than a label that can disagree with the list under it.
            verdict="blocking_issues" if blocking else "pass",
            blocking=blocking,
            advisory=_findings(issues, ("warning", "note")),
            checked_at=None,
        )

    row = (
        await db.execute(
            sa.select(NodeRun)
            .where(NodeRun.run_id == plan_run_id, NodeRun.node_id == CRITIQUE_NODE)
            .order_by(NodeRun.attempt.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if row is None or not isinstance(row.output, dict):
        return None
    return PlanCritique(
        verdict=_text(row.output.get("verdict")),
        blocking=_issues(row.output, "blocking"),
        advisory=_issues(row.output, "advisory"),
        checked_at=row.finished_at,
    )


def _findings(issues: list[Any], severities: tuple[str, ...]) -> list[str]:
    """`critique_issues[]` rows at the given severities, as sentences.

    The row carries `finding` and `fix`; both are shown, because a blocking
    issue a reader cannot act on is just an obstacle. `check` is a stable id
    (`1_allocation_sums` … `10_launch_blockers`, or `reader`) and is prefixed so
    the same failure is recognisable across two versions of a plan even when the
    model rewords its prose.
    """
    found: list[str] = []
    for issue in issues:
        if not isinstance(issue, dict) or issue.get("severity") not in severities:
            continue
        finding = issue.get("finding") or issue.get("statement") or issue.get("detail")
        if not finding:
            continue
        fix = issue.get("fix")
        section = issue.get("section")
        prefix = f"{section}: " if section else ""
        suffix = f" Fix: {fix}" if fix else ""
        found.append(f"{prefix}{finding}{suffix}")
    return found


def _issues(output: dict[str, Any], severity: str) -> list[str]:
    """Issues at one severity, in the shapes a *node run* might carry them.

    Kept as the fallback path for a run whose payload has no `critique_issues`:
    a flat `blocking: [...]` list, or `issues: [{severity, statement}]`.
    """
    found = [str(item) for item in output.get(severity, []) if item]
    for issue in output.get("issues", []):
        if not isinstance(issue, dict) or issue.get("severity") != severity:
            continue
        text = issue.get("statement") or issue.get("detail") or issue.get("issue")
        if text:
            found.append(str(text))
    return found


def _text(value: Any) -> str | None:
    return str(value) if isinstance(value, str) and value else None


def _campaign_identity(row: dict[str, Any]) -> str:
    ref = str(row.get("campaign_ref") or row.get("name") or "")
    market = str(row.get("market") or "")
    return f"{ref}@{market}" if market else ref


def _validator_regex(payload: dict[str, Any]) -> str | None:
    """2.4.1's regex, wherever the assembled plan put it.

    `account_structure.naming_convention.validator_regex` is where S2-P5b's
    `plan_contract.py` writes it. The other two are the shapes the node output
    itself carries, kept because a payload written before that model landed is
    still a payload this screen has to render.
    """
    structure = payload.get("account_structure")
    if isinstance(structure, dict):
        convention = structure.get("naming_convention")
        if isinstance(convention, dict) and (regex := convention.get("validator_regex")):
            return str(regex)
        if regex := structure.get("validator_regex"):
            return str(regex)
    convention = payload.get("naming_convention")
    if isinstance(convention, dict) and (regex := convention.get("validator_regex")):
        return str(regex)
    return None


def _volume_checks(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """2.4.3's per-campaign verdicts, by campaign ref.

    Keyed on `ref` alone because `CampaignCheck` carries no market — it is one
    row per campaign as 2.4.3 emitted them. Where a ref names two campaigns in
    two markets they share a badge, which is what the node actually said.
    """
    structure = payload.get("account_structure")
    rows = structure.get("volume_check") if isinstance(structure, dict) else None
    if not isinstance(rows, list):
        rows = payload.get("volume_check")
    if isinstance(rows, dict):
        rows = rows.get("campaigns")
    if not isinstance(rows, list):
        return {}
    return {str(row.get("ref")): row for row in rows if isinstance(row, dict) and row.get("ref")}


def _structure_campaign(
    row: dict[str, Any], checks: dict[str, dict[str, Any]], invalid: set[str] | None
) -> PlanStructureCampaign:
    ref = str(row.get("campaign_ref") or "")
    name = str(row.get("name") or ref)
    check = checks.get(ref, {})
    groups = [
        _structure_ad_group(group, invalid)
        for group in row.get("ad_groups", [])
        if isinstance(group, dict)
    ]
    return PlanStructureCampaign(
        campaign_ref=ref,
        name=name,
        type=_text(row.get("type")),
        market=_text(row.get("market")),
        language=_text(row.get("language")),
        monthly_budget_usd=_number(row.get("monthly_budget_usd")),
        daily_budget_usd=_number(row.get("daily_budget_usd")),
        bid_strategy=_text(row.get("bid_strategy")),
        target=_number(row.get("target")),
        locations=[str(place) for place in row.get("locations", [])],
        negatives=[str(term) for term in row.get("negatives", [])],
        name_valid=_name_valid(name, invalid),
        verdict=_text(check.get("verdict")),
        threshold=_number(check.get("threshold")),
        forecast_conv_30d=_number(check.get("forecast_conv_30d")),
        action=_text(check.get("action")),
        remedy=_text(check.get("remedy")),
        reason=_text(check.get("reason")),
        ad_groups=groups,
        ad_group_count=len(groups),
        keyword_count=sum(group.keyword_count for group in groups),
    )


def _structure_ad_group(row: dict[str, Any], invalid: set[str] | None) -> PlanStructureAdGroup:
    name = str(row.get("name") or row.get("theme") or "")
    keywords = [
        PlanStructureKeyword(
            term=str(keyword.get("term") or ""),
            match_type=_text(keyword.get("match_type")),
            forecast_cpc_usd=_number(keyword.get("forecast_cpc_usd")),
            search_volume=(
                int(volume) if isinstance(volume := keyword.get("search_volume"), int) else None
            ),
        )
        for keyword in row.get("keywords", [])
        if isinstance(keyword, dict)
    ]
    return PlanStructureAdGroup(
        name=name,
        theme=_text(row.get("theme")),
        landing_url=_text(row.get("landing_url")),
        primary_message=_text(row.get("primary_message")),
        market=_text(row.get("market")),
        coherence=_number(row.get("coherence")),
        negatives=[str(term) for term in row.get("negatives", [])],
        name_valid=_name_valid(name, invalid),
        keywords=keywords,
        keyword_count=len(keywords),
    )


def _name_valid(name: str, invalid: set[str] | None) -> bool | None:
    """A tick, a cross, or nothing.

    `None` is not a cosmetic third state. A plan whose payload never declares
    `invalid_names` has said nothing about any name — not that every name
    passed — and a green tick there is the screen asserting something nobody
    verified. So: no list, no verdict.
    """
    if not name or invalid is None:
        return None
    return name not in invalid


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


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
        source = await _accepted_source(db, me.workspace_id, acceptance, report)
        # Read off the source rather than recomputed: E7's threshold and the
        # age the landing page prints have to be the same number.
        age_days = source.age_days

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
                # `created_at` first, and `version` only as the tiebreak.
                #
                # §15.3 A asks for "newest first", and after migration 0014 every
                # unfrozen plan sits at version 0 — so ordering by version would
                # put a v1 frozen last week *above* a draft created this
                # morning, which is the opposite of newest first. For frozen
                # plans the two orders coincide, because versions are minted in
                # time order; for drafts only this one is right.
                #
                # The compare screen depends on it: it takes the first two rows
                # as the newer and older side of the diff, so a wrong order here
                # renders every delta backwards.
                .order_by(CampaignPlan.created_at.desc(), CampaignPlan.version.desc())
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


async def _accepted_source(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    acceptance: ResearchAcceptance,
    report: Report | None,
) -> AcceptedSource:
    """The accepted research, as both the eligibility panel and the plan header show it.

    Extracted when the Plan Viewer needed the same five fields in its header
    (§15.3 D). Two copies of this would have drifted on the first field added
    to `AcceptedSource`, and the drift would read as the landing page and the
    plan disagreeing about which research a plan came from.
    """
    payload = report.payload if report else {}
    names = await UserRepo(db, workspace_id).names([acceptance.accepted_by])
    return AcceptedSource(
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
        age_days=max(0, (_utcnow() - acceptance.accepted_at).days),
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
    """Move "current" from one row to another without ever holding two.

    Three statements, and the order is forced by two constraints that pull in
    opposite directions:

    * the partial unique index on `(project_id) WHERE superseded_by IS NULL`
      is checked at the end of every statement, not at commit, so the old row
      must stop being current *before* the new one starts;
    * `superseded_by` is a foreign key, also checked per statement, so the old
      row cannot point at the new one until the new one exists.

    So: withdraw the old row onto itself, insert or revive the new one, then
    point the old row at its replacement. Each step is valid on its own, and
    the two-statement version that looks obviously correct is the one that
    raises.
    """
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

    if previous is not None:
        # Step 1 — nothing is current. Self-reference is this column's
        # "withdrawn"; it becomes "replaced by" in step 3.
        previous.superseded_by = previous.id
        await db.flush()

    if existing is not None:
        # Re-accepting a run whose acceptance had been withdrawn or superseded.
        acceptance = existing
        acceptance.superseded_by = None
        acceptance.accepted_by = me.user.id
        acceptance.accepted_at = _utcnow()
        acceptance.launch_readiness_at_acceptance = readiness
    else:
        acceptance = ResearchAcceptance(
            workspace_id=me.workspace_id,
            project_id=run.project_id,
            run_id=run.id,
            report_id=report.id,
            accepted_by=me.user.id,
            accepted_at=_utcnow(),
            launch_readiness_at_acceptance=readiness,
        )
        db.add(acceptance)
    acceptance.note = note
    acceptance.override_reason = override_reason
    await db.flush()  # step 2 — exactly one row is current again

    if previous is not None:
        previous.superseded_by = acceptance.id  # step 3 — say what replaced it
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
