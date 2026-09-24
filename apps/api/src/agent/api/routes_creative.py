"""Stage 04's gated entry path (PRD §4.2, §16).

Stage 03's entry had no handshake and said so. This one is gated again, and
harder than Stage 02's: a creative run needs a frozen, non-superseded plan
**and** a published ruleset (law 32). CR-E1 and CR-E2 are blockers and are
never softened into warnings — doing so would rebuild Stage 03's independence
in the one stage that cannot have it.

Carried over unchanged from both earlier stages: **eligibility is computed in
one place and read in two.** `GET .../eligibility` renders it; `POST .../runs`
re-runs the same function before it takes the lock. The resolvers it is built
from live in `orchestrator/creative_input.py`, so the answer the Start button
shows and the input the run is built from agree about which plan and which pin.
"""

from __future__ import annotations

import shutil
import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_creative import (
    CreativeBlocker,
    CreativeEligibility,
    CreativeOverview,
    CreativePackageSummary,
    CreativeRunAccepted,
    CreativeRunSummary,
    CreativeWarning,
    StartCreativeRequest,
)
from agent.api.throttle import throttle
from agent.auth.deps import Principal, require
from agent.auth.ratelimit import RUN_QUOTA
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.credentials import MissingCredential, resolve_values
from agent.db.models import (
    AmendmentStatus,
    CreativePackage,
    CreativePackageStatus,
    CredentialKind,
    PolicyAmendment,
    Project,
    Run,
    RunStage,
    RunTrigger,
    Workspace,
)
from agent.db.repos import ProjectRepo
from agent.db.session import get_session
from agent.guidelines.constants import get_content_constants
from agent.guidelines.projection import project as project_context
from agent.orchestrator import creative_input as inputs
from agent.orchestrator.creative_input import CreativeInputError, build_creative_input
from agent.orchestrator.launch import LaunchRequest, ProjectBusy, QueueUnavailable, launch
from agent.orchestrator.state import RunLock
from agent.redis_client import get_redis
from agent.schemas.creative_input import CreativeScope

log = structlog.get_logger(__name__)

router = APIRouter(tags=["creative"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
#: CR-E15. `admin` and `operator`; an approver releases creative and never
#: makes it (PRD §5.1).
CreativeOperator = Annotated[Principal, Depends(require(Permission.CREATIVE_EXECUTE))]

#: The rule categories CR-E4 needs a rule in before a run can check anything,
#: and the ones whose absence is only worth saying out loud.
REQUIRED_CATEGORIES: tuple[str, ...] = ("asset_spec", "claim")
EXPECTED_CATEGORIES: tuple[str, ...] = ("lexicon", "policy", "image", "disclosure")

#: Where `Workspace.settings` records that the workspace enforces zero data
#: retention. PRD §4.2 CR-E10 names the condition and no key; this is the one
#: place the key is spelled (see docs/stage-04-questions.md).
ZDR_SETTING = "zdr_enforced"


# ---------------------------------------------------------------------------
# eligibility
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/creative/eligibility",
    response_model=CreativeEligibility,
    summary="Can a creative run start on this project",
)
async def creative_eligibility(
    project_id: uuid.UUID, me: AnyMember, db: Db
) -> CreativeEligibility:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")
    return await _eligibility(db, me, project, default_scope(project))


def default_scope(project: Project) -> CreativeScope:
    """The scope a run would take if nobody changed it: text, plus any media
    modality the project has a default model for (`settings.media_models`,
    PRD §7.1). Two concepts per campaign is the lower of the two §4.3 allows."""
    media = (project.settings or {}).get("media_models") or {}
    return CreativeScope(
        images=bool(media.get("image")) if isinstance(media, dict) else False,
        video=bool(media.get("video")) if isinstance(media, dict) else False,
        concepts_per_campaign=2,
    )


def storage_blocker(*, free_bytes: int, footprint_bytes: int) -> CreativeBlocker | None:
    """CR-E11: free space must be at least twice the estimated media footprint."""
    if free_bytes >= 2 * footprint_bytes:
        return None
    return CreativeBlocker(
        code="storage_insufficient",
        detail=(
            f"The media volume has {free_bytes:,} bytes free; this run needs at least "
            f"{2 * footprint_bytes:,} (twice its estimated {footprint_bytes:,}-byte "
            "footprint). Free space or prune unreleased runs first."
        ),
        fix_url="/settings",
    )


def media_footprint_bytes(scope: CreativeScope) -> int | None:
    """The media a run of this scope would write, in bytes.

    A text-only run writes none. A media run's footprint is priced from its
    shot plan, which arrives with the media gateway in S4-P1 — and until then
    CR-E8 blocks every media scope as `media_not_configured`, so no run that
    would need the number can start. `None` means "not estimable yet", never
    "zero".
    """
    if not (scope.images or scope.video):
        return 0
    return None


async def _eligibility(
    db: AsyncSession, me: Principal, project: Project, scope: CreativeScope
) -> CreativeEligibility:
    """PRD §4.2, CR-E1…CR-E15. Pure read: no writes, no locks taken.

    Every check runs even after one fails — the person reading this wants the
    whole list. `eligible` is computed from `blockers` alone, so a warning
    cannot stop a run however a client renders it.
    """
    blockers: list[CreativeBlocker] = []
    warnings: list[CreativeWarning] = []
    pins: dict[str, str | int | None] = {}
    ws, pid = me.workspace_id, project.id
    home = f"/projects/{pid}"

    # CR-E1 — the load-bearing line. A blocker, never anything else.
    plan = await inputs.frozen_plan(db, ws, pid)
    if plan is None:
        latest = await inputs.latest_plan(db, ws, pid)
        if latest is None:
            state = "No campaign plan exists yet — run and freeze a plan first."
        elif latest.status.value == "ready_to_freeze":
            state = (
                f"The latest plan (run {str(latest.plan_run_id)[:8]}) is ready_to_freeze; "
                "nobody has frozen it."
            )
        elif latest.status.value == "frozen":
            state = (
                f"Plan v{latest.version} is frozen but its research was superseded; re-plan "
                "from the current research and freeze again."
            )
        else:
            state = (
                f"The latest plan (run {str(latest.plan_run_id)[:8]}) is {latest.status.value}; "
                "creative needs a frozen plan to write into."
            )
        blockers.append(CreativeBlocker(code="no_frozen_plan", detail=state, fix_url=f"{home}/plan"))
    else:
        pins |= {
            "plan_id": str(plan.id),
            "plan_version": plan.version,
            "plan_run_id": str(plan.plan_run_id),
        }

    # CR-E2 — the other load-bearing line.
    pin = await inputs.published_pin(db, ws, pid)
    if pin is None:
        blockers.append(
            CreativeBlocker(
                code="no_published_ruleset",
                detail=(
                    "No content guidelines are published for this project, so there are no "
                    "rules to check creative against. 'No rules' is never a fallback."
                ),
                fix_url=f"{home}/guidelines",
            )
        )
    else:
        pins |= {
            "guideline_id": str(pin.guideline.id),
            "ruleset_version": pin.ruleset.ruleset_version,
            "ruleset_hash": pin.ruleset.hash,
        }

    # CR-E3 — needs both halves; caught before a token is spent.
    if plan is not None and pin is not None:
        skew = inputs.schema_skew(plan, pin)
        if skew is not None:
            blockers.append(
                CreativeBlocker(code="schema_unsupported", detail=skew, fix_url=f"{home}/plan")
            )
        else:
            pins["context_hash"] = project_context(pin.guideline, pin.ruleset).hash

    # CR-E4 — what the pinned ruleset can actually check.
    if pin is not None:
        categories = {
            str(rule.get("category"))
            for rule in (pin.ruleset.compiled or {}).get("rules", [])
            if isinstance(rule, dict)
        }
        missing_required = [c for c in REQUIRED_CATEGORIES if c not in categories]
        if missing_required:
            blockers.append(
                CreativeBlocker(
                    code="ruleset_incomplete",
                    detail=(
                        f"Ruleset {pin.ruleset.ruleset_version} has no "
                        f"{' or '.join(missing_required)} rules, so creative could not be "
                        "checked for specs or licensed claims. Publish a rulebook that has them."
                    ),
                    fix_url=f"{home}/guidelines",
                )
            )
        for category in EXPECTED_CATEGORIES:
            if category not in categories:
                warnings.append(
                    CreativeWarning(
                        code="ruleset_category_missing",
                        detail=(
                            f"Ruleset {pin.ruleset.ruleset_version} has no {category} rules; "
                            f"nothing will be checked for {category}."
                        ),
                        fix_url=f"{home}/guidelines",
                    )
                )

    # CR-E5 — G7, G8 and H3 route to named people.
    if await inputs.current_signoff(db, ws, pid) is None:
        blockers.append(
            CreativeBlocker(
                code="no_signoff_matrix",
                detail=(
                    "No current sign-off matrix names brand, legal and performance owners, so "
                    "the brief, the media review and legal exceptions have nobody to route to."
                ),
                fix_url=f"{home}/guidelines",
            )
        )

    # CR-E6 — one creative run per project.
    holder = await RunLock(get_redis(), RunStage.CREATIVE).holder(pid)
    if holder is not None:
        blockers.append(
            CreativeBlocker(
                code="creative_in_flight",
                detail=(
                    f"{holder.user_name or 'Someone'} is already running creative for this "
                    f"project (run {holder.run_id})."
                ),
                fix_url=f"{home}/runs/{holder.run_id}",
            )
        )

    # CR-E7 — a model key, resolved the way every stage resolves one.
    try:
        await resolve_values(db, workspace_id=ws, kind=CredentialKind.OPENROUTER)
    except MissingCredential:
        blockers.append(
            CreativeBlocker(
                code="missing_credential",
                detail="No OpenRouter credential resolves for this workspace, so a creative "
                "run has no model to call.",
                fix_url="/settings/sources",
            )
        )

    # CR-E8/E9 — S4-P0 has no media gateway, so any enabled modality blocks.
    # S4-P1 replaces this with model selection, allowlist, catalogue and the
    # pre-flight estimate against both caps.
    for modality, enabled in (("image", scope.images), ("video", scope.video)):
        if enabled:
            blockers.append(
                CreativeBlocker(
                    code="media_not_configured",
                    detail=(
                        f"{modality.capitalize()} generation is enabled for this project but "
                        "no media model can be selected yet. Turn it off to run text-only."
                    ),
                    fix_url=f"{home}/settings",
                )
            )

    # CR-E10 — OpenRouter does not route video under ZDR.
    if scope.video:
        workspace = await db.get(Workspace, ws)
        if workspace is not None and (workspace.settings or {}).get(ZDR_SETTING) is True:
            blockers.append(
                CreativeBlocker(
                    code="zdr_blocks_video",
                    detail=(
                        "This workspace enforces zero data retention, and OpenRouter does not "
                        "route video under ZDR. Disable video for this run, or disable ZDR."
                    ),
                    fix_url="/settings",
                )
            )

    # CR-E11 — only a scope with a known footprint can be measured; see
    # `media_footprint_bytes` for why a media scope has none yet.
    footprint = media_footprint_bytes(scope)
    if footprint:
        usage = shutil.disk_usage(get_settings().storage_dir)
        found = storage_blocker(free_bytes=usage.free, footprint_bytes=footprint)
        if found is not None:
            blockers.append(found)

    # CR-E12 — guideline flags. Warnings, never blockers.
    if pin is not None:
        if pin.guideline.signature_stale:
            warnings.append(
                CreativeWarning(
                    code="claims_unlicensed_stale",
                    detail=(
                        "The legal owner's claims signature is stale, so claims it covered are "
                        "unlicensed until it is re-signed. Copy will not assert them."
                    ),
                    fix_url=f"{home}/guidelines/claims",
                )
            )
        amendments = (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(PolicyAmendment)
                .where(
                    PolicyAmendment.workspace_id == ws,
                    PolicyAmendment.project_id == pid,
                    PolicyAmendment.status == AmendmentStatus.NEEDS_REVIEW,
                )
            )
        ).scalar_one()
        if amendments:
            warnings.append(
                CreativeWarning(
                    code="unreviewed_amendments",
                    detail=(
                        f"{amendments} policy amendment(s) are waiting for review; this run "
                        f"is pinned to {pin.ruleset.ruleset_version} and does not apply them."
                    ),
                    fix_url=f"{home}/guidelines/amendments",
                )
            )
        h2 = await inputs.open_h2_tasks(db, ws, pin.guideline)
        if h2:
            warnings.append(
                CreativeWarning(
                    code="verification_open_blocks_launch",
                    detail=(
                        f"{len(h2)} verification task(s) are still open. Creative can be made; "
                        "it cannot launch until they are complete."
                    ),
                    fix_url="/tasks",
                )
            )

    # CR-E13 — offer freshness. Stage 03's own staleness window (Q4), so the
    # two stages cannot disagree about when offer data is old.
    max_age = int(get_content_constants().offers.staleness_warning_days.value)
    offers = await inputs.offer_snapshot(db, pid)
    if offers.freshest is None or offers.freshest < datetime.now(UTC) - timedelta(days=max_age):
        seen = (
            "There is no offer data for this project"
            if offers.freshest is None
            else f"The newest offer data is from {offers.freshest:%Y-%m-%d}"
        )
        warnings.append(
            CreativeWarning(
                code="offer_data_stale",
                detail=(
                    f"{seen} (older than {max_age} days counts as stale). Promotion and price "
                    "assets will be skipped, not guessed."
                ),
                fix_url=f"{home}/documents",
            )
        )

    # CR-E14 — a released package already exists for this plan version.
    if plan is not None:
        released = (
            await db.execute(
                sa.select(sa.func.max(CreativePackage.version)).where(
                    CreativePackage.workspace_id == ws,
                    CreativePackage.project_id == pid,
                    CreativePackage.plan_id == plan.id,
                    CreativePackage.status == CreativePackageStatus.RELEASED,
                )
            )
        ).scalar_one()
        if released is not None:
            warnings.append(
                CreativeWarning(
                    code="will_mint_new_version",
                    detail=(
                        f"Package v{released} is already released for plan v{plan.version}. "
                        "It stays released; this run produces a new version."
                    ),
                    fix_url=f"{home}/creative",
                )
            )

    # CR-E15 — on the read endpoint a row, not a 403: every role may ask
    # whether the project is ready, and only the answer to "can I start it"
    # depends on who is asking. POST enforces it as a 403.
    if Permission.CREATIVE_EXECUTE not in me.permissions:
        blockers.append(
            CreativeBlocker(
                code="missing_permission",
                detail=(
                    f"Your role ({me.role.value}) does not hold the 'creative_execute' "
                    "permission. An admin or operator can start this run."
                ),
                fix_url=home,
            )
        )

    return CreativeEligibility(
        eligible=not blockers, blockers=blockers, warnings=warnings, pins=pins
    )


# ---------------------------------------------------------------------------
# starting a run
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/creative/runs",
    response_model=CreativeRunAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Start a creative run",
    dependencies=[Depends(throttle(RUN_QUOTA))],
)
async def start_creative_run(
    project_id: uuid.UUID,
    me: CreativeOperator,
    request: Request,
    body: StartCreativeRequest,
    db: Db,
) -> CreativeRunAccepted:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")

    # Before eligibility: a media request is refused as what it is — an
    # unsupported request, 422 — rather than as a project that is not ready.
    try:
        inputs.reject_media(body.scope, body.media_models)
    except CreativeInputError as refused:
        raise _unprocessable(refused) from refused

    # Re-checked server-side, for the run's own scope. The Start button is a
    # cache of this answer; this is the answer.
    eligibility = await _eligibility(db, me, project, body.scope)
    if not eligibility.eligible:
        skew = next((b for b in eligibility.blockers if b.code == "schema_unsupported"), None)
        if skew is not None:
            # Version skew is the Stage 02 §4.3 rule: a 422 at trigger time
            # naming both versions — the request cannot be served by waiting,
            # which is what a 409 would imply.
            raise problems.unprocessable(
                skew.detail,
                title="Cannot start creative",
                code=skew.code,
                blockers=[item.model_dump() for item in eligibility.blockers],
            )
        first = eligibility.blockers[0]
        raise problems.conflict(
            first.detail,
            title="A creative run cannot start yet",
            code=first.code,
            blockers=[item.model_dump() for item in eligibility.blockers],
        )

    # Minted here, before the row: the input names its run and is hashed into
    # that run's `input_hash`.
    run_id = uuid.uuid4()
    try:
        built, input_hash = await build_creative_input(
            db,
            project_id,
            body.scope,
            body.media_models,
            workspace_id=me.workspace_id,
            creative_run_id=run_id,
        )
    except CreativeInputError as refused:
        raise _unprocessable(refused) from refused

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
                stage=RunStage.CREATIVE,
                # `ck_run_creative_has_source`: the frozen plan's run.
                source_run_id=built.plan_ref.plan_run_id,
                input_hash=input_hash,
                reuse_cache=body.reuse_cache,
                pins=[
                    {
                        "ruleset_version": built.ruleset_ref.ruleset_version,
                        "reason": "start",
                        "at": datetime.now(UTC).isoformat(),
                    }
                ],
                run_id=run_id,
                audit_meta={
                    "plan_version": str(built.plan_ref.version),
                    "ruleset_version": built.ruleset_ref.ruleset_version,
                    "context_hash": built.context_ref.hash,
                },
            ),
        )
    except ProjectBusy as busy:
        raise problems.conflict(
            f"{busy.holder.user_name or 'Someone'} is already running creative for this "
            f"project (run {busy.holder.run_id}).",
            title="A creative run is already in flight",
            code="creative_in_flight",
            holder=busy.holder.as_dict(),
        ) from busy
    except QueueUnavailable as unavailable:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Queue unavailable",
            detail="The creative run was recorded but could not be queued. "
            "Retry it once Redis is back.",
        ) from unavailable

    return CreativeRunAccepted(run_id=run.id, status=run.status, input_hash=input_hash)


def _unprocessable(refused: CreativeInputError) -> problems.Problem:
    extra: dict[str, Any] = {"code": refused.code, **refused.extra}
    return problems.unprocessable(refused.detail, title="Cannot start creative", **extra)


# ---------------------------------------------------------------------------
# reads
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/creative",
    response_model=CreativeOverview,
    summary="Creative runs and package history for a project",
)
async def creative_overview(project_id: uuid.UUID, me: AnyMember, db: Db) -> CreativeOverview:
    if await ProjectRepo(db, me.workspace_id).get(project_id) is None:
        raise problems.not_found(f"No project {project_id}.")
    runs = (
        (
            await db.execute(
                sa.select(Run)
                .where(
                    Run.workspace_id == me.workspace_id,
                    Run.project_id == project_id,
                    Run.stage == RunStage.CREATIVE,
                )
                .order_by(Run.started_at.desc().nulls_first(), Run.id)
            )
        )
        .scalars()
        .all()
    )
    packages = (
        (
            await db.execute(
                sa.select(CreativePackage)
                .where(
                    CreativePackage.workspace_id == me.workspace_id,
                    CreativePackage.project_id == project_id,
                )
                .order_by(CreativePackage.version.desc())
            )
        )
        .scalars()
        .all()
    )
    return CreativeOverview(
        runs=[
            CreativeRunSummary(
                run_id=run.id,
                status=run.status,
                source_run_id=run.source_run_id,
                input_hash=run.input_hash,
                pins=list(run.pins or []),
                triggered_by=run.triggered_by,
                started_at=run.started_at,
                finished_at=run.finished_at,
                cost_usd=run.cost_usd,
            )
            for run in runs
        ],
        packages=[
            CreativePackageSummary(
                package_id=package.id,
                creative_run_id=package.creative_run_id,
                version=package.version,
                status=package.status,
                plan_version=package.plan_version,
                ruleset_version=package.ruleset_version,
                released_at=package.released_at,
                plan_superseded=package.plan_superseded,
                ruleset_superseded=package.ruleset_superseded,
            )
            for package in packages
        ],
    )
