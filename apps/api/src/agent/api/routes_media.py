"""The media routes (PRD §16 "Media", §9.2–9.3; Stage 04 §23.1 item 9).

Where the user chooses. `admin` keeps two allowlists (image, video) picked from
the live catalogue — nothing is allowlisted by default — and a project keeps a
default model per modality. Every choice is resolved server-side against the
allowlist and the live catalogue, and its defaults are validated against the
capability record *before* anything is priced or spent (Law 36).

`POST /projects/{id}/creative/estimate` spends nothing and writes no
`GenerationJob`. It persists the two calculations it made — `PlanCalc` plus a
`derived` Evidence row each — anchored to the frozen plan's run, the run a
creative run is sourced from (`Run.source_run_id`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

import httpx
import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent import queue
from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_media import (
    CatalogueResponse,
    EstimateRequest,
    EstimateResponse,
    MediaAllowlist,
    MediaDefaults,
    MediaModelRow,
    MediaModelsResponse,
    MediaReferenceOut,
    MediaSettings,
    MediaSettingsUpdate,
    ProjectMediaSettings,
    ProjectMediaSettingsPatch,
    ScopeReduction,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.calc.derived import DerivedWriter
from agent.calc.media import cost_estimate_v1, ratio_plan_v1, scope_reduction
from agent.config import get_settings
from agent.creative.constants import creative_constants_for
from agent.db.models import (
    MediaReference,
    MediaReferenceKind,
    MediaReferenceOrigin,
    Project,
    Workspace,
)
from agent.db.repos import ProjectRepo
from agent.db.session import get_session
from agent.evidence.store import EvidenceStore
from agent.export.plan_contract import CampaignPlan as PlanContract
from agent.media import references
from agent.media.budget import resolve_media_caps
from agent.media.capability import capability_hash, ratio_coverage
from agent.media.catalogue import CatalogueUnavailable, MediaCatalogue
from agent.media.constants import media_constants
from agent.orchestrator import creative_input as inputs
from agent.orchestrator.creative_input import CreativeInputError
from agent.redis_client import get_redis
from agent.schemas.creative_input import CreativeScope, MediaModelSelection

log = structlog.get_logger(__name__)

router = APIRouter(tags=["media"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
SettingsWriter = Annotated[Principal, Depends(require(Permission.SETTINGS_WRITE))]
CreativeOperator = Annotated[Principal, Depends(require(Permission.CREATIVE_EXECUTE))]
ModalityParam = Literal["image", "video"]

#: The node id the estimate's calculations are recorded under.
ESTIMATE_NODE = "creative.estimate"

_client: httpx.AsyncClient | None = None


def get_media_catalogue() -> MediaCatalogue:
    """The live catalogue. Public reads, so no key is sent from the `api`
    service; tests replace this dependency with the recorded one."""
    global _client  # noqa: PLW0603 — one pool per process, like `get_redis`
    if _client is None:
        _client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
    settings = get_settings()
    return MediaCatalogue(
        client=_client,
        redis=get_redis(),
        base_url=settings.openrouter_base_url,
        referer=settings.app_base_url,
    )


Catalogue = Annotated[MediaCatalogue, Depends(get_media_catalogue)]


# ---------------------------------------------------------------------------
# models and catalogue
# ---------------------------------------------------------------------------


@router.get(
    "/media/models",
    response_model=MediaModelsResponse,
    summary="Allowlisted media models, as the live catalogue describes them",
)
async def media_models(
    me: AnyMember,
    db: Db,
    catalogue: Catalogue,
    modality: Annotated[ModalityParam, Query()],
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> MediaModelsResponse:
    workspace = await db.get(Workspace, me.workspace_id)
    image_ratios: list[str] = []
    video_ratios: list[str] = []
    if project_id is not None:
        image_ratios, video_ratios = await _project_ratios(db, me, project_id)
    constants = media_constants()
    rows: list[MediaModelRow] = []
    warning: str | None = None
    catalogue_hash: str | None = None
    for entry in inputs.allowlist(workspace, modality):
        model_id = str(entry.get("model_id"))
        provider_tag = entry.get("provider_tag")
        enabled = bool(entry.get("enabled", True))
        try:
            snapshot = await (
                catalogue.fetch_video_models()
                if modality == "video"
                else catalogue.fetch_image_models()
            )
            record = await catalogue.record_for(modality, model_id, provider_tag)
        except CatalogueUnavailable as unavailable:
            raise _catalogue_down(unavailable) from unavailable
        warning = warning or snapshot.warning
        catalogue_hash = snapshot.catalogue_hash
        if record is None:
            rows.append(
                MediaModelRow(
                    modality=modality,
                    model_id=model_id,
                    provider_tag=provider_tag,
                    enabled=enabled,
                    available=False,
                    reason="media_model_unavailable",
                )
            )
            continue
        coverage = None
        required = video_ratios if modality == "video" else image_ratios
        if required:
            coverage = dict(
                ratio_coverage(
                    required,
                    record,
                    tolerance=constants.ratio_tolerance,
                    min_retained=constants.crop_min_saliency_retained,
                )
            )
        rows.append(
            MediaModelRow(
                modality=modality,
                model_id=model_id,
                provider_tag=provider_tag,
                enabled=enabled,
                available=enabled,
                reason=None if enabled else "disabled",
                capability=record.model_dump(mode="json"),
                capability_hash=capability_hash(record),
                ratio_coverage=coverage,
            )
        )
    return MediaModelsResponse(models=rows, catalogue_hash=catalogue_hash, warning=warning)


@router.get(
    "/media/catalogue",
    response_model=CatalogueResponse,
    summary="The full live media catalogue, for the allowlist editor",
)
async def media_catalogue(
    me: SettingsWriter,
    catalogue: Catalogue,
    modality: Annotated[ModalityParam, Query()],
) -> CatalogueResponse:
    del me
    try:
        snapshot = await (
            catalogue.fetch_video_models()
            if modality == "video"
            else catalogue.fetch_image_models()
        )
    except CatalogueUnavailable as unavailable:
        raise _catalogue_down(unavailable) from unavailable
    return CatalogueResponse(
        modality=modality,
        models=[record.model_dump(mode="json") for record in snapshot.records],
        catalogue_hash=snapshot.catalogue_hash,
        fetched_at=snapshot.fetched_at.isoformat(),
        warning=snapshot.warning,
    )


# ---------------------------------------------------------------------------
# workspace settings
# ---------------------------------------------------------------------------


@router.get(
    "/settings/media", response_model=MediaSettings, summary="Media allowlists and defaults"
)
async def get_media_settings(me: AnyMember, db: Db) -> MediaSettings:
    workspace = await db.get(Workspace, me.workspace_id)
    return _media_settings(workspace)


@router.put(
    "/settings/media",
    response_model=MediaSettings,
    summary="Set the media allowlists, provider pins and workspace defaults",
)
async def put_media_settings(
    me: SettingsWriter, db: Db, request: Request, body: MediaSettingsUpdate, catalogue: Catalogue
) -> MediaSettings:
    workspace = await db.get(Workspace, me.workspace_id)
    if workspace is None:
        raise problems.not_found("No workspace.")
    settings = dict(workspace.settings or {})
    changed: dict[str, Any] = {}
    if body.media_allowlist is not None:
        for modality in ("image", "video"):
            for entry in getattr(body.media_allowlist, modality):
                try:
                    record = await catalogue.record_for(
                        modality, entry.model_id, entry.provider_tag
                    )
                except CatalogueUnavailable as unavailable:
                    raise _catalogue_down(unavailable) from unavailable
                if record is None:
                    raise problems.unprocessable(
                        f"{entry.model_id}"
                        + (f" on {entry.provider_tag}" if entry.provider_tag else "")
                        + f" is not a {modality} model in OpenRouter's live catalogue"
                        + (
                            " (a video model cannot pin a provider)."
                            if modality == "video" and entry.provider_tag
                            else "."
                        ),
                        title="Cannot allowlist this model",
                        code="media_model_unavailable",
                        modality=modality,
                        model_id=entry.model_id,
                    )
        settings[inputs.MEDIA_ALLOWLIST_SETTING] = body.media_allowlist.model_dump(mode="json")
        changed["media_allowlist"] = {
            modality: [e.model_id for e in getattr(body.media_allowlist, modality)]
            for modality in ("image", "video")
        }
    if body.media_defaults is not None:
        for modality in ("image", "video"):
            given = set(getattr(body.media_defaults, modality))
            unknown = sorted(given - inputs.DEFAULT_FIELDS[modality])
            if unknown:
                raise problems.unprocessable(
                    f"{unknown[0]!r} is not a {modality} default.",
                    title="Cannot save these defaults",
                    code="capability_unsupported",
                    field=unknown[0],
                    supported=sorted(inputs.DEFAULT_FIELDS[modality]),
                )
        settings[inputs.MEDIA_DEFAULTS_SETTING] = body.media_defaults.model_dump(mode="json")
        changed["media_defaults"] = True
    if changed:
        workspace.settings = settings
        write_audit(
            db,
            workspace_id=workspace.id,
            actor_id=me.user.id,
            action=AuditAction.WORKSPACE_UPDATED,
            target_type=AuditTarget.WORKSPACE,
            target_id=workspace.id,
            meta=me.audit_meta(**changed),
            ip=client_ip(request),
        )
        await db.commit()
    return _media_settings(workspace)


# ---------------------------------------------------------------------------
# project settings
# ---------------------------------------------------------------------------


@router.patch(
    "/projects/{project_id}/settings/media",
    response_model=ProjectMediaSettings,
    summary="Set a project's default media models, reference permission and caps",
)
async def patch_project_media_settings(
    project_id: uuid.UUID,
    me: SettingsWriter,
    db: Db,
    request: Request,
    body: ProjectMediaSettingsPatch,
    catalogue: Catalogue,
) -> ProjectMediaSettings:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")
    settings = dict(project.settings or {})
    changed: dict[str, Any] = {}
    if body.media_models is not None:
        media = dict(settings.get(inputs.MEDIA_MODELS_SETTING) or {})
        for modality, chosen in body.media_models.items():
            if chosen is None:
                media.pop(modality, None)
                continue
            selection = MediaModelSelection(
                modality=modality,
                model_id=chosen.model_id,
                provider_tag=chosen.provider_tag,
                defaults=chosen.defaults,
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
            media[modality] = {
                "model_id": choice.model_id,
                "provider_tag": choice.provider_tag,
                "defaults": choice.defaults,
            }
        settings[inputs.MEDIA_MODELS_SETTING] = media
        changed["media_models"] = {k: v["model_id"] for k, v in media.items()}
    if body.media_references_allowed is not None:
        settings["media_references_allowed"] = body.media_references_allowed
        changed["media_references_allowed"] = body.media_references_allowed
    for key in ("max_creative_cost_usd", "max_media_cost_usd"):
        value = getattr(body, key)
        if value is not None:
            settings[key] = value
            changed[key] = value
    if changed:
        project.settings = settings
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.PROJECT_UPDATED,
            target_type=AuditTarget.PROJECT,
            target_id=project.id,
            meta=me.audit_meta(**changed),
            ip=client_ip(request),
        )
        await db.commit()
    return await _project_media_settings(db, project, me.workspace_id)


# ---------------------------------------------------------------------------
# the estimate
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/creative/estimate",
    response_model=EstimateResponse,
    summary="Price a creative scope before it runs. No spend, no job rows",
)
async def creative_estimate(
    project_id: uuid.UUID, me: AnyMember, db: Db, body: EstimateRequest, catalogue: Catalogue
) -> EstimateResponse:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")
    # Law 36 first: an unsupported request is refused before anything is read
    # or priced.
    try:
        choices = await inputs.resolve_media_models(
            db, me.workspace_id, body.scope, body.media_models, catalogue=catalogue
        )
    except CreativeInputError as refused:
        raise _unprocessable(refused) from refused
    plan_row = await inputs.frozen_plan(db, me.workspace_id, project_id)
    pin = await inputs.published_pin(db, me.workspace_id, project_id)
    if plan_row is None or pin is None:
        raise problems.conflict(
            "An estimate needs a frozen plan and published guidelines to price against.",
            title="Nothing to estimate yet",
            code="no_frozen_plan" if plan_row is None else "no_published_ruleset",
        )
    plan = PlanContract.model_validate(plan_row.payload)
    workspace = await db.get(Workspace, me.workspace_id)
    caps = resolve_media_caps(
        project_settings=project.settings,
        workspace_settings=workspace.settings if workspace else None,
        defaults=get_settings(),
    )
    # The project's own constants: the run it prices will execute under them.
    constants = creative_constants_for(project).media_constants()
    priced = inputs.estimate_inputs(plan, pin, body.scope, choices, caps)
    try:
        estimate = cost_estimate_v1(**priced, constants=constants)
        smaller = scope_reduction(**priced, constants=constants)
    except ValueError as unpriced:  # CalcError
        raise problems.unprocessable(
            str(unpriced), title="This scope cannot be priced", code="estimate_unavailable"
        ) from unpriced
    ratios = ratio_plan_v1(
        **inputs.ratio_inputs(plan, pin, body.scope, choices), constants=constants
    )
    writer = DerivedWriter(
        db,
        store=EvidenceStore(db, me.workspace_id),
        project_id=project_id,
        plan_run_id=plan_row.plan_run_id,
    )
    estimate_id = await writer.record(estimate, node_id=ESTIMATE_NODE)
    ratios_id = await writer.record(ratios, node_id=ESTIMATE_NODE)
    await db.commit()
    result = estimate.result
    return EstimateResponse(
        text_usd=result["text_usd"],
        image_usd=result["image_usd"],
        video_usd=result["video_usd"],
        total_usd=result["total_usd"],
        confidence=result["confidence"],
        calc_evidence_id=estimate_id,
        jobs=result["jobs"],
        fits=result["fits"],
        caps=result["caps"],
        reduction=result["reduction"],
        scope_reduction=(
            None
            if smaller is None
            else ScopeReduction(
                **{key: value for key, value in smaller.items() if key != "scope"},
                # The walk prices the flags; the campaigns are the request's own.
                scope=CreativeScope(campaign_refs=body.scope.campaign_refs, **smaller["scope"]),
            )
        ),
        ratio_plan=ratios.result,
        ratio_plan_evidence_id=ratios_id,
    )


# ---------------------------------------------------------------------------
# media references (PRD §10.3) — attested on upload, retired, never deleted
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/media-references",
    response_model=list[MediaReferenceOut],
    summary="The project's product and style references, retired ones included",
)
async def list_media_references(
    project_id: uuid.UUID, me: AnyMember, db: Db
) -> list[MediaReferenceOut]:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")
    rows = (
        (
            await db.execute(
                sa.select(MediaReference)
                .where(
                    MediaReference.workspace_id == me.workspace_id,
                    MediaReference.project_id == project.id,
                )
                .order_by(MediaReference.created_at, MediaReference.id)
            )
        )
        .scalars()
        .all()
    )
    return [MediaReferenceOut.model_validate(row) for row in rows]


@router.post(
    "/projects/{project_id}/media-references",
    response_model=MediaReferenceOut,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a product or style reference, attesting the rights to it",
)
async def upload_media_reference(
    project_id: uuid.UUID,
    me: CreativeOperator,
    db: Db,
    request: Request,
    file: Annotated[UploadFile, File()],
    kind: Annotated[Literal["product_reference", "style_reference"], Form()],
    origin: Annotated[Literal["own", "licensed", "third_party"], Form()],
    rights_statement: Annotated[str, Form()],
    product_ref: Annotated[str | None, Form()] = None,
) -> MediaReferenceOut:
    """The upload is the attestation: the uploader is `attested_by`.

    Nothing is written unless everything holds — a rights statement, bytes that
    decode as PNG/JPEG/WebP within `media.reference_max_bytes`, and a worker
    that stored them on the Volume. Whether the reference may ever reach a
    provider is not decided here; Law 44 decides that each time it would be
    sent (`media/references.py`).
    """
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")
    statement = rights_statement.strip()
    if not statement:
        raise problems.unprocessable(
            "A rights statement is required: say who owns this image and on what basis it "
            "may be used in advertising.",
            title="Rights statement required",
        )
    max_bytes = creative_constants_for(project).media_constants().reference_max_bytes
    # One byte past the cap is enough to know it is over; never buffer more.
    content = await file.read(max_bytes + 1)
    try:
        upload = references.inspect_upload(content, max_bytes=max_bytes)
    except references.ReferenceRejected as refused:
        if refused.code == "too_large":
            raise problems.Problem(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                title="File too large",
                detail=str(refused),
                type_=problems.TYPE_VALIDATION,
            ) from refused
        raise problems.unprocessable(str(refused), title="Not a usable image") from refused

    existing = (
        await db.execute(
            sa.select(MediaReference).where(
                MediaReference.project_id == project.id, MediaReference.sha256 == upload.sha256
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        retired = (
            f", retired {existing.retired_at:%Y-%m-%d}" if existing.retired_at is not None else ""
        )
        raise problems.conflict(
            f"This image is already reference {existing.id}{retired}. The same file is never "
            "registered twice, so its attestation stays the one on record.",
            title="Already uploaded",
        )

    key = references.storage_key(project.id, upload.sha256, upload.media_type)
    try:
        await queue.store_reference(
            {
                "key": key,
                "content": upload.content,
                "content_type": upload.media_type,
                "sha256": upload.sha256,
            }
        )
    except queue.WorkerUnavailable as exc:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="File not stored",
            detail="The worker that owns file storage did not store this image, so nothing "
            "was saved. Try again in a minute.",
        ) from exc

    row = MediaReference(
        workspace_id=me.workspace_id,
        project_id=project.id,
        kind=MediaReferenceKind(kind),
        storage_path=key,
        media_type=upload.media_type,
        width=upload.width,
        height=upload.height,
        bytes=upload.size,
        sha256=upload.sha256,
        product_ref=(product_ref or "").strip() or None,
        origin=MediaReferenceOrigin(origin),
        rights_statement=statement,
        attested_by=me.user.id,
        attested_at=datetime.now(UTC),
    )
    db.add(row)
    await db.flush()
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.MEDIA_REFERENCE_UPLOADED,
        target_type=AuditTarget.MEDIA_REFERENCE,
        target_id=row.id,
        meta=me.audit_meta(
            project_id=str(project.id),
            kind=kind,
            origin=origin,
            sha256=upload.sha256,
            bytes=upload.size,
        ),
        ip=client_ip(request),
    )
    try:
        await db.commit()
    except IntegrityError as exc:
        # `uq_media_reference_project_sha256`: the same bytes, uploaded by
        # someone else between the check above and this commit.
        await db.rollback()
        raise problems.conflict(
            "This image was registered by someone else a moment ago; refresh to see it.",
            title="Already uploaded",
        ) from exc
    await db.refresh(row)
    return MediaReferenceOut.model_validate(row)


@router.post(
    "/media-references/{reference_id}/retire",
    response_model=MediaReferenceOut,
    summary="Retire a reference so nothing new uses it. References are never deleted",
)
async def retire_media_reference(
    reference_id: uuid.UUID, me: CreativeOperator, db: Db, request: Request
) -> MediaReferenceOut:
    """Idempotent: retiring a retired reference returns it unchanged.

    Never deleted, so the provenance of every asset made from it stays
    resolvable (§10.3, §13 `reference_sha256s[]`).
    """
    row = (
        await db.execute(
            sa.select(MediaReference)
            .where(
                MediaReference.id == reference_id, MediaReference.workspace_id == me.workspace_id
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise problems.not_found(f"No media reference {reference_id}.")
    if row.retired_at is None:
        row.retired_at = datetime.now(UTC)
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.MEDIA_REFERENCE_RETIRED,
            target_type=AuditTarget.MEDIA_REFERENCE,
            target_id=row.id,
            meta=me.audit_meta(project_id=str(row.project_id), sha256=row.sha256),
            ip=client_ip(request),
        )
        await db.commit()
        await db.refresh(row)
    return MediaReferenceOut.model_validate(row)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _media_settings(workspace: Workspace | None) -> MediaSettings:
    raw = (workspace.settings or {}) if workspace else {}
    return MediaSettings(
        media_allowlist=MediaAllowlist.model_validate(
            raw.get(inputs.MEDIA_ALLOWLIST_SETTING) or {}
        ),
        media_defaults=MediaDefaults.model_validate(raw.get(inputs.MEDIA_DEFAULTS_SETTING) or {}),
    )


async def _project_media_settings(
    db: AsyncSession, project: Project, workspace_id: uuid.UUID
) -> ProjectMediaSettings:
    workspace = await db.get(Workspace, workspace_id)
    caps = resolve_media_caps(
        project_settings=project.settings,
        workspace_settings=workspace.settings if workspace else None,
        defaults=get_settings(),
    )
    settings = project.settings or {}
    return ProjectMediaSettings(
        media_models=dict(settings.get(inputs.MEDIA_MODELS_SETTING) or {}),
        media_references_allowed=bool(settings.get("media_references_allowed", False)),
        max_creative_cost_usd=str(caps.max_creative_cost_usd),
        max_media_cost_usd=str(caps.max_media_cost_usd),
    )


async def _project_ratios(
    db: AsyncSession, me: Principal, project_id: uuid.UUID
) -> tuple[list[str], list[str]]:
    if await ProjectRepo(db, me.workspace_id).get(project_id) is None:
        raise problems.not_found(f"No project {project_id}.")
    plan_row = await inputs.frozen_plan(db, me.workspace_id, project_id)
    pin = await inputs.published_pin(db, me.workspace_id, project_id)
    if plan_row is None or pin is None:
        return [], []
    plan = PlanContract.model_validate(plan_row.payload)
    image: list[str] = []
    video: list[str] = []
    for campaign in plan.account_structure.campaigns:
        wanted_image, wanted_video = inputs.required_ratios(pin, campaign.type)
        image += [r for r in wanted_image if r not in image]
        video += [r for r in wanted_video if r not in video]
    return image, video


def _unprocessable(refused: CreativeInputError) -> problems.Problem:
    extra: dict[str, Any] = {"code": refused.code, **refused.extra}
    return problems.unprocessable(refused.detail, title="Cannot use this media model", **extra)


def _catalogue_down(unavailable: CatalogueUnavailable) -> problems.Problem:
    return problems.Problem(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        title="Media catalogue unavailable",
        detail=str(unavailable),
        code="catalogue_unavailable",
    )
