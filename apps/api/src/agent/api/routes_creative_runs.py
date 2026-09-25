"""A creative run's own reads, and "Check again" (Stage 04 PRD §16).

The Creative Console and the brief page (§15.4 C–D) are built on these:

- `GET /creative-runs/{id}/brief` — the brief on record, its hash, and what
  approving it authorises. The gate itself stays on the approvals surface:
  G7 is decided through `POST /approvals/{id}` like every other gate, and
  `can_decide` is read from there, never re-derived here.
- `GET /creative-runs/{id}/assets` — what the run wrote, with each asset's
  stored lint verdict.
- `GET /creative-runs/{id}/generation-jobs` — every media request, estimate
  beside actual.
- `POST /generation-jobs/{id}/check` — queue `MediaJobs.check()` for a job it
  can act on. The re-poll waits out a video's poll window, so it runs in the
  worker; this route only decides whether there is anything to check.
- `GET /creative-runs/{id}/landing-audits` — every landing URL 4.5.1/4.5.2
  audited, with its verdict and the facts behind it.
- `GET /landing-audits/{id}/patch?format=html|json` — the `LandingPagePatch`
  for the site owner. Stage 04 deploys nothing (law 41); this is the handover.

The Ad Studio (§15.4 E) writes through three more:

- `POST /creative-runs/{id}/lint-preview` — the pinned linter's verdict on
  text nobody has saved, for the chip beside an edit. Nothing is written.
- `PATCH /creative-assets/{id}` — a person's rewrite of a headline or a
  description: the checks its node applied (`creative/edits.py`), then lint at
  the run's current pin; only a pass is stored (law 33), with
  `lineage.origin = 'human_edit'`.
- `POST /creative-assets/{id}/swap` — a reserve into the ad in place of the
  asset named, re-checked and re-linted at the current pin first.

The Media Library (§15.4 G) regenerates through one more:

- `POST /creative-assets/{id}/regenerate` — an operator's, while G8 is still
  pending: the model (allowlisted), its params (capability-validated) and the
  budget are checked before the 202, the new asset is committed, and the
  worker produces it through node 4.4.6's code (`orchestrator/regeneration.py`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any, Literal

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Response, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.routes_media import Catalogue
from agent.api.schemas_creative_runs import (
    AssetTextEdit,
    BriefAuthorisation,
    CreativeAssetItem,
    CreativeAssetListResponse,
    CreativeBriefResponse,
    GenerationCheckAccepted,
    GenerationJobItem,
    GenerationJobListResponse,
    LandingAuditItem,
    LandingAuditListResponse,
    LintPreviewRequest,
    RegenerateAccepted,
    RegenerateRequest,
    ReserveSwap,
    ReserveSwapResponse,
)
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.creative import brief as briefs
from agent.creative import edits, g7, review
from agent.db.models import (
    Approval,
    ApprovalStatus,
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    GenerationJob,
    LandingPageAudit,
    Project,
    Run,
    RunStage,
    Workspace,
)
from agent.db.models import CreativeBrief as CreativeBriefRow
from agent.db.session import get_session, get_sessionmaker
from agent.media import runtime as media_runtime
from agent.media.budget import MediaBudget, resolve_media_caps, run_spend
from agent.media.jobs import checkable
from agent.nodes.base import CreativeResources, NodeContractError
from agent.nodes.creative._ad_groups import Slot, search_slots
from agent.nodes.creative._regenerate import (
    MEDIA_KINDS,
    RegenerationRequest,
    estimate_usd,
    open_child,
)
from agent.nodes.creative._text_assets import content_hash
from agent.orchestrator.creative_run import CreativeRunError, load_resources
from agent.queue import enqueue_generation_check, enqueue_regeneration
from agent.redis_client import get_redis
from agent.schemas.creative_brief import MAX_RENDERED_WORDS, CreativeBrief
from agent.schemas.creative_input import CreativeInput
from agent.schemas.creative_review import G8, AiAssetReview, ReviewDecisionItem
from agent.schemas.guardrails import LintResult, LintTarget
from agent.schemas.landing import LandingPagePatch
from agent.schemas.search_ads import PASSING

log = structlog.get_logger(__name__)

router = APIRouter(tags=["creative"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
#: `admin` and `operator`, as for starting a run (CR-E15).
CreativeOperator = Annotated[Principal, Depends(require(Permission.CREATIVE_EXECUTE))]


async def _creative_run(db: AsyncSession, me: Principal, run_id: uuid.UUID) -> Run:
    run = await db.scalar(
        sa.select(Run).where(Run.id == run_id, Run.workspace_id == me.workspace_id)
    )
    if run is None or run.stage is not RunStage.CREATIVE:
        raise problems.not_found(f"No creative run {run_id}.")
    return run


# ---------------------------------------------------------------------------
# brief
# ---------------------------------------------------------------------------


@router.get(
    "/creative-runs/{run_id}/brief",
    response_model=CreativeBriefResponse,
    summary="The run's brief, its hash, and what approving it authorises",
)
async def get_brief(run_id: uuid.UUID, me: AnyMember, db: Db) -> CreativeBriefResponse:
    run = await _creative_run(db, me, run_id)
    row = await db.scalar(
        sa.select(CreativeBriefRow).where(CreativeBriefRow.creative_run_id == run.id)
    )
    if row is None:
        raise problems.not_found(
            "This run has not written its brief yet. Node 4.1.1 writes it first; it appears "
            "here the moment that node finishes.",
            title="No brief yet",
        )
    if not run.creative_input:  # pragma: no cover — a creative run cannot start without it
        raise problems.conflict(f"Run {run.id} carries no creative input.", title="No input")
    brief = CreativeBrief.model_validate(row.payload)
    authorised = g7.authorises(brief, CreativeInput.model_validate(run.creative_input))
    approval_id = row.approval_id or await db.scalar(
        sa.select(Approval.id)
        .where(Approval.run_id == run.id, Approval.gate_key == g7.G7)
        .order_by(Approval.created_at.desc())
        .limit(1)
    )
    return CreativeBriefResponse(
        run_id=run.id,
        brief=brief,
        brief_hash=row.brief_hash,
        approved_hash=row.approved_hash,
        word_count=brief.rendered_word_count,
        max_words=MAX_RENDERED_WORDS,
        authorises=BriefAuthorisation(
            rsas=authorised.rsas,
            images=authorised.images,
            videos=authorised.videos,
            media_usd=authorised.media_usd,
        ),
        approval_id=approval_id,
    )


# ---------------------------------------------------------------------------
# assets
# ---------------------------------------------------------------------------


@router.get(
    "/creative-runs/{run_id}/assets",
    response_model=CreativeAssetListResponse,
    summary="What the run wrote, each with its stored lint verdict",
)
async def list_assets(
    run_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    kind: Annotated[CreativeAssetKind | None, Query(description="headline | image | …")] = None,
    ad_group_ref: Annotated[str | None, Query(description="One ad group's assets")] = None,
    asset_status: Annotated[
        CreativeAssetStatus | None, Query(alias="status", description="draft | approved | …")
    ] = None,
) -> CreativeAssetListResponse:
    run = await _creative_run(db, me, run_id)
    query = sa.select(CreativeAsset).where(CreativeAsset.creative_run_id == run.id)
    if kind is not None:
        query = query.where(CreativeAsset.kind == kind)
    if ad_group_ref is not None:
        query = query.where(CreativeAsset.ad_group_ref == ad_group_ref)
    if asset_status is not None:
        query = query.where(CreativeAsset.status == asset_status)
    rows = (
        (await db.execute(query.order_by(CreativeAsset.created_at, CreativeAsset.id)))
        .scalars()
        .all()
    )
    return CreativeAssetListResponse(items=[_asset(row) for row in rows])


def _asset(row: CreativeAsset) -> CreativeAssetItem:
    verdict = (row.lint or {}).get("verdict")
    return CreativeAssetItem(
        id=row.id,
        node_id=row.node_id,
        campaign_ref=row.campaign_ref,
        ad_group_ref=row.ad_group_ref,
        ad_ref=row.ad_ref,
        kind=row.kind,
        surface=row.surface,
        variant=row.variant,
        category=row.category,
        text=row.text,
        fields=row.fields,
        claim_ids=list(row.claim_ids),
        pin_position=row.pin_position,
        generated_by_ai=row.generated_by_ai,
        status=row.status,
        lint_verdict=verdict if isinstance(verdict, str) else None,
        ruleset_version=row.ruleset_version,
        lineage=row.lineage,
        content_hash=row.content_hash,
        frozen_at=row.frozen_at,
        created_at=row.created_at,
    )


# ---------------------------------------------------------------------------
# generation jobs
# ---------------------------------------------------------------------------


@router.get(
    "/creative-runs/{run_id}/generation-jobs",
    response_model=GenerationJobListResponse,
    summary="Every media request of the run, estimate beside actual",
)
async def list_generation_jobs(
    run_id: uuid.UUID, me: AnyMember, db: Db
) -> GenerationJobListResponse:
    run = await _creative_run(db, me, run_id)
    rows = (
        (
            await db.execute(
                sa.select(GenerationJob)
                .where(GenerationJob.creative_run_id == run.id)
                .order_by(GenerationJob.created_at, GenerationJob.id)
            )
        )
        .scalars()
        .all()
    )
    choices = media_runtime.pinned_choices(run)
    # §15.5 item 4: a control the caller cannot use is absent, so the answer
    # carries both halves — would it do anything, and may *this* caller ask.
    may_check = Permission.CREATIVE_EXECUTE in me.permissions
    return GenerationJobListResponse(
        items=[
            _job(
                row, can_check=may_check and checkable(row, media_runtime.choice_for(choices, row))
            )
            for row in rows
        ]
    )


def _job(row: GenerationJob, *, can_check: bool) -> GenerationJobItem:
    return GenerationJobItem(
        id=row.id,
        node_id=row.node_id,
        asset_id=row.asset_id,
        round=row.round,
        modality=row.modality,
        model_id=row.model_id,
        provider_tag=row.provider_tag,
        status=row.status,
        estimate_usd=row.estimate_usd,
        cost_usd=row.cost_usd,
        attempts=row.attempts,
        polls=row.polls,
        error=row.error,
        submitted_at=row.submitted_at,
        completed_at=row.completed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
        can_check=can_check,
    )


@router.post(
    "/generation-jobs/{job_id}/check",
    response_model=GenerationCheckAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Check again: re-poll a timed-out video, or re-submit an unknown-state image",
)
async def check_generation_job(
    job_id: uuid.UUID, me: CreativeOperator, db: Db
) -> GenerationCheckAccepted:
    row = await db.scalar(
        sa.select(GenerationJob).where(
            GenerationJob.id == job_id, GenerationJob.workspace_id == me.workspace_id
        )
    )
    if row is None:
        raise problems.not_found(f"No generation job {job_id}.")
    run = await db.get(Run, row.creative_run_id)
    if not checkable(row, media_runtime.choice_for(media_runtime.pinned_choices(run), row)):
        raise problems.conflict(
            f"This job is {row.status.value}. Check again re-polls a video that timed out, "
            "or re-submits an image whose submit state is unknown; a video whose submit "
            "state is unknown is never re-submitted, because that can bill twice.",
            title="Nothing to check",
            status=row.status.value,
        )
    try:
        queued = await enqueue_generation_check(
            row.id, state=f"{row.status.value}.{row.polls}.{row.updated_at.timestamp():.0f}"
        )
    except (RedisError, OSError) as exc:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Queue unavailable",
            detail="The check could not be queued. Try again once Redis is back.",
        ) from exc
    log.info(
        "generation_check.requested",
        generation_job_id=str(row.id),
        run_id=str(row.creative_run_id),
        status=row.status.value,
        by=str(me.user.id),
        queued=queued is not None,
    )
    return GenerationCheckAccepted(job_id=row.id, status=row.status, queued=queued is not None)


# ---------------------------------------------------------------------------
# landing audits (4.5)
# ---------------------------------------------------------------------------


@router.get(
    "/creative-runs/{run_id}/landing-audits",
    response_model=LandingAuditListResponse,
    summary="Every landing URL the run audited, with its verdict and the facts behind it",
)
async def list_landing_audits(run_id: uuid.UUID, me: AnyMember, db: Db) -> LandingAuditListResponse:
    run = await _creative_run(db, me, run_id)
    rows = (
        (
            await db.execute(
                sa.select(LandingPageAudit)
                .where(LandingPageAudit.creative_run_id == run.id)
                .order_by(LandingPageAudit.created_at, LandingPageAudit.url)
            )
        )
        .scalars()
        .all()
    )
    return LandingAuditListResponse(items=[_landing(row) for row in rows])


def _landing(row: LandingPageAudit) -> LandingAuditItem:
    metrics: dict[str, Any] = row.metrics or {}
    return LandingAuditItem.model_validate(
        {
            "id": row.id,
            "creative_run_id": row.creative_run_id,
            "url": row.url,
            "final_url": row.final_url,
            "http_status": row.http_status,
            "ad_group_refs": list(row.ad_group_refs),
            "verdict": row.verdict,
            "reasons": metrics.get("reasons") or [],
            "h1": metrics.get("h1") or {},
            "fold_px": metrics.get("fold_px") or {},
            "obscured_by_overlay": metrics.get("obscured_by_overlay") or {},
            "message_match": metrics.get("message_match"),
            "proposed_h1": metrics.get("proposed_h1"),
            "proposed_h1_note": metrics.get("proposed_h1_note"),
            "offer_above_fold": metrics.get("offer_above_fold") or [],
            "form": metrics.get("form"),
            "screenshots": row.screenshots or {},
            "has_patch": row.patch is not None,
            "evidence_ids": list(row.evidence_ids),
            "created_at": row.created_at,
        }
    )


#: The HTML patch is markup for someone else's site, served from ours: it may
#: be read and copied, never run. Every value in it is escaped when it is
#: built; this makes a browser that opens it directly render it inert too.
_PATCH_HTML_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


@router.get(
    "/landing-audits/{audit_id}/patch",
    response_model=LandingPagePatch,
    responses={200: {"content": {"text/html": {}}}},
    summary="The landing-page patch for the site owner, as JSON or as markup",
)
async def get_landing_patch(
    audit_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    patch_format: Annotated[
        Literal["html", "json"], Query(alias="format", description="html | json")
    ] = "json",
) -> Any:
    row = await db.scalar(
        sa.select(LandingPageAudit)
        .join(Run, Run.id == LandingPageAudit.creative_run_id)
        .where(LandingPageAudit.id == audit_id, Run.workspace_id == me.workspace_id)
    )
    if row is None:
        raise problems.not_found(f"No landing audit {audit_id}.")
    if row.patch is None:
        raise problems.not_found(
            f"The audit of {row.url} proposes no change: its verdict is "
            f"{row.verdict.value}. A patch exists only when the H1, the offer or the "
            "form needs one.",
            title="No patch",
        )
    patch = LandingPagePatch.model_validate(row.patch)
    if patch_format == "html":
        return Response(
            content=patch.html_snippet,
            media_type="text/html; charset=utf-8",
            headers={
                **_PATCH_HTML_HEADERS,
                "Content-Disposition": f'inline; filename="landing-patch-{row.id}.html"',
            },
        )
    return patch


# ---------------------------------------------------------------------------
# the Ad Studio: lint preview, edit, swap
# ---------------------------------------------------------------------------


async def _resources(db: AsyncSession, run: Run) -> CreativeResources:
    """The run's input, pinned linter and constants — what its nodes lint with."""
    project = await db.get(Project, run.project_id)
    if project is None:  # pragma: no cover — the run's FK guarantees it
        raise problems.not_found(f"No project {run.project_id}.")
    try:
        return await load_resources(db, run, project)
    except CreativeRunError as exc:
        raise problems.conflict(
            f"{exc} Nothing can be linted against this run until that is resolved.",
            title="The run cannot lint",
            code=exc.code,
        ) from exc


@router.post(
    "/creative-runs/{run_id}/lint-preview",
    response_model=LintResult,
    summary="Lint text at the run's pin, as a candidate is linted at creation. No side effects",
)
async def lint_preview(
    run_id: uuid.UUID, body: LintPreviewRequest, me: AnyMember, db: Db
) -> LintResult:
    run = await _creative_run(db, me, run_id)
    resources = await _resources(db, run)
    return resources.linter.lint_candidates(
        [edits.as_linted(target) for target in body.targets], now=datetime.now(UTC)
    )


async def _locked_assets(
    db: AsyncSession, me: Principal, ids: list[uuid.UUID]
) -> dict[uuid.UUID, CreativeAsset]:
    """The rows, locked in id order so two swaps over the same pair cannot deadlock."""
    rows = (
        (
            await db.execute(
                sa.select(CreativeAsset)
                .where(CreativeAsset.id.in_(ids), CreativeAsset.workspace_id == me.workspace_id)
                .order_by(CreativeAsset.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    found = {row.id: row for row in rows}
    for asset_id in ids:
        if asset_id not in found:
            raise problems.not_found(f"No creative asset {asset_id}.")
    return found


def _changeable(row: CreativeAsset) -> None:
    if row.frozen_at is not None:
        raise problems.conflict(
            f"Asset {row.id} was frozen when its package was released "
            f"({row.frozen_at:%d %b %Y}), and a released package is immutable (law 42). "
            "Start a new creative run to change its copy.",
            title="Asset frozen",
            code="asset_frozen",
        )
    if row.kind not in edits.EDITABLE_KINDS:
        raise problems.unprocessable(
            f"A {row.kind.value.replace('_', ' ')} is not changed here: the Ad Studio edits "
            "headlines and descriptions.",
            title="Not editable here",
            code="kind_not_editable",
        )
    if row.status not in edits.EDITABLE_STATUSES:
        raise problems.conflict(
            f"Asset {row.id} is {row.status.value}. Only an asset the ad carries, or one it "
            "keeps in reserve, can be changed.",
            title="Not in the ad",
            code="asset_not_editable",
            asset_status=row.status.value,
        )


async def _slot(
    db: AsyncSession, run: Run, resources: CreativeResources, row: CreativeAsset
) -> Slot:
    """The ad group the asset was written for: its keywords, market and language."""
    brief_row = await db.scalar(
        sa.select(CreativeBriefRow).where(CreativeBriefRow.creative_run_id == run.id)
    )
    if brief_row is None:  # pragma: no cover — 4.1.1 writes the brief before any copy
        raise problems.conflict(f"Run {run.id} has no brief.", title="No brief")
    brief = CreativeBrief.model_validate(brief_row.payload)
    try:
        slots = search_slots(brief, resources.input.account_structure.campaigns)
    except NodeContractError as exc:  # pragma: no cover — 4.2.1 wrote against these slots
        raise problems.conflict(str(exc), title="Brief and plan disagree") from exc
    for slot in slots:
        if (slot.brief.campaign_ref, slot.brief.ad_group_ref) == (
            row.campaign_ref,
            row.ad_group_ref,
        ):
            return slot
    raise problems.conflict(
        f"Asset {row.id} belongs to {row.campaign_ref} / {row.ad_group_ref}, which is not a "
        "Search ad group of this run's brief.",
        title="No such ad group",
    )


def _lint(
    resources: CreativeResources, row: CreativeAsset, slot: Slot, text: str, now: datetime
) -> LintResult:
    """The asset's text at the run's current pin, exactly as its node linted it (law 33)."""
    result = resources.linter.lint_candidate(
        LintTarget(
            ref=str(row.id),
            surface=row.surface,
            campaign_type=slot.campaign_type,
            market=slot.market,
            language=slot.language,
            text=edits.linted_text(row.surface, text),
            generated_by_ai=row.generated_by_ai,
        ),
        now=now,
    )
    if result.verdict not in PASSING:
        blocking = [f.message for f in result.findings if f.severity == "blocking"]
        raise problems.unprocessable(
            f"It fails lint at ruleset {result.ruleset_version}: "
            f"{' '.join(blocking[:3] or [f.message for f in result.findings[:3]])} "
            "Nothing was saved.",
            title="Fails lint",
            code="lint_failed",
            lint=result.model_dump(mode="json"),
        )
    return result


def _checked(
    row: CreativeAsset, text: str, slot: Slot, resources: CreativeResources, now: datetime
) -> edits.Edited:
    licensed = edits.licensed_ids(briefs.licensed_claims(resources.linter.ruleset, now))
    try:
        return edits.check_edit(row, text, keywords=slot.keywords, licensed=licensed)
    except edits.EditRefused as exc:
        raise problems.unprocessable(
            f"{exc.detail} Nothing was saved.", title="Edit refused", code=exc.code
        ) from exc


def _relint(row: CreativeAsset, fields: dict[str, object], result: LintResult) -> None:
    row.fields = dict(fields)
    row.lint = result.model_dump(mode="json")
    row.ruleset_version = result.ruleset_version
    row.content_hash = content_hash(
        kind=row.kind,
        surface=row.surface,
        text=row.text,
        fields=row.fields,
        claim_ids=row.claim_ids,
        offer_binding=row.offer_binding,
    )


@router.patch(
    "/creative-assets/{asset_id}",
    response_model=CreativeAssetItem,
    summary="Rewrite a headline or description: checked, linted at the pin, lineage human_edit",
)
async def edit_asset(
    asset_id: uuid.UUID, body: AssetTextEdit, me: CreativeOperator, db: Db
) -> CreativeAssetItem:
    row = (await _locked_assets(db, me, [asset_id]))[asset_id]
    _changeable(row)
    if body.text == row.text:
        return _asset(row)
    run = await db.get(Run, row.creative_run_id)
    if run is None:  # pragma: no cover — FK
        raise problems.not_found(f"No creative run {row.creative_run_id}.")
    resources = await _resources(db, run)
    slot = await _slot(db, run, resources, row)
    now = datetime.now(UTC)
    edited = _checked(row, body.text, slot, resources, now)
    result = _lint(resources, row, slot, body.text, now)
    before = row.content_hash
    row.lineage = edits.edit_lineage(row, me.user.id)
    row.text = body.text
    _relint(row, edited.fields, result)
    await db.commit()
    log.info(
        "creative_asset.edited",
        asset_id=str(row.id),
        run_id=str(row.creative_run_id),
        kind=row.kind.value,
        verdict=result.verdict,
        ruleset_version=result.ruleset_version,
        content_hash_before=before,
        content_hash=row.content_hash,
        by=str(me.user.id),
    )
    return _asset(row)


@router.post(
    "/creative-assets/{asset_id}/swap",
    response_model=ReserveSwapResponse,
    summary="Swap a reserve into the ad in place of this asset, re-linted at the pin first",
)
async def swap_asset(
    asset_id: uuid.UUID, body: ReserveSwap, me: CreativeOperator, db: Db
) -> ReserveSwapResponse:
    """The reserve goes in unpinned: a pin belongs to the order-dependent pair 4.2.3
    judged, and the reserve was never in it. What it pairs with now is checked
    again by the final lint and preview (4.6.4), not here."""
    if body.with_reserve_id == asset_id:
        raise problems.unprocessable(
            "An asset cannot be swapped for itself. Name one of the ad group's reserves.",
            code="swap_self",
        )
    rows = await _locked_assets(db, me, [asset_id, body.with_reserve_id])
    out, into = rows[asset_id], rows[body.with_reserve_id]
    _changeable(out)
    _changeable(into)
    if out.status is not CreativeAssetStatus.LINTED:
        raise problems.conflict(
            f"Asset {out.id} is a reserve, not in the ad: swap a reserve in for one the ad "
            "carries.",
            title="Not in the ad",
            code="not_carried",
        )
    if into.status is not CreativeAssetStatus.RESERVE:
        raise problems.conflict(
            f"Asset {into.id} is already in the ad. Swap in one of the ad group's reserves.",
            title="Not a reserve",
            code="not_a_reserve",
        )
    if not edits.same_slot(out, into):
        raise problems.unprocessable(
            f"Reserve {into.id} was written for {into.campaign_ref} / {into.ad_group_ref} "
            f"({into.variant.value if into.variant else '-'} {into.kind.value}), not for the "
            f"slot {out.id} fills. Swap in a reserve of the same ad, variant and kind.",
            title="Another ad's reserve",
            code="swap_slot_mismatch",
        )
    run = await db.get(Run, out.creative_run_id)
    if run is None:  # pragma: no cover — FK
        raise problems.not_found(f"No creative run {out.creative_run_id}.")
    resources = await _resources(db, run)
    slot = await _slot(db, run, resources, into)
    now = datetime.now(UTC)
    if into.text is None:  # pragma: no cover — text kinds only (_changeable)
        raise problems.unprocessable(f"Reserve {into.id} has no text.")
    edited = _checked(into, into.text, slot, resources, now)
    result = _lint(resources, into, slot, into.text, now)
    out.status = CreativeAssetStatus.RESERVE
    out.pin_position = None
    into.status = CreativeAssetStatus.LINTED
    into.lineage = edits.swap_lineage(into, out, me.user.id)
    _relint(into, edited.fields, result)
    await db.commit()
    log.info(
        "creative_asset.swapped",
        run_id=str(out.creative_run_id),
        out=str(out.id),
        into=str(into.id),
        ruleset_version=result.ruleset_version,
        by=str(me.user.id),
    )
    return ReserveSwapResponse(out=_asset(out), into=_asset(into))


# ---------------------------------------------------------------------------
# media regeneration before G8 (§16, §9.2)
# ---------------------------------------------------------------------------


@router.post(
    "/creative-assets/{asset_id}/regenerate",
    response_model=RegenerateAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Regenerate an AI image or video before G8: model, params and budget checked first",
)
async def regenerate_asset(
    asset_id: uuid.UUID, body: RegenerateRequest, me: CreativeOperator, db: Db, catalogue: Catalogue
) -> RegenerateAccepted:
    """§5.2: an operator regenerates media *before G8* — while it waits on the
    card, undecided. At G8 the brand owner regenerates through the review
    itself (4.4.6), and after it there is no route back: G8b has no
    regeneration (§8.5)."""
    parent = await db.scalar(
        sa.select(CreativeAsset)
        .where(CreativeAsset.id == asset_id, CreativeAsset.workspace_id == me.workspace_id)
        .with_for_update()
    )
    if parent is None:
        raise problems.not_found(f"No creative asset {asset_id}.")
    if parent.kind not in MEDIA_KINDS or not parent.generated_by_ai:
        raise problems.unprocessable(
            f"Asset {asset_id} is a {parent.kind.value.replace('_', ' ')}; only an AI-made image "
            "or video is regenerated. Copy is edited in the Ad Studio.",
            title="Not regenerable",
            code="kind_not_regenerable",
        )
    if parent.frozen_at is not None:
        raise problems.conflict(
            f"Asset {asset_id} was frozen when its package was released (law 42).",
            title="Asset frozen",
            code="asset_frozen",
        )
    run = await db.get(Run, parent.creative_run_id)
    if run is None:  # pragma: no cover — FK
        raise problems.not_found(f"No creative run {parent.creative_run_id}.")
    card = await _pending_g8(db, run, parent)

    item = next(i for i in card.items if i.asset_id == parent.id)
    sent = ReviewDecisionItem(
        asset_id=parent.id,
        decision="regenerate",
        note=body.note,
        model_override=body.model_override,
        params_override=body.params_override,
    )
    workspace = await db.get(Workspace, run.workspace_id)
    pinned = media_runtime.pinned_choices(run)
    try:
        choice = await review.regeneration_choice(
            item, sent, pinned=pinned, workspace=workspace, catalogue=catalogue
        )
    except review.ReviewRefused as exc:
        raise problems.unprocessable(
            str(exc),
            title="This regeneration cannot be made",
            asset_id=str(parent.id),
            field=exc.field,
            code=exc.code,
            **{k: v for k, v in exc.extra.items() if k not in {"asset_id", "field", "code"}},
        ) from exc

    request = RegenerationRequest(
        parent_id=parent.id, choice=choice, note=body.note.strip(), by_user=me.user.id, round=1
    )
    project = await db.get(Project, run.project_id)
    resources = await _resources(db, run)
    specs = resources.linter.ruleset.asset_specs.model_dump(mode="json").get("specs") or {}
    caps = resolve_media_caps(
        project_settings=project.settings if project else None,
        workspace_settings=workspace.settings if workspace else None,
        defaults=get_settings(),
    )
    estimate = estimate_usd(
        resources.input,
        specs,
        parent,
        choice,
        caps=caps,
        constants=resources.constants.media_constants(),
    )
    media_left, total_left = await _remaining(db, run, caps)
    busy = await review.running_children(db, [parent.id])
    if busy:
        return await _redrive(
            busy[0], parent, request, estimate=estimate, remaining=(media_left, total_left)
        )
    if estimate > media_left or estimate > total_left:
        raise problems.conflict(
            f"Regenerating this {parent.kind.value} spends ≈ ${estimate:.2f}; "
            f"${media_left:.2f} of the media budget and ${total_left:.2f} of the creative "
            "budget remain. Raise the cap in project settings, or choose a cheaper model or "
            "params.",
            title="Over budget",
            code="estimate_exceeds_budget",
            estimate_usd=str(estimate),
            media_remaining_usd=str(media_left),
            total_remaining_usd=str(total_left),
        )

    child_id = await open_child(get_sessionmaker(), parent, request)
    await db.commit()
    queued = await _enqueue(child_id, redrive=False)
    log.info(
        "creative_asset.regeneration_requested",
        run_id=str(run.id),
        parent=str(parent.id),
        asset=str(child_id),
        model=choice.model_id,
        estimate_usd=str(estimate),
        by=str(me.user.id),
    )
    return RegenerateAccepted(
        job_id=queued,
        asset_id=child_id,
        parent_asset_id=parent.id,
        model_id=choice.model_id,
        estimate_usd=estimate,
        media_remaining_usd=media_left,
        total_remaining_usd=total_left,
    )


async def _pending_g8(db: AsyncSession, run: Run, parent: CreativeAsset) -> AiAssetReview:
    """The pending G8 card this asset waits on — refused otherwise, saying why."""
    approval = await db.scalar(
        sa.select(Approval)
        .where(Approval.run_id == run.id, Approval.gate_key == G8)
        .order_by(Approval.created_at.desc())
    )
    if approval is None:
        raise problems.conflict(
            "This run's media is still being produced; regenerate from the G8 review once it "
            "opens.",
            title="G8 is not open yet",
            code="review_not_open",
        )
    if approval.status is not ApprovalStatus.PENDING:
        raise problems.conflict(
            f"G8 was {approval.status.value}. An operator regenerates before G8 is decided; "
            "after it, regeneration is the brand owner's, through the review (and G8b offers "
            "none).",
            title="G8 already decided",
            code="review_decided",
            status=approval.status.value,
        )
    card = AiAssetReview.model_validate(approval.proposal)
    if parent.id not in {item.asset_id for item in card.items}:
        raise problems.conflict(
            f"Asset {parent.id} is not on the G8 card"
            + (
                f" — it is {parent.status.value}."
                if parent.status.value != "awaiting_review"
                else "."
            ),
            title="Not awaiting review",
            code="not_on_card",
            asset_status=parent.status.value,
        )
    return card


async def _redrive(
    child: CreativeAsset,
    parent: CreativeAsset,
    request: RegenerationRequest,
    *,
    estimate: Decimal,
    remaining: tuple[Decimal, Decimal],
) -> RegenerateAccepted:
    """The same regeneration asked again while it is still `running` — how a
    worker that died mid-way is recovered: it is queued again under a fresh id
    and resumes from its committed jobs (Law 37). A different request for the
    same asset waits for the first."""
    recorded = RegenerationRequest.of(child)
    if (recorded.choice, recorded.note) != (request.choice, request.note):
        raise problems.conflict(
            f"Asset {parent.id} is already being regenerated (new asset {child.id}) with "
            f"{recorded.choice.model_id}. Wait for it to finish, then regenerate that one.",
            title="Already regenerating",
            code="regeneration_in_progress",
            regenerated_asset_id=str(child.id),
        )
    queued = await _enqueue(child.id, redrive=True)
    return RegenerateAccepted(
        job_id=queued,
        asset_id=child.id,
        parent_asset_id=parent.id,
        model_id=recorded.choice.model_id,
        estimate_usd=estimate,
        media_remaining_usd=remaining[0],
        total_remaining_usd=remaining[1],
    )


async def _enqueue(asset_id: uuid.UUID, *, redrive: bool) -> str | None:
    try:
        return await enqueue_regeneration(asset_id, redrive=redrive)
    except (RedisError, OSError) as exc:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Queue unavailable",
            detail=f"Regeneration {asset_id} is recorded but could not be queued. Ask again once "
            "Redis is back; it resumes where it is.",
        ) from exc


async def _remaining(db: AsyncSession, run: Run, caps: Any) -> tuple[Decimal, Decimal]:
    """Law 43's two meters' headroom, read from the three places the reserve
    script reads — caps, committed media, Redis's reservations — so the check
    before the 202 and the guard at submit agree about "how close"."""
    committed = await db.scalar(
        sa.select(sa.func.coalesce(sa.func.sum(GenerationJob.cost_usd), 0)).where(
            GenerationJob.creative_run_id == run.id, GenerationJob.cost_usd.is_not(None)
        )
    )
    try:
        state = await MediaBudget(get_redis()).state(run.id)
    except (RedisError, OSError) as exc:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Budget unavailable",
            detail="Reserved spend lives in Redis, which is unreachable; nothing is regenerated "
            "without knowing what remains. Try again once it is back.",
        ) from exc
    spend = run_spend(
        caps=caps,
        run_cost_usd=run.cost_usd,
        media_committed_usd=Decimal(committed or 0),
        state=state,
    )
    return (
        spend.media.cap_usd - spend.media.spent_usd - spend.media.reserved_usd,
        spend.total.cap_usd - spend.total.spent_usd - spend.total.reserved_usd,
    )
