"""The Media Library's two server reads (Stage 04 PRD §15.4 G, §16 "Media").

- `GET /media/{media_id}/content?variant=preview|poster|master` — a `302` to
  the worker's file server for one stored file, signed for 300 s (HMAC,
  `export/tokens.py`). The api never reads the bytes (§16 contract rule 7): the
  browser follows the redirect to `/files/…`, which the web tier proxies to the
  file server over the private network, and seeks a video there with `Range`
  (`206`). `preview` and `poster` resolve through `derived_from`; a variant
  that was never made is a 404 — never the master in its place, because the
  grid must never load one (§15.5 items 2–3).
- `GET /media-references/{reference_id}/content` (S4-P22) — the same `302` for
  a product or style reference, so G8's `ReferenceCompare` can put the real
  product beside the generated asset (§15.4 H). §16 lists and uploads
  references but gave nothing a browser could load one from. Retired
  references are served too: a gate opened before the retirement still
  compares against what the model was given.
- `POST /creative-assets/{id}/regeneration-estimate` — what regenerating a
  media asset would cost and what would remain of the media cap, before
  anything is submitted (§15.2 rule 8). The model and parameters are resolved
  exactly as a start resolves them — allowlist ∩ live catalogue, parameters
  validated against the capability record (Law 36) — so a request the
  regeneration would refuse is refused here first, with the same 422. Nothing
  is reserved, spent or written.
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Annotated, Any
from urllib.parse import quote

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.routes_media import Catalogue, _unprocessable
from agent.api.routes_runs import _creative_spend
from agent.api.schemas_media_library import (
    MediaVariant,
    RegenerationEstimate,
    RegenerationEstimateRequest,
)
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.creative.constants import creative_constants_for
from agent.db.models import (
    CreativeAsset,
    CreativeAssetKind,
    MediaArtifact,
    MediaArtifactRole,
    MediaReference,
    NodeRun,
    Project,
    Run,
    RunStage,
    Workspace,
)
from agent.db.session import get_session
from agent.export.tokens import sign
from agent.media import runtime as media_runtime
from agent.media.regeneration_price import PlannedClip, regeneration_price
from agent.orchestrator import creative_input as inputs
from agent.orchestrator.creative_input import CreativeInputError
from agent.schemas.creative_input import CreativeScope, MediaModelSelection
from agent.schemas.creative_media import ImageMasters
from agent.schemas.creative_video import VideoProduction

log = structlog.get_logger(__name__)

router = APIRouter(tags=["media"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]

#: §16: "302 -> HMAC file-server URL (TTL 300 s)".
CONTENT_TTL_SECONDS = 300
#: Where the browser reaches the file server: the web tier proxies this path to
#: the worker (`apps/web/next.config.ts`). Relative, so the redirect stays on
#: the origin the page was served from.
FILES_PATH = "/files"
#: The redirect may be reused while the token behind it has time left; a
#: minute short of the TTL, so a cached hop never lands on an expired token.
REDIRECT_MAX_AGE_SECONDS = CONTENT_TTL_SECONDS - 60


# ---------------------------------------------------------------------------
# content
# ---------------------------------------------------------------------------


async def _variant(db: AsyncSession, row: MediaArtifact, variant: MediaVariant) -> MediaArtifact:
    if variant == "master":
        return row
    role = MediaArtifactRole.PREVIEW if variant == "preview" else MediaArtifactRole.POSTER
    if row.role is role:
        return row
    found = await db.scalar(
        sa.select(MediaArtifact)
        .where(MediaArtifact.derived_from == row.id, MediaArtifact.role == role)
        .order_by(MediaArtifact.created_at.desc(), MediaArtifact.id)
        .limit(1)
    )
    if found is None:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title=f"No {variant} for this file",
            detail=(
                f"Media {row.id} has no {variant}: it was made before previews were stored, "
                "or it is not a kind of file that has one. Open it in the detail view to "
                "load the file itself."
                if variant == "preview"
                else f"Media {row.id} has no poster frame; only a finished video has one."
            ),
            code="media_variant_missing",
            variant=variant,
        )
    return found


@router.get(
    "/media/{media_id}/content",
    status_code=status.HTTP_302_FOUND,
    summary="Redirect to one stored media file, signed for 300 s and seekable with Range",
    responses={302: {"description": "`Location` is the signed file-server URL."}},
)
async def media_content(
    media_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    variant: Annotated[MediaVariant, Query()] = "preview",
) -> Response:
    row = await db.scalar(
        sa.select(MediaArtifact).where(
            MediaArtifact.id == media_id, MediaArtifact.workspace_id == me.workspace_id
        )
    )
    if row is None:
        raise problems.not_found(f"No media {media_id}.")
    target = await _variant(db, row, variant)
    key = target.storage_path
    token = sign(key, ttl_seconds=CONTENT_TTL_SECONDS)
    log.info(
        "media_content.signed",
        media_id=str(media_id),
        variant=variant,
        served_id=str(target.id),
        role=target.role.value,
    )
    return Response(
        status_code=status.HTTP_302_FOUND,
        headers={
            # Quoted as `worker_files.signed_url` quotes it: the key's `/`s stay,
            # and the file server's one `{key:path}` parameter takes them all.
            "Location": f"{FILES_PATH}/{quote(key)}?token={token}",
            "Cache-Control": f"private, max-age={REDIRECT_MAX_AGE_SECONDS}",
        },
    )


@router.get(
    "/media-references/{reference_id}/content",
    status_code=status.HTTP_302_FOUND,
    summary="Redirect to one product or style reference, signed for 300 s",
    responses={302: {"description": "`Location` is the signed file-server URL."}},
)
async def media_reference_content(reference_id: uuid.UUID, me: AnyMember, db: Db) -> Response:
    row = await db.scalar(
        sa.select(MediaReference).where(
            MediaReference.id == reference_id, MediaReference.workspace_id == me.workspace_id
        )
    )
    if row is None:
        raise problems.not_found(f"No media reference {reference_id}.")
    key = row.storage_path
    token = sign(key, ttl_seconds=CONTENT_TTL_SECONDS)
    log.info("media_reference_content.signed", reference_id=str(reference_id))
    return Response(
        status_code=status.HTTP_302_FOUND,
        headers={
            "Location": f"{FILES_PATH}/{quote(key)}?token={token}",
            "Cache-Control": f"private, max-age={REDIRECT_MAX_AGE_SECONDS}",
        },
    )


# ---------------------------------------------------------------------------
# regeneration estimate
# ---------------------------------------------------------------------------


async def _output(db: AsyncSession, run_id: uuid.UUID, node_id: str) -> dict[str, Any] | None:
    """The node's newest stored output — a failed attempt stores none."""
    outputs = await db.scalars(
        sa.select(NodeRun.output)
        .where(NodeRun.run_id == run_id, NodeRun.node_id == node_id)
        .order_by(NodeRun.attempt.desc())
    )
    return next((output for output in outputs if output), None)


@router.post(
    "/creative-assets/{asset_id}/regeneration-estimate",
    response_model=RegenerationEstimate,
    summary="Price regenerating one media asset against the media cap. No spend, no writes",
)
async def regeneration_estimate(
    asset_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    body: RegenerationEstimateRequest,
    catalogue: Catalogue,
) -> RegenerationEstimate:
    asset = await db.scalar(
        sa.select(CreativeAsset).where(
            CreativeAsset.id == asset_id, CreativeAsset.workspace_id == me.workspace_id
        )
    )
    if asset is None:
        raise problems.not_found(f"No creative asset {asset_id}.")
    if asset.kind not in (CreativeAssetKind.IMAGE, CreativeAssetKind.VIDEO):
        raise problems.unprocessable(
            f"A {asset.kind.value} asset is not generated media; only images and videos are "
            "regenerated.",
            title="Nothing to regenerate",
            code="not_generated_media",
        )
    run = await db.get(Run, asset.creative_run_id)
    if run is None or run.stage is not RunStage.CREATIVE:
        raise problems.not_found(f"No creative run for asset {asset_id}.")
    modality = "image" if asset.kind is CreativeAssetKind.IMAGE else "video"
    pinned = next((c for c in media_runtime.pinned_choices(run) if c.modality == modality), None)
    if pinned is None:
        raise problems.conflict(
            f"This run was started without a {modality} model, so it has nothing to "
            "regenerate with.",
            title="No media model on this run",
            code="media_model_unselected",
        )

    model_id = body.model_override or pinned.model_id
    same = model_id == pinned.model_id and body.provider_tag in (None, pinned.provider_tag)
    selection = MediaModelSelection(
        modality=modality,
        model_id=model_id,
        provider_tag=body.provider_tag if body.model_override else pinned.provider_tag,
        # The run's validated defaults carry over only to the model they were
        # validated for; a switched model starts from the workspace's defaults.
        defaults={**(pinned.defaults if same else {}), **body.params_override},
    )
    scope = CreativeScope(
        images=modality == "image", video=modality == "video", concepts_per_campaign=2
    )
    try:
        (choice,) = await inputs.resolve_media_models(
            db, me.workspace_id, scope, [selection], catalogue=catalogue
        )
    except CreativeInputError as refused:
        raise _unprocessable(refused) from refused

    project = await db.get(Project, run.project_id)
    if project is None:  # pragma: no cover — the run's FK holds it
        raise problems.not_found(f"No project for asset {asset_id}.")
    constants = creative_constants_for(project).media_constants()
    aspect_ratio: str | None = None
    clips: list[PlannedClip] | None = None
    if modality == "image":
        masters = await _output(db, run.id, "4.4.2")
        if masters:
            made = ImageMasters.model_validate(masters)
            aspect_ratio = next(
                (c.aspect_ratio for c in made.concepts if c.asset_id == asset.id), None
            )
    else:
        stored = await _output(db, run.id, "4.4.4")
        videos = VideoProduction.model_validate(stored).videos if stored else []
        found = next((v for v in videos if v.asset_id == asset.id), None)
        clips = (
            [PlannedClip(ratio=c.ratio, duration_s=c.duration_s) for c in found.clips]
            if found
            else None
        )
    try:
        price = regeneration_price(
            choice, constants=constants, aspect_ratio=aspect_ratio, clips=clips
        )
    except CreativeInputError as refused:
        raise _unprocessable(refused) from refused
    except ValueError as unpriced:  # CalcError
        raise problems.unprocessable(
            str(unpriced), title="This regeneration cannot be priced", code="estimate_unavailable"
        ) from unpriced

    workspace = await db.get(Workspace, me.workspace_id)
    spend = await _creative_spend(db, run, project=project, workspace=workspace)
    if spend is None:
        # Reserved money lives only in Redis: a remaining figure without it
        # would overstate what is left (the meters withhold for the same reason).
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Budget unavailable",
            detail="What remains of the media cap could not be read. Try again once Redis is back.",
            code="budget_unavailable",
        )
    media = spend.media
    remaining = max(Decimal(0), media.cap_usd - media.spent_usd - media.reserved_usd)
    estimate = price.usd.quantize(Decimal("0.0001"))
    return RegenerationEstimate(
        asset_id=asset.id,
        modality=modality,
        model_id=choice.model_id,
        provider_tag=choice.provider_tag,
        params=price.params,
        requests=price.requests,
        estimate_usd=estimate,
        confidence=price.confidence,
        media=media,
        remaining_usd=remaining,
        remaining_after_usd=max(Decimal(0), remaining - estimate),
        fits=estimate <= remaining,
    )
