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

import uuid
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_guidelines import (
    AvailableBindings,
    EligibilityNote,
    GuidelineDetail,
    GuidelineEligibility,
    GuidelineRunAccepted,
    GuidelineVersion,
    GuidelineVersionList,
    PlanBinding,
    ResearchBinding,
    StartGuidelineRequest,
)
from agent.api.throttle import throttle
from agent.auth.deps import Principal, require
from agent.auth.ratelimit import RUN_QUOTA
from agent.auth.rbac import Permission
from agent.credentials import MissingCredential, resolve_values
from agent.db.models import (
    AmendmentStatus,
    CampaignPlan,
    CampaignPlanStatus,
    ContentGuideline,
    CredentialKind,
    GuidelineStatus,
    Membership,
    PolicyAmendment,
    Report,
    ResearchAcceptance,
    RunStage,
    RunTrigger,
    SignOffMatrix,
    UserRole,
    UserStatus,
)
from agent.db.repos import ProjectRepo
from agent.db.session import get_session
from agent.orchestrator.guideline_input import build_guideline_input
from agent.orchestrator.launch import LaunchRequest, ProjectBusy, QueueUnavailable, launch
from agent.orchestrator.state import RunLock
from agent.redis_client import get_redis

log = structlog.get_logger(__name__)

router = APIRouter(tags=["guidelines"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
GuidelineOperator = Annotated[Principal, Depends(require(Permission.GUIDELINE_EXECUTE))]


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
                # The existing run console, not `/guidelines/runs/...`: the
                # Guideline Console arrives in S3-P7 and a fix_url that 404s
                # is worse than no link.
                fix_url=f"{home}/runs/{holder.run_id}",
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
    rows = (
        (
            await db.execute(
                sa.select(ContentGuideline)
                .where(
                    ContentGuideline.workspace_id == me.workspace_id,
                    ContentGuideline.project_id == project_id,
                )
                .order_by(
                    ContentGuideline.version_major.desc(),
                    ContentGuideline.version_minor.desc(),
                    ContentGuideline.created_at.desc(),
                )
            )
        )
        .scalars()
        .all()
    )
    return GuidelineVersionList(
        versions=[GuidelineVersion.model_validate(row, from_attributes=True) for row in rows]
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
