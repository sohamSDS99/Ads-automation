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
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Literal

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Response, status
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.schemas_creative_runs import (
    BriefAuthorisation,
    CreativeAssetItem,
    CreativeAssetListResponse,
    CreativeBriefResponse,
    GenerationCheckAccepted,
    GenerationJobItem,
    GenerationJobListResponse,
    LandingAuditItem,
    LandingAuditListResponse,
)
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.creative import g7
from agent.db.models import (
    Approval,
    CreativeAsset,
    CreativeAssetKind,
    CreativeAssetStatus,
    GenerationJob,
    LandingPageAudit,
    Run,
    RunStage,
)
from agent.db.models import CreativeBrief as CreativeBriefRow
from agent.db.session import get_session
from agent.media import runtime as media_runtime
from agent.media.jobs import checkable
from agent.queue import enqueue_generation_check
from agent.schemas.creative_brief import MAX_RENDERED_WORDS, CreativeBrief
from agent.schemas.creative_input import CreativeInput
from agent.schemas.landing import LandingPagePatch

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
