"""Stage 03's entry path (PRD §4.4, §16).

There is no handshake here, and that absence is the module's whole shape.
`routes_plan` opens with two manual human acts that gate the stage; this one
opens with a Start button that is enabled from the moment a project exists.

What is carried over from `routes_plan` is the thing worth carrying: **
eligibility is computed in one place and read in two.** `GET .../eligibility`
renders it for a person, `POST .../runs` re-runs the same function before it
takes the lock. A UI that computed its own answer would eventually disagree
with the server, and the disagreement would show up as a button that does
nothing.

What is deliberately *not* carried over is the failure posture. A binding that
will not resolve costs this stage scope, not the run — so an unknown research
id returns 202 with a narrower mode rather than a 422. `build_guideline_input`
owns that decision; this module never inspects a binding itself.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, get_args

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from agent import queue
from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_guidelines import (
    AvailableBindings,
    EligibilityNote,
    GuidelineAttention,
    GuidelineDetail,
    GuidelineEligibility,
    GuidelineRunAccepted,
    GuidelineVersion,
    GuidelineVersionList,
    ImageLintFinding,
    ImageLintResult,
    ImageMetrics,
    LintRequest,
    LintResponse,
    OpenTaskRef,
    PlanBinding,
    PublishBlocker,
    PublishedRuleSet,
    PublishRequest,
    PublishResponse,
    ResearchBinding,
    StartGuidelineRequest,
)
from agent.api.schemas_report import ExportAccepted, ExportJob
from agent.api.throttle import throttle
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.ratelimit import EXPORT_QUOTA, RUN_QUOTA
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.credentials import MissingCredential, resolve_values
from agent.db.models import (
    AmendmentChangeKind,
    AmendmentStatus,
    CampaignPlan,
    CampaignPlanStatus,
    ClaimRecord,
    ClaimStatus,
    ContentGuideline,
    CredentialKind,
    EvidenceSource,
    ExportArtifactType,
    ExportFormat,
    GuidelineStatus,
    HumanTask,
    HumanTaskStatus,
    Membership,
    NodeRun,
    NodeRunStatus,
    PolicyAmendment,
    Report,
    ResearchAcceptance,
    RuleSet,
    RunStage,
    RunTrigger,
    SignOffMatrix,
    User,
    UserRole,
    UserStatus,
)
from agent.db.repos import ExportRepo, ProjectRepo
from agent.db.session import get_session
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.store import EvidenceScopeError, EvidenceStore
from agent.export.jobs import CONTENT_GUIDELINE_FORMATS, guideline_filename_for
from agent.guardrails.compiler import compiler_version, ruleset_hash
from agent.guardrails.linter import lint
from agent.guidelines import publish as publishing
from agent.guidelines import versions
from agent.guidelines.constants import get_content_constants
from agent.guidelines.projection import ProjectionNotFound, build_creative_context
from agent.orchestrator.guideline_input import build_guideline_input
from agent.orchestrator.launch import LaunchRequest, ProjectBusy, QueueUnavailable, launch
from agent.orchestrator.state import RunLock
from agent.queue import enqueue_export
from agent.redis_client import get_redis
from agent.schemas.creative_input import CreativeContext
from agent.schemas.guardrails import LintResult, LintTarget, LogoTemplate, Rule, Surface
from agent.schemas.guardrails import RuleSet as RuleSetContract
from agent.schemas.imaging import ImageMeasurement

log = structlog.get_logger(__name__)

router = APIRouter(tags=["guidelines"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
GuidelineOperator = Annotated[Principal, Depends(require(Permission.GUIDELINE_EXECUTE))]
#: §5.3. `GUIDELINE_PUBLISH` is held by `admin` and `approver` and by nobody
#: else — an operator may run the stage and may not seal it.
GuidelinePublisher = Annotated[Principal, Depends(require(Permission.GUIDELINE_PUBLISH))]


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/guidelines/eligibility",
    response_model=GuidelineEligibility,
    summary="Can content guidelines start on this project",
)
async def guideline_eligibility(
    project_id: uuid.UUID, me: AnyMember, db: Db
) -> GuidelineEligibility:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")
    return await _eligibility(db, me, project_id)


async def _eligibility(
    db: AsyncSession, me: Principal, project_id: uuid.UUID
) -> GuidelineEligibility:
    """PRD §4.4, C-E1…C-E8. Pure read: no writes, no locks taken.

    Every check runs even after one has already failed — a person looking at
    this screen wants the whole list of what to fix, not the first item of it.

    The split between the two lists is load-bearing and is the reason this
    function returns a typed object rather than a dict: `eligible` is computed
    from `blockers` alone, so a warning cannot stop a run however it is
    rendered.
    """
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    assert project is not None  # noqa: S101 — the caller looked it up first
    blockers: list[EligibilityNote] = []
    warnings: list[EligibilityNote] = []
    home = f"/projects/{project_id}"

    # C-E1 — something to read. A domain is enough; so is a product context, a
    # brand-book upload or a connected ad account. This is a low bar on
    # purpose: the stage's job is to widen scope when an input is missing, not
    # to refuse.
    if not (project.domain or project.product_context):
        blockers.append(
            EligibilityNote(
                code="no_content_source",
                detail=(
                    "This project has no website, no product context and no uploaded brand "
                    "material, so there is nothing to derive a rulebook from yet."
                ),
                fix_url=home,
            )
        )

    # C-E2 — one guideline run per project.
    holder = await RunLock(get_redis(), RunStage.GUIDELINE).holder(project_id)
    if holder is not None:
        blockers.append(
            EligibilityNote(
                code="guideline_in_flight",
                detail=(
                    f"{holder.user_name or 'Someone'} is already building content guidelines "
                    f"for this project (run {str(holder.run_id)[:8]})."
                ),
                # The Guideline Console, which S3-P7 built. S3-P0 pointed
                # this at the Stage 01 route because a fix_url that 404s is
                # worse than no link; that reason has expired.
                fix_url=f"{home}/guidelines/runs/{holder.run_id}",
            )
        )

    # C-E3 — a model key, resolved the same way every other stage resolves one.
    try:
        await resolve_values(db, workspace_id=me.workspace_id, kind=CredentialKind.OPENROUTER)
    except MissingCredential:
        blockers.append(
            EligibilityNote(
                code="missing_credential",
                detail=(
                    "No OpenRouter credential resolves for this workspace, so a guideline "
                    "run has no model to call."
                ),
                fix_url="/settings/sources",
            )
        )

    # C-E5 — somebody who could hold a non-delegable signature. The one
    # precondition of this stage that is a person rather than an upstream
    # artifact, which is why it does not offend law 21: an admin fixes it by
    # inviting an approver, not by running another stage first.
    if not await _can_name_owners(db, me.workspace_id, project_id):
        blockers.append(
            EligibilityNote(
                code="no_eligible_owners",
                detail=(
                    "Nobody in this workspace holds the approver role, so there is no one "
                    "to name as the legal owner and no one who could sign the claims "
                    "register. Invite an approver first — the system will not fabricate a "
                    "signer."
                ),
                fix_url="/settings/users",
            )
        )

    bindings = await _available_bindings(db, me.workspace_id, project_id)

    # C-E6 — the load-bearing line. A warning, and never anything else.
    if bindings.research is None and bindings.plan is None:
        warnings.append(
            EligibilityNote(
                code="running_unlinked",
                detail=(
                    "No accepted research and no frozen plan exist for this project, so the "
                    "run will cover every Google campaign type and every market on the "
                    "project rather than only the ones you are launching. That is a wider "
                    "rulebook, not a worse one."
                ),
                fix_url=f"{home}/guidelines",
            )
        )

    # C-E7 — a published version is a reason to say what a run produces, never
    # a reason to stop it. The current version stays published and serving.
    current = await _current_major(db, me.workspace_id, project_id)
    if current is not None:
        warnings.append(
            EligibilityNote(
                code="will_mint_major",
                detail=(
                    f"v{current}.x is published and will keep serving. A new run produces "
                    f"v{current + 1}.0, which only takes effect when somebody publishes it."
                ),
                fix_url=f"{home}/guidelines/published",
            )
        )

    # C-E8 — unreviewed amendments.
    open_amendments = await _open_amendments(db, me.workspace_id, project_id)
    if open_amendments:
        warnings.append(
            EligibilityNote(
                code="unreviewed_amendments",
                detail=(
                    f"{open_amendments} policy amendment(s) on this project are waiting for "
                    "review. A new run does not apply them."
                ),
                fix_url=f"{home}/guidelines/amendments",
            )
        )

    return GuidelineEligibility(
        eligible=not blockers,
        blockers=blockers,
        warnings=warnings,
        available_bindings=bindings,
    )


# ---------------------------------------------------------------------------
# starting a run
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/guidelines/runs",
    response_model=GuidelineRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a content guidelines run",
    # Same reasoning as the other two stages: each costs dollars and holds a
    # worker. The guideline lock allows one per project; this bounds a caller
    # cycling across projects.
    dependencies=[Depends(throttle(RUN_QUOTA))],
)
async def start_guideline_run(
    project_id: uuid.UUID,
    me: GuidelineOperator,
    request: Request,
    body: StartGuidelineRequest,
    db: Db,
) -> GuidelineRunAccepted:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")

    # Re-checked server-side. The Start button reads the same function, but a
    # button is a cache of an answer and this is the answer.
    eligibility = await _eligibility(db, me, project_id)
    if not eligibility.eligible:
        first = eligibility.blockers[0]
        raise problems.conflict(
            first.detail,
            title="Content guidelines cannot start yet",
            code=first.code,
            blockers=[item.model_dump() for item in eligibility.blockers],
        )

    # Before the lock and before the row. Unlike S2-P0 this cannot fail — an
    # unresolvable binding is dropped into `unbound_inputs` — so there is no
    # 422 branch here, and adding one would be the handshake creeping back.
    built, input_hash = await build_guideline_input(
        db, project_id, body.bindings, workspace_id=me.workspace_id
    )

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
                stage=RunStage.GUIDELINE,
                # Set only when a research binding actually resolved. A
                # standalone run leaves it NULL, which the rewritten
                # `ck_run_plan_has_source` permits and the original did not.
                source_run_id=built.bindings.research_run_id,
                input_hash=input_hash,
                reuse_cache=body.reuse_cache,
                bindings=_json_bindings(built),
                audit_meta={"mode": built.mode.value},
            ),
        )
    except ProjectBusy as busy:
        raise problems.conflict(
            f"{busy.holder.user_name or 'Someone'} is already building guidelines "
            "for this project.",
            title="A guideline run is already in flight",
            code="guideline_in_flight",
            holder=busy.holder.as_dict(),
        ) from busy
    except QueueUnavailable as unavailable:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Queue unavailable",
            detail="The guideline run was recorded but could not be queued. "
            "Retry it once Redis is back.",
        ) from unavailable

    # The artifact exists from the moment the run does. It has to: 3.2.3
    # registers claims a dozen nodes before 3.6.1 synthesises anything, and
    # `ClaimRecord.first_seen_guideline_id` is NOT NULL. `payload` and
    # `markdown` are nullable for exactly this window — see
    # `guidelines/versions.ensure_draft`.
    #
    # **Not atomic with the run, and it cannot be.** `launch` commits the `Run`
    # and only then enqueues (a job that starts against an uncommitted run
    # cannot find it), so by the time this line executes the run is already
    # queued. The window is milliseconds and the worker is several nodes away
    # from needing the row — and `ensure_draft` is idempotent precisely so the
    # node that does need it can create it if this never ran. What this buys is
    # not correctness but reach: the Rulebook Viewer, the claims register and
    # the linter playground can address the guideline from the first second of
    # the run rather than only after the last node lands.
    await versions.ensure_draft(
        db,
        workspace_id=me.workspace_id,
        project_id=project_id,
        run_id=run.id,
        mode=built.mode,
        bindings=_json_bindings(built),
        unbound_inputs=list(built.unbound_inputs),
    )
    await db.commit()

    return GuidelineRunAccepted(
        run_id=run.id,
        status=run.status,
        mode=built.mode,
        bindings=built.bindings,
        unbound_inputs=built.unbound_inputs,
        input_hash=input_hash,
    )


def _json_bindings(built: object) -> dict[str, str | int | None]:
    """`GuidelineBindings` as the JSONB column stores it.

    Always an object, never NULL and never the JSON scalar `null`: the CHECK
    requires `jsonb_typeof(bindings) = 'object'`, and a standalone run's `{}`
    is a positive statement that nothing bound.
    """
    bindings = built.bindings  # type: ignore[attr-defined]
    return {
        key: (str(value) if isinstance(value, uuid.UUID) else value)
        for key, value in bindings.model_dump().items()
        if value is not None
    }


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/guidelines",
    response_model=GuidelineVersionList,
    summary="Every guideline version on this project, newest first",
)
async def guideline_versions(project_id: uuid.UUID, me: AnyMember, db: Db) -> GuidelineVersionList:
    if await ProjectRepo(db, me.workspace_id).get(project_id) is None:
        raise problems.not_found(f"No project {project_id}.")

    # Counted in the database. `payload` is the whole rulebook and this list
    # renders every version of it, so selecting the column to call `len()` on
    # two of its keys would move megabytes to count tens.
    #
    # `jsonb_array_length` raises on a non-array, so each count is guarded by
    # the type check as well as the NULL check: a draft written before 3.6.1
    # has no payload at all, and one halted mid-synthesis can have the key
    # without the list.
    def _count(*path: str) -> sa.ColumnElement[int]:
        member: Any = ContentGuideline.payload
        for step in path:
            member = member[step]
        return sa.case(
            (
                sa.and_(
                    ContentGuideline.payload.is_not(None),
                    sa.func.jsonb_typeof(member) == "array",
                ),
                sa.func.jsonb_array_length(member),
            ),
            else_=0,
        )

    publisher = aliased(User)
    rows = (
        await db.execute(
            sa.select(
                ContentGuideline,
                sa.func.coalesce(publisher.name, "").label("published_by_name"),
                _count("rules").label("rule_count"),
                _count("claims_register", "claims").label("claim_count"),
            )
            .outerjoin(publisher, publisher.id == ContentGuideline.published_by)
            .where(
                ContentGuideline.workspace_id == me.workspace_id,
                ContentGuideline.project_id == project_id,
            )
            # `version DESC` alone stops being an order the moment a project
            # holds two drafts: every unpublished version is 0.0. The
            # `created_at` tiebreak is what keeps the newest draft on top.
            .order_by(
                ContentGuideline.version_major.desc(),
                ContentGuideline.version_minor.desc(),
                ContentGuideline.created_at.desc(),
            )
        )
    ).all()
    return GuidelineVersionList(
        versions=[
            GuidelineVersion.model_validate(row.ContentGuideline, from_attributes=True).model_copy(
                update={
                    "published_by_name": row.published_by_name,
                    "rule_count": row.rule_count,
                    "claim_count": row.claim_count,
                }
            )
            for row in rows
        ]
    )


@router.get(
    "/projects/{project_id}/guidelines/attention",
    response_model=GuidelineAttention,
    summary="What on this project is waiting for a person",
)
async def guideline_attention(project_id: uuid.UUID, me: AnyMember, db: Db) -> GuidelineAttention:
    """§15.1 rule 3 and §15.3 A block 3, in one request.

    Two surfaces read this and neither wants a list: the stage rail wants two
    dots, the landing wants a handful of rows. So it returns counts plus the
    first few tasks, and `open_tasks_total` stays the truth when the list is
    shorter than the count.

    It reads three tables that have no HTTP route of their own yet — S3-P8 owns
    the person-task card, the claims register and the amendment inbox. That is
    the reason this is a *summary* and not `GET /human-tasks`: shipping the
    list endpoints here would build S3-P8's data layer a phase early, and §22
    rules that out. Answering "is anything waiting on a human" is this phase's
    own question, because this phase's rail asks it.

    `READ` for everybody, `viewer` included: it is a count of work, not the
    work, and it carries no instructions, no artifacts and no claim text.
    """
    if await ProjectRepo(db, me.workspace_id).get(project_id) is None:
        raise problems.not_found(f"No project {project_id}.")

    now = datetime.now(UTC)
    window_days = 30
    horizon = now + timedelta(days=window_days)

    # --- person-tasks -----------------------------------------------------
    #
    # `not_required` is a finding, not an omission (see `HumanTaskStatus`), so
    # it is not open. `expired` is not open either — it has stopped waiting for
    # anyone and re-opening it is the lifecycle's job, not a badge's.
    open_states = (HumanTaskStatus.PENDING, HumanTaskStatus.IN_PROGRESS, HumanTaskStatus.BLOCKED)
    task_rows = (
        await db.execute(
            sa.select(HumanTask, User.name)
            .join(User, User.id == HumanTask.assignee_id)
            .where(
                HumanTask.workspace_id == me.workspace_id,
                HumanTask.project_id == project_id,
                HumanTask.status.in_(open_states),
            )
            # Soonest due first, and undated tasks after dated ones rather than
            # before them: `NULLS LAST` because "no deadline" is not "overdue".
            .order_by(HumanTask.due_at.asc().nullslast(), HumanTask.created_at.asc())
            .limit(10)
        )
    ).all()
    open_tasks_total = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(HumanTask)
            .where(
                HumanTask.workspace_id == me.workspace_id,
                HumanTask.project_id == project_id,
                HumanTask.status.in_(open_states),
            )
        )
    ).scalar_one()
    my_open_tasks = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(HumanTask)
            .where(
                HumanTask.workspace_id == me.workspace_id,
                HumanTask.project_id == project_id,
                HumanTask.status.in_(open_states),
                HumanTask.assignee_id == me.user.id,
            )
        )
    ).scalar_one()

    # --- claims about to lapse -------------------------------------------
    #
    # Only `approved` claims can expire into anything: an unsupported or
    # rejected claim licenses nothing today, so its expiry date changes
    # nothing. `ix_claim_record_project_status_expiry` is this query.
    expiring = sa.select(ClaimRecord).where(
        ClaimRecord.workspace_id == me.workspace_id,
        ClaimRecord.project_id == project_id,
        ClaimRecord.superseded_by.is_(None),
        ClaimRecord.status == ClaimStatus.APPROVED,
        ClaimRecord.expires_at.is_not(None),
        ClaimRecord.expires_at <= horizon,
        ClaimRecord.expires_at > now,
    )
    expiring_claims = (
        await db.execute(sa.select(sa.func.count()).select_from(expiring.subquery()))
    ).scalar_one()
    earliest_expiry = (
        await db.execute(
            sa.select(sa.func.min(ClaimRecord.expires_at)).where(
                ClaimRecord.workspace_id == me.workspace_id,
                ClaimRecord.project_id == project_id,
                ClaimRecord.superseded_by.is_(None),
                ClaimRecord.status == ClaimStatus.APPROVED,
                ClaimRecord.expires_at.is_not(None),
                ClaimRecord.expires_at > now,
            )
        )
    ).scalar_one_or_none()

    # --- amendments nobody has ruled on -----------------------------------
    #
    # `auto_applied` is reviewed by construction: a mechanical change applied
    # itself and minted its MINOR, and law 29 says that is the correct end of
    # its life. Counting it here would put an amber dot on every project that
    # is working exactly as designed.
    unreviewed_states = (AmendmentStatus.OPEN, AmendmentStatus.NEEDS_REVIEW)
    unreviewed_amendments = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(PolicyAmendment)
            .where(
                PolicyAmendment.workspace_id == me.workspace_id,
                PolicyAmendment.project_id == project_id,
                PolicyAmendment.status.in_(unreviewed_states),
            )
        )
    ).scalar_one()
    signature_affecting = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(PolicyAmendment)
            .where(
                PolicyAmendment.workspace_id == me.workspace_id,
                PolicyAmendment.project_id == project_id,
                PolicyAmendment.status.in_(unreviewed_states),
                PolicyAmendment.change_kind == AmendmentChangeKind.SIGNATURE_AFFECTING,
            )
        )
    ).scalar_one()

    # --- the published version's signature --------------------------------
    signature_stale = bool(
        (
            await db.execute(
                sa.select(ContentGuideline.signature_stale).where(
                    ContentGuideline.workspace_id == me.workspace_id,
                    ContentGuideline.project_id == project_id,
                    ContentGuideline.status == GuidelineStatus.PUBLISHED,
                )
            )
        ).scalar_one_or_none()
    )

    return GuidelineAttention(
        open_tasks=[
            OpenTaskRef(
                task_id=task.id,
                task_key=task.task_key,
                title=task.title,
                status=str(task.status),
                blocking_for=str(task.blocking_for),
                assignee_id=task.assignee_id,
                assignee_name=name or "",
                mine=task.assignee_id == me.user.id,
                due_at=task.due_at,
            )
            for task, name in task_rows
        ],
        open_tasks_total=open_tasks_total,
        my_open_tasks=my_open_tasks,
        expiring_claims=expiring_claims,
        earliest_expiry=earliest_expiry,
        expiry_window_days=window_days,
        unreviewed_amendments=unreviewed_amendments,
        signature_affecting_amendments=signature_affecting,
        signature_stale=signature_stale,
    )


@router.get(
    "/guidelines/{guideline_id}",
    response_model=GuidelineDetail,
    summary="One guideline version",
)
async def guideline_detail(guideline_id: uuid.UUID, me: AnyMember, db: Db) -> GuidelineDetail:
    row = (
        await db.execute(
            sa.select(ContentGuideline).where(
                ContentGuideline.id == guideline_id,
                ContentGuideline.workspace_id == me.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise problems.not_found(f"No guideline {guideline_id}.")
    return GuidelineDetail.model_validate(row, from_attributes=True)


# ---------------------------------------------------------------------------
# the individual checks
# ---------------------------------------------------------------------------


async def _can_name_owners(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> bool:
    """C-E5: a current sign-off matrix, or somebody who could populate one."""
    existing = (
        await db.execute(
            sa.select(SignOffMatrix.id).where(
                SignOffMatrix.workspace_id == workspace_id,
                SignOffMatrix.project_id == project_id,
                SignOffMatrix.superseded_at.is_(None),
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return True
    approvers = (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(Membership)
            .where(
                Membership.workspace_id == workspace_id,
                Membership.role == UserRole.APPROVER,
                # `invited` counts: an approver who has not accepted yet is
                # still somebody an admin can name, and H1 is nowhere near this
                # point in the run. `disabled` does not — naming a disabled
                # account as the legal owner produces a signature nobody can
                # give.
                Membership.status != UserStatus.DISABLED,
            )
        )
    ).scalar_one()
    return bool(approvers)


async def _available_bindings(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> AvailableBindings:
    """What the start dialog may offer, each side resolved independently."""
    research = None
    row = (
        await db.execute(
            sa.select(ResearchAcceptance, Report)
            .join(Report, Report.id == ResearchAcceptance.report_id)
            .where(
                ResearchAcceptance.workspace_id == workspace_id,
                ResearchAcceptance.project_id == project_id,
                ResearchAcceptance.superseded_by.is_(None),
            )
        )
    ).first()
    if row is not None:
        acceptance, report = row
        research = ResearchBinding(
            acceptance_id=acceptance.id,
            research_run_id=acceptance.run_id,
            research_schema_version=report.schema_version,
            accepted_at=acceptance.accepted_at,
            adds=(
                "Legal guardrails and regulated terms, the differentiation claim, the "
                "competitor creative corpus and the markets research covered."
            ),
        )

    plan_row = (
        await db.execute(
            sa.select(CampaignPlan)
            .where(
                CampaignPlan.workspace_id == workspace_id,
                CampaignPlan.project_id == project_id,
                CampaignPlan.status == CampaignPlanStatus.FROZEN,
            )
            .order_by(CampaignPlan.version.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    plan = (
        PlanBinding(
            plan_id=plan_row.id,
            plan_version=plan_row.version,
            plan_schema_version=plan_row.schema_version,
            frozen_at=plan_row.frozen_at,
            adds=(
                "Narrows the asset spec sheet to the campaign types you are actually "
                "launching, and lints each ad group's primary message at guideline time."
            ),
        )
        if plan_row is not None
        else None
    )
    return AvailableBindings(research=research, plan=plan)


async def _current_major(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> int | None:
    return (
        await db.execute(
            sa.select(sa.func.max(ContentGuideline.version_major)).where(
                ContentGuideline.workspace_id == workspace_id,
                ContentGuideline.project_id == project_id,
                ContentGuideline.status == GuidelineStatus.PUBLISHED,
            )
        )
    ).scalar_one_or_none()


async def _open_amendments(db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID) -> int:
    return (
        await db.execute(
            sa.select(sa.func.count())
            .select_from(PolicyAmendment)
            .where(
                PolicyAmendment.workspace_id == workspace_id,
                PolicyAmendment.project_id == project_id,
                PolicyAmendment.status == AmendmentStatus.NEEDS_REVIEW,
            )
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# the image precheck — PRD §16, §9.5
# ---------------------------------------------------------------------------

#: What Google accepts as an image asset, and therefore the only thing worth
#: prechecking. Anything else is refused here rather than handed to a decoder.
ImageVerdict = Literal["pass", "pass_with_warnings", "fail", "indeterminate"]

IMAGE_MEDIA_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp", "image/gif"}

#: The `Surface` literal's members, read from the contract rather than retyped.
#: A copy here would drift the first time §12.2 gains a surface, and the drift
#: would show up as a 422 on a surface the linter happily scopes rules to.
SURFACES: frozenset[str] = frozenset(get_args(Surface))


@router.post(
    "/guidelines/{guideline_id}/lint",
    response_model=LintResponse,
    summary="Check copy against this guideline's rules",
)
async def lint_text(
    guideline_id: uuid.UUID, body: LintRequest, me: AnyMember, db: Db
) -> LintResponse:
    """Evaluate copy against the compiled ruleset. No writes, no model calls.

    `READ`, and deliberately the weakest permission in the stage. A `viewer`
    who can read the rulebook must be able to ask it a question — §15.3 F calls
    this the single best adoption lever in Stage 03, and putting it behind
    `guideline_execute` would hand it to exactly the people who least need it.

    **The evaluation happens here and only here.** §15.4 rule 2 calls a matcher
    reimplemented in the frontend a bug rather than an optimisation, because two
    implementations are two sets of verdicts and a writer whose copy passes in
    the browser and fails in the pipeline has been told two different things by
    the same system.
    """
    guideline = (
        await db.execute(
            sa.select(ContentGuideline).where(
                ContentGuideline.id == guideline_id,
                ContentGuideline.workspace_id == me.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if guideline is None:
        raise problems.not_found(f"No guideline {guideline_id}.")

    rules, ruleset_version = await _text_ruleset(db, guideline)
    constants = get_content_constants()
    ruleset = RuleSetContract(
        ruleset_version=ruleset_version,
        project_id=guideline.project_id,
        guideline_id=guideline.id,
        compiler_version=compiler_version(),
        constants_version=constants.version,
        rules=rules,
        hash=_transient_hash(rules),
        compiled_at=datetime.now(UTC),
    )
    result = lint(body.targets, ruleset, now=datetime.now(UTC))
    return LintResponse(guideline_id=guideline.id, result=result)


async def _text_ruleset(db: AsyncSession, guideline: ContentGuideline) -> tuple[list[Rule], str]:
    """This guideline's copy rules, published or draft.

    **Image rules are excluded, and that is not an oversight.** An image rule
    evaluated against a target carrying no `image_metrics` is `indeterminate`,
    and law 31 makes a blocking indeterminate finding *fail*. Including them
    would make every text lint return `fail` with a finding about a picture
    nobody submitted — the linter working exactly as specified, producing an
    answer that is useless. `POST /lint/image` is where images are checked, and
    it applies the mirror-image filter for the mirror-image reason.
    """
    if guideline.ruleset_id is not None:
        row = (
            await db.execute(sa.select(RuleSet).where(RuleSet.id == guideline.ruleset_id))
        ).scalar_one_or_none()
        if row is not None:
            compiled = RuleSetContract.model_validate(row.compiled)
            return (
                [rule for rule in compiled.rules if rule.category != "image"],
                compiled.ruleset_version,
            )

    payload = guideline.payload or {}
    raw_rules = payload.get("rules")
    if raw_rules is None:
        # Mid-run, before 3.6.1 has synthesised a payload. Every node that has
        # emitted rules so far is the best available answer, and assembling it
        # is what makes the playground usable before publish rather than after.
        raw_rules = await _draft_rules(db, guideline)
    if not raw_rules:
        raise problems.conflict(
            "This guideline has no rules yet. Nothing has compiled a rule for this run, "
            "so there is nothing to check copy against.",
            title="Rules not ready",
        )

    try:
        rules = [Rule.model_validate(item) for item in raw_rules]
    except ValidationError as exc:
        # A stored payload the current `Rule` model cannot read. Reachable in
        # practice: `guideline.payload` is written by 3.6.1 and outlives the
        # compiler version that wrote it, so a contract change makes every old
        # draft unparseable. Letting pydantic escape here turns that into a 500
        # with a stack trace, which tells a writer checking a headline nothing
        # and tells an operator the wrong thing — the server is fine, the
        # stored rules are stale.
        raise problems.conflict(
            f"This guideline's stored rules were written by an older compiler and cannot "
            f"be read by this one ({exc.error_count()} field(s) disagree). Re-run the "
            "guideline stage to recompile them.",
            title="Rules cannot be read",
        ) from exc
    return (
        [rule for rule in rules if rule.category != "image"],
        f"draft+{guideline.version_major}.{guideline.version_minor}",
    )


async def _draft_rules(db: AsyncSession, guideline: ContentGuideline) -> list[dict[str, Any]]:
    """Every rule the succeeded nodes of this run have emitted so far.

    Collected in node order and de-duplicated on `rule_id`, last writer winning
    — a node that re-ran after a retry should not have its superseded rule
    counted beside its replacement.
    """
    rows = (
        (
            await db.execute(
                sa.select(NodeRun)
                .where(
                    NodeRun.run_id == guideline.guideline_run_id,
                    NodeRun.status == NodeRunStatus.SUCCEEDED,
                )
                .order_by(NodeRun.node_id, NodeRun.started_at)
            )
        )
        .scalars()
        .all()
    )
    collected: dict[str, dict[str, Any]] = {}
    for row in rows:
        for item in (row.output or {}).get("rules") or []:
            rule_id = item.get("rule_id")
            if rule_id:
                collected[rule_id] = item
    return list(collected.values())


@router.post(
    "/guidelines/{guideline_id}/lint/image",
    response_model=ImageLintResult,
    summary="Check one image against this guideline's image rules",
)
async def lint_image(
    guideline_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    file: Annotated[UploadFile, File(description="PNG, JPEG, WebP or GIF")],
    surface: Annotated[str, Form()] = "display_text",
    campaign_type: Annotated[str, Form()] = "search",
    market: Annotated[str, Form()] = "",
    language: Annotated[str, Form()] = "en",
) -> ImageLintResult:
    """Measure an image in `worker`, adjudicate it in `guardrails`, return both.

    Three constraints meet on this route and only one shape satisfies all of
    them: §16 says it answers with metrics and findings in one response, §9.5
    says the measurement happens in `worker` one image at a time, and §6 says
    `api` never grows a `tesseract` dependency. So `api` takes the bytes,
    enqueues the measurement, waits for the number, and evaluates the rule
    itself — the rule being pure, which is the entire reason `guardrails/` is
    allowed nowhere near a native binary.

    `READ` permission, and that is not an oversight: this writes no guideline,
    decides nothing, and a `viewer` who can see the rulebook should be able to
    check an image against it before asking anybody for anything. The one thing
    it does write is the §7.3 `derived` evidence row, which is the record of a
    measurement rather than a change to the rulebook.
    """
    guideline = (
        await db.execute(
            sa.select(ContentGuideline).where(
                ContentGuideline.id == guideline_id,
                ContentGuideline.workspace_id == me.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if guideline is None:
        raise problems.not_found(f"No guideline {guideline_id}.")

    content = await file.read()
    if not content:
        raise problems.unprocessable("That file is empty.")
    settings = get_settings()
    if len(content) > settings.image_lint_max_bytes:
        raise problems.unprocessable(
            f"That image is {len(content) // 1024} KB; the limit is "
            f"{settings.image_lint_max_bytes // 1024} KB.",
            title="File too large",
        )
    media_type = (file.content_type or "").split(";")[0].strip().lower()
    if media_type and media_type not in IMAGE_MEDIA_TYPES:
        raise problems.unprocessable(
            f"{media_type} is not an image format Google accepts as an asset.",
            title="Unsupported format",
        )

    if surface not in SURFACES:
        # Without this the `Surface` literal raises inside `LintTarget` and the
        # caller gets a 500 for a typo. The valid values are listed back because
        # this is a form field, not a dropdown the UI necessarily constrains.
        raise problems.unprocessable(
            f"{surface!r} is not an ad surface. Valid values: {', '.join(sorted(SURFACES))}.",
            title="Unknown surface",
        )

    rules, templates, ruleset_version = await _image_ruleset(db, guideline)
    constants = get_content_constants()

    measured = await queue.measure_image(
        {
            "content": content,
            "templates": [
                {
                    "asset_id": str(t.asset_id),
                    "label": t.label,
                    "phash": t.phash,
                    "descriptors_b64": t.descriptors_b64,
                    "keypoint_count": t.keypoint_count,
                    "min_score": t.min_score,
                }
                for t in templates
            ],
            "working_width": constants.image_policy.ocr_working_width_px.as_int(),
        }
    )
    measurement = _measurement_from(measured, content=content, media_type=media_type)

    ruleset = RuleSetContract(
        ruleset_version=ruleset_version,
        project_id=guideline.project_id,
        guideline_id=guideline.id,
        compiler_version=compiler_version(),
        constants_version=constants.version,
        compiled_at=datetime.now(UTC),
        rules=tuple(rules),
        logo_templates=tuple(templates),
        hash=_transient_hash(rules),
    )
    target = LintTarget(
        ref=file.filename or "image",
        surface=surface,
        campaign_type=campaign_type,
        market=market or "*",
        language=language or "en",
        image_ref=measurement.image_hash,
        image_metrics=measurement.metrics(),
    )
    result = lint([target], ruleset, now=datetime.now(UTC))

    evidence_id = await _record_measurement(db, me, guideline, measurement)
    await db.commit()

    verdict, unchecked = image_verdict(result)
    return ImageLintResult(
        guideline_id=guideline.id,
        ruleset_version=ruleset.ruleset_version,
        verdict=verdict,
        reason=measurement.reason if unchecked else None,
        findings=[
            ImageLintFinding(
                rule_id=f.rule_id,
                severity=f.severity,
                message=f.message,
                fix_hint=f.fix_hint,
                authority_ref=f.authority_ref,
                indeterminate=f.indeterminate,
            )
            for f in result.findings
        ],
        metrics=ImageMetrics.model_validate(measurement.model_dump(mode="json")),
        rules_evaluated=result.rules_evaluated,
        evidence_id=evidence_id,
        evaluated_at=result.evaluated_at,
    )


def _transient_hash(rules: list[Rule]) -> str:
    """A hash over the rules this check actually used.

    Not a `RuleSet` row and never written as one — `compiler.py` is the only
    writer of those (§9.1 rule 3). This exists so the response can name what it
    evaluated against when the guideline is still a draft and no ruleset has
    been minted, which is every guideline until S3-P6's publish.
    """
    return ruleset_hash({"rules": [rule.model_dump(mode="json") for rule in rules]})[:8]


async def _image_ruleset(
    db: AsyncSession, guideline: ContentGuideline
) -> tuple[list[Rule], list[LogoTemplate], str]:
    """This guideline's image rules and logo templates, published or draft.

    Filtered to the `image` category deliberately. A published ruleset also
    holds length and count rules, and a `count` rule is evaluated over the
    whole target set — so linting a single image against the full set would
    report "fewer than three headlines" about a picture. §16 calls this route
    "multipart -> image metrics + findings"; the image rules are the findings
    it means.
    """
    if guideline.ruleset_id is not None:
        row = (
            await db.execute(sa.select(RuleSet).where(RuleSet.id == guideline.ruleset_id))
        ).scalar_one_or_none()
        if row is not None:
            compiled = RuleSetContract.model_validate(row.compiled)
            return (
                [rule for rule in compiled.rules if rule.category == "image"],
                list(compiled.logo_templates),
                compiled.ruleset_version,
            )

    payload = guideline.payload or {}
    raw_rules = payload.get("rules")
    raw_logos = payload.get("logo_templates")
    if raw_rules is None:
        # No published ruleset and no synthesised payload: the guideline is
        # mid-run. 3.4.3's own output is the authoritative source at that point,
        # and reading it is what makes the playground usable before publish.
        node = (
            (
                await db.execute(
                    sa.select(NodeRun).where(
                        NodeRun.run_id == guideline.guideline_run_id,
                        NodeRun.node_id == "3.4.3",
                        NodeRun.status == NodeRunStatus.SUCCEEDED,
                    )
                )
            )
            .scalars()
            .first()
        )
        if node is None or not node.output:
            raise problems.conflict(
                "This guideline has no image rules yet. Node 3.4.3 has not completed "
                "for this run, so there is nothing to check an image against.",
                title="Image rules not ready",
            )
        raw_rules = node.output.get("rules") or []
        raw_logos = node.output.get("logo_templates") or []

    rules = [Rule.model_validate(item) for item in raw_rules]
    logos = [LogoTemplate.model_validate(item) for item in (raw_logos or [])]
    return (
        [rule for rule in rules if rule.category == "image"],
        logos,
        f"draft+{guideline.version_major}.{guideline.version_minor}",
    )


def _measurement_from(
    measured: dict[str, Any], *, content: bytes, media_type: str
) -> ImageMeasurement:
    """Validate the worker's answer, or fail closed.

    The degraded dict `queue.measure_image` returns on an unreachable worker
    carries only a status and a reason, so the fields a full measurement would
    have are filled in here from what `api` already knows. It must still be an
    `ImageMeasurement` with every metric `None` — that is what makes the rules
    report `indeterminate` rather than the route inventing a verdict of its own.
    """
    if measured.get("status") == "measured":
        try:
            return ImageMeasurement.model_validate(measured)
        except ValidationError as exc:
            log.warning("imaging.measurement_invalid", error=str(exc))
    return ImageMeasurement(
        image_hash=hashlib.sha256(content).hexdigest(),
        width_px=1,
        height_px=1,
        byte_size=len(content),
        media_type=media_type or "image/unknown",
        status="detector_unavailable",
        reason=str(measured.get("reason") or "detector_unavailable"),
        detector_version="unavailable",
        working_width_px=1,
        measured_ms=0,
    )


def image_verdict(result: LintResult) -> tuple[ImageVerdict, bool]:
    """The image verdict, and whether anything went unchecked.

    Law 31, spelled out rather than inherited. `LintResult.verdict` has only
    `pass`, `pass_with_warnings` and `fail`; an `indeterminate` finding is
    *neither* blocking nor warning, so it lands in `pass` — and a pass is
    exactly what §18 forbids when a detector could not run. A green tick that
    means "we could not check this" is worse than a red one, because nobody
    looks at it again.

    Public and separately tested because it is the one line in this route where
    getting it wrong is silent: every other mistake here surfaces as an error,
    and this one surfaces as an approval.

    **A measured failure outranks an unmeasured check**, and that ordering was
    wrong in the first draft of this function. Law 31 requires that
    `indeterminate` never become `pass`; it says nothing about `fail`, and
    between the two `fail` is both truthful and more useful. An image whose
    coverage was measured at 31% against a 20% ceiling has definitely failed,
    whatever else went unchecked — reporting `indeterminate` there would demote
    a fact to a maybe and invite somebody to retry rather than fix it. The
    `unchecked` flag still travels, so the response can say what was skipped
    even when the verdict is `fail`.
    """
    unchecked = any(finding.indeterminate for finding in result.findings)
    if any(f.severity == "blocking" and not f.indeterminate for f in result.findings):
        return "fail", unchecked
    if unchecked:
        return "indeterminate", True
    return result.verdict, False


def _summary(measurement: ImageMeasurement) -> str:
    """The one-line human rendering §7.3 wants beside the numbers."""
    if measurement.text_coverage_ratio is None:
        return "not measured"
    return f"{measurement.text_coverage_ratio:.1%} text"


async def _record_measurement(
    db: AsyncSession,
    me: Principal,
    guideline: ContentGuideline,
    measurement: ImageMeasurement,
) -> uuid.UUID | None:
    """Write §7.3's `derived` / `image_metric` row.

    This is the architectural answer to a measurement that is not guaranteed
    bit-identical across CPU architectures: the number is persisted once, and a
    later re-check reads the stored value rather than re-measuring. A verdict
    issued today therefore still means the same thing next year, on whatever
    hardware happens to be running then.
    """
    store = EvidenceStore(db, me.workspace_id)
    try:
        written = await store.write(
            [
                EvidenceDraft(
                    source=EvidenceSource.DERIVED,
                    kind="image_metric",
                    payload=measurement.evidence_payload(),
                    content_text=f"image {measurement.image_hash[:12]}: {_summary(measurement)}",
                )
            ],
            project_id=guideline.project_id,
        )
    except EvidenceScopeError as exc:  # pragma: no cover - the guideline scopes the project
        log.warning("imaging.evidence_scope", error=str(exc))
        return None
    return written.evidence_ids[0] if written.evidence_ids else None


# ---------------------------------------------------------------------------
# publish and the Stage 04 contract — PRD §12.4, §16
# ---------------------------------------------------------------------------


@router.post(
    "/guidelines/{guideline_id}/publish",
    response_model=PublishResponse,
    summary="Publish a rulebook and mint its immutable ruleset",
)
async def publish(
    guideline_id: uuid.UUID,
    body: PublishRequest,
    me: GuidelinePublisher,
    request: Request,
    db: Db,
) -> PublishResponse:
    """§12.4. One transaction: assert, compile, mint, supersede, audit.

    A refusal is a `409` carrying **every** blocker, not the first one. §21 asks
    for that explicitly and the reason is arithmetic: a dialog that reports one
    blocker at a time turns a five-minute fix into five round trips through a
    legal owner's inbox.
    """
    try:
        result = await publishing.publish_guideline(
            db,
            workspace_id=me.workspace_id,
            guideline_id=guideline_id,
            confirm_version=body.confirm_version,
            actor_id=me.user.id,
            ip=client_ip(request),
        )
    except publishing.PublishRefused as refused:
        first = refused.blockers[0]
        raise problems.conflict(
            first.detail,
            title="This rulebook cannot be published yet",
            code=first.code,
            blockers=[
                PublishBlocker(
                    code=item.code, detail=item.detail, fix_url=item.fix_url
                ).model_dump()
                for item in refused.blockers
            ],
        ) from refused
    except publishing.PublishConflict as conflict:
        raise problems.conflict(
            str(conflict),
            title="Version conflict",
            code="version_conflict",
            expected=conflict.expected,
            submitted=conflict.submitted,
        ) from conflict

    if result.guideline.id != guideline_id:  # pragma: no cover — defensive
        raise problems.not_found(f"No guideline {guideline_id}.")

    ruleset = result.ruleset or await versions.current_ruleset(db, result.guideline)
    return PublishResponse(
        guideline_id=result.guideline.id,
        version=result.version,
        version_major=result.version_major,
        version_minor=result.version_minor,
        status=result.guideline.status,
        ruleset_version=ruleset.ruleset_version if ruleset else None,
        ruleset_id=ruleset.id if ruleset else None,
        rule_count=ruleset.rule_count if ruleset else 0,
        published_at=result.guideline.published_at,
        published_by=result.guideline.published_by,
        superseded=result.superseded,
        already_published=result.already_published,
    )


@router.get(
    "/guidelines/published/ruleset",
    response_model=PublishedRuleSet,
    summary="*** THE STAGE 04 CONTRACT *** — the ruleset governing this project",
)
async def published_ruleset(
    project_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    pin: str | None = None,
) -> PublishedRuleSet:
    """§12.2 and §16 rule 4. `404` when nothing is published — never an empty set.

    **The absence is the contract.** §16 rule 4: "Stage 04 must handle that
    rather than falling back to 'no rules'." An empty ruleset *is* "no rules",
    so returning one with a 200 would let a creative run lint every asset
    against nothing and report a clean pass.

    **What "governing" means is not `guideline.ruleset_id`.** That column
    records what the rulebook was published *with* and never moves again — the
    trigger forbids it. An amendment mints `v{major}.{minor+1}` as a new
    `rule_set` row and leaves the guideline untouched, so after one mechanical
    amendment the governing ruleset and the published one are different rows.
    `versions.current_ruleset` resolves the governing one; `pin` overrides it
    with an exact historical version, which is how an asset made six months ago
    is re-audited against the rules that actually applied.
    """
    if await ProjectRepo(db, me.workspace_id).get(project_id) is None:
        raise problems.not_found(f"No project {project_id}.")

    guideline = (
        await db.execute(
            sa.select(ContentGuideline)
            .where(
                ContentGuideline.workspace_id == me.workspace_id,
                ContentGuideline.project_id == project_id,
                ContentGuideline.status == GuidelineStatus.PUBLISHED,
            )
            .order_by(ContentGuideline.version_major.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if guideline is None:
        raise problems.not_found(
            "No content guidelines have been published for this project. Stage 04 must "
            "treat this as 'no rulebook exists yet' and stop — not as 'there are no "
            "rules'.",
            title="Nothing published",
        )

    row = (
        await _ruleset_by_pin(db, me.workspace_id, pin)
        if pin
        else await versions.current_ruleset(db, guideline)
    )
    if row is None:
        raise problems.not_found(
            f"No ruleset {pin!r} for this project."
            if pin
            else "The published guideline has no compiled ruleset, which should be "
            "impossible — publish mints one in the same transaction.",
            title="Nothing published",
        )
    return _as_ruleset(row, guideline)


@router.get(
    "/guidelines/published/creative-context",
    response_model=CreativeContext,
    summary="The published guideline's creative context, at the governing pin or `pin`",
)
async def published_creative_context(
    project_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    pin: str | None = None,
) -> CreativeContext:
    """Stage 04 PRD §4.3 — the Law 27 resolution. `404` when nothing is published.

    Resolves exactly like `published_ruleset` so the two can never name
    different pins for the same question: no `pin` means the governing ruleset
    of the latest published guideline; a `pin` means that historical ruleset,
    whose guideline may since have been superseded — which is the point of a
    pin. Either way the projection is of a published rulebook only, never a
    draft (`guidelines/projection.py`).
    """
    if await ProjectRepo(db, me.workspace_id).get(project_id) is None:
        raise problems.not_found(f"No project {project_id}.")

    if pin:
        row = await _ruleset_by_pin(db, me.workspace_id, pin)
        if row is None or row.project_id != project_id:
            raise problems.not_found(
                f"No ruleset {pin!r} for this project.", title="Nothing published"
            )
    else:
        guideline = (
            await db.execute(
                sa.select(ContentGuideline)
                .where(
                    ContentGuideline.workspace_id == me.workspace_id,
                    ContentGuideline.project_id == project_id,
                    ContentGuideline.status == GuidelineStatus.PUBLISHED,
                )
                .order_by(ContentGuideline.version_major.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        row = await versions.current_ruleset(db, guideline) if guideline else None
        if row is None:
            raise problems.not_found(
                "No content guidelines have been published for this project, so there is "
                "no creative context. Stage 04 must stop, not write without guidance.",
                title="Nothing published",
            )
    try:
        return await build_creative_context(
            db, row.guideline_id, row.ruleset_version, workspace_id=me.workspace_id
        )
    except ProjectionNotFound as missing:
        raise problems.not_found(str(missing), title="Nothing published") from missing


@router.get(
    "/rulesets/{ruleset_version}",
    response_model=PublishedRuleSet,
    summary="Any historical ruleset, by pin — superseded or not",
)
async def ruleset_by_pin(ruleset_version: str, me: AnyMember, db: Db) -> PublishedRuleSet:
    """§16 rule 5. A superseded pin still resolves, and returns exactly what it did.

    This is the whole reason a creative run records `ruleset_version` rather
    than resolving the current one: an asset produced under v1 can be re-audited
    against v1's rules a year later, after v2 and v3 have both replaced it.
    """
    row = await _ruleset_by_pin(db, me.workspace_id, ruleset_version)
    if row is None:
        raise problems.not_found(f"No ruleset {ruleset_version!r}.")
    guideline = (
        await db.execute(sa.select(ContentGuideline).where(ContentGuideline.id == row.guideline_id))
    ).scalar_one_or_none()
    if guideline is None:  # pragma: no cover — FK is ON DELETE CASCADE
        raise problems.not_found(f"No ruleset {ruleset_version!r}.")
    return _as_ruleset(row, guideline)


async def _ruleset_by_pin(db: AsyncSession, workspace_id: uuid.UUID, pin: str) -> RuleSet | None:
    return (
        await db.execute(
            sa.select(RuleSet).where(
                RuleSet.workspace_id == workspace_id, RuleSet.ruleset_version == pin
            )
        )
    ).scalar_one_or_none()


def _as_ruleset(row: RuleSet, guideline: ContentGuideline) -> PublishedRuleSet:
    return PublishedRuleSet(
        ruleset_version=row.ruleset_version,
        ruleset_id=row.id,
        guideline_id=row.guideline_id,
        project_id=row.project_id,
        compiler_version=row.compiler_version,
        constants_version=row.constants_version,
        rule_count=row.rule_count,
        hash=row.hash,
        compiled=row.compiled,
        guideline_status=guideline.status,
        published_at=guideline.published_at,
        stale=bool(guideline.signature_stale or guideline.binding_superseded),
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# exporting a rulebook (Stage 03 PRD §14)
# ---------------------------------------------------------------------------


@router.post(
    "/guidelines/{guideline_id}/export",
    response_model=ExportAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate an export of a content rulebook",
    dependencies=[Depends(throttle(EXPORT_QUOTA))],
)
async def request_guideline_export(
    guideline_id: uuid.UUID,
    me: AnyMember,
    request: Request,
    db: Db,
    export_format: Annotated[
        ExportFormat,
        Query(alias="format", description="pdf | docx | md | json | xlsx | ruleset_json"),
    ],
) -> ExportAccepted:
    """Queue one rulebook export.

    `READ`, not a write permission, for the reason the plan route gives: §14
    gives every role the export, and the write-shaped verb is about where the
    work happens — a job row and a file on the worker's volume — not about
    privilege. A `viewer` may export a rulebook and may not publish one.

    Every format of a non-published guideline is watermarked, which is enforced
    in `guideline_view.build_context` rather than here: a route that decided it
    would be a second place the rule lived, and §14's requirement is that a
    draft claims register cannot circulate as a legal sign-off record *in any
    format*.
    """
    if export_format not in CONTENT_GUIDELINE_FORMATS:
        raise problems.unprocessable(
            f"A content guideline cannot be exported as {export_format.value}. Choose one "
            f"of: {', '.join(sorted(item.value for item in CONTENT_GUIDELINE_FORMATS))}.",
            title="Unsupported export format",
        )

    guideline = (
        await db.execute(
            sa.select(ContentGuideline).where(
                ContentGuideline.id == guideline_id,
                ContentGuideline.workspace_id == me.workspace_id,
            )
        )
    ).scalar_one_or_none()
    if guideline is None:
        raise problems.not_found(f"No guideline {guideline_id}.")

    if export_format is ExportFormat.RULESET_JSON and guideline.ruleset_id is None:
        # Refused here rather than in the worker: a queued job that can never
        # succeed is a worse answer than a 422 that says what to do. A ruleset
        # exists only once a rulebook has been published.
        raise problems.unprocessable(
            "This rulebook has no compiled ruleset, so there is nothing to hand Stage 04. "
            "Publish it first — a ruleset is minted in the publish transaction.",
            title="Nothing to export",
            code="no_ruleset",
        )

    project = await ProjectRepo(db, me.workspace_id).get(guideline.project_id)
    export = ExportRepo(db, me.workspace_id).add(
        guideline.id,
        export_format,
        artifact_type=ExportArtifactType.CONTENT_GUIDELINE,
        requested_by=me.user.id,
    )
    await db.flush()

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.EXPORT_REQUESTED,
        target_type=AuditTarget.EXPORT,
        target_id=export.id,
        meta={
            "guideline_id": str(guideline.id),
            "guideline_run_id": str(guideline.guideline_run_id),
            "format": export_format.value,
            "guideline_status": guideline.status.value,
            "version": f"{guideline.version_major}.{guideline.version_minor}",
        },
        ip=client_ip(request),
    )
    # Committed before the job is queued: the worker looks this row up by id,
    # so enqueueing first is a race it can lose.
    await db.commit()
    await enqueue_export(export.id)

    return ExportAccepted(
        job_id=export.id,
        export=ExportJob(
            id=export.id,
            report_id=export.artifact_id,
            run_id=guideline.guideline_run_id,
            format=export.format,
            status=export.status,
            bytes=export.bytes,
            filename=guideline_filename_for(
                export.format,
                project_name=project.name if project else None,
                version=f"{guideline.version_major}.{guideline.version_minor}",
                generated_at=export.created_at,
            ),
            error=export.error,
            created_at=export.created_at,
            ready_at=export.ready_at,
        ),
    )
