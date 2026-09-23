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
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, get_args

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, File, Form, Request, UploadFile, status
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from agent import queue
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
    ImageLintFinding,
    ImageLintResult,
    ImageMetrics,
    PlanBinding,
    ResearchBinding,
    StartGuidelineRequest,
)
from agent.api.throttle import throttle
from agent.auth.deps import Principal, require
from agent.auth.ratelimit import RUN_QUOTA
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.credentials import MissingCredential, resolve_values
from agent.db.models import (
    AmendmentStatus,
    CampaignPlan,
    CampaignPlanStatus,
    ContentGuideline,
    CredentialKind,
    EvidenceSource,
    GuidelineStatus,
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
    UserRole,
    UserStatus,
)
from agent.db.repos import ProjectRepo
from agent.db.session import get_session
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.store import EvidenceScopeError, EvidenceStore
from agent.guardrails.compiler import compiler_version, ruleset_hash
from agent.guardrails.linter import lint
from agent.guidelines import versions
from agent.guidelines.constants import get_content_constants
from agent.orchestrator.guideline_input import build_guideline_input
from agent.orchestrator.launch import LaunchRequest, ProjectBusy, QueueUnavailable, launch
from agent.orchestrator.state import RunLock
from agent.redis_client import get_redis
from agent.schemas.guardrails import LintResult, LintTarget, LogoTemplate, Rule, Surface
from agent.schemas.guardrails import RuleSet as RuleSetContract
from agent.schemas.imaging import ImageMeasurement

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

    # The artifact exists from the moment the run does. It has to: 3.2.2
    # registers claims a dozen nodes before 3.6.1 synthesises anything, and
    # `ClaimRecord.first_seen_guideline_id` is NOT NULL. `payload` and
    # `markdown` are nullable for exactly this window — see
    # `guidelines/versions.ensure_draft`.
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
