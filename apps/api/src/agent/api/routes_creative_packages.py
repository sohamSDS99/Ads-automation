"""Package, release and the Stage 05 contract (Stage 04 PRD §16, §12.3, §12.4).

* `POST /creative-packages/{id}/release` — `CREATIVE_RELEASE`. One
  transaction (`creative/release.py`); guarded by `UPDATE … WHERE
  status='ready_to_release'`, so the loser of two concurrent releases gets
  `409`. Offer drift, a claim that expired, or `plan_superseded` at `now` is
  a `409` naming each offending asset (contract rule 5).
* `GET /packages/released?project_id=&pin=` — **the Stage 05 contract**.
  `404` when nothing is released, which Stage 05 reads as "nothing to load",
  never "load the draft"; `pin=` returns that exact version even once it is
  superseded (contract rule 6). The payload is served as released, with the
  row's current status — outside `package_hash`, so the hash still verifies.
* `GET /creative-packages/{id}/diff?against={id}` — `package_diff.diff`.
* `POST /creative-packages/{id}/export?format=json` — `202 {job_id}`; the
  worker renders `export/package_json.py` into `exports/{creative_run_id}/`.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Request, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent import queue
from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_creative_packages import ReleaseRequest, ReleaseResponse
from agent.api.schemas_report import ExportAccepted, ExportJob
from agent.api.throttle import throttle
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.ratelimit import EXPORT_QUOTA
from agent.auth.rbac import Permission
from agent.creative import release
from agent.creative.package_diff import PackageDiff, diff
from agent.db.models import CreativePackage as CreativePackageRow
from agent.db.models import CreativePackageStatus, ExportArtifactType, ExportFormat
from agent.db.repos import ExportRepo, ProjectRepo
from agent.db.session import get_session
from agent.export.jobs import CREATIVE_PACKAGE_FORMATS, package_filename_for
from agent.queue import enqueue_export
from agent.schemas.creative_package import CreativePackage

log = structlog.get_logger(__name__)

router = APIRouter(tags=["creative"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
#: `admin` and `approver` (PRD §5.1).
Releaser = Annotated[Principal, Depends(require(Permission.CREATIVE_RELEASE))]


@router.post("/creative-packages/{package_id}/release", response_model=ReleaseResponse)
async def release_package(
    package_id: uuid.UUID, body: ReleaseRequest, me: Releaser, db: Db, request: Request
) -> ReleaseResponse:
    try:
        released = await release.release_package(
            db,
            workspace_id=me.workspace_id,
            package_id=package_id,
            actor_id=me.user.id,
            confirm_version=body.confirm_version,
            ip=client_ip(request),
        )
    except release.PackageNotFound as exc:
        raise problems.not_found(str(exc)) from exc
    except release.ReleaseRefused as exc:
        raise problems.conflict(
            exc.detail, title="Release refused", code=exc.code, **exc.extra
        ) from exc
    except queue.WorkerUnavailable as exc:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Package files not written",
            detail="The worker that owns file storage did not write this package's files, so "
            "nothing was released. Try again in a minute.",
        ) from exc
    row = released.row
    assert row.package_hash is not None and row.released_at is not None  # noqa: S101
    return ReleaseResponse(
        package_id=row.id,
        creative_run_id=row.creative_run_id,
        version=row.version,
        status=row.status.value,
        package_hash=row.package_hash,
        released_at=row.released_at,
        released_by=row.released_by,
        superseded=released.superseded,
        files=len(released.package.manifest),
    )


# ---------------------------------------------------------------------------
# reading packages
# ---------------------------------------------------------------------------

#: What `pin=` may load: a version that was released, whether or not it still is.
PINNABLE = (CreativePackageStatus.RELEASED, CreativePackageStatus.SUPERSEDED)


def served(row: CreativePackageRow) -> CreativePackage:
    """The package as stored, with its row's current status (outside `package_hash`)."""
    return CreativePackage.model_validate({**row.payload, "status": row.status.value})


async def _package(db: AsyncSession, me: Principal, package_id: uuid.UUID) -> CreativePackageRow:
    row = await db.scalar(
        sa.select(CreativePackageRow).where(
            CreativePackageRow.id == package_id,
            CreativePackageRow.workspace_id == me.workspace_id,
        )
    )
    if row is None:
        raise problems.not_found(f"No creative package {package_id}.")
    return row


@router.get("/packages/released", response_model=CreativePackage)
async def released_package(
    me: AnyMember,
    db: Db,
    project_id: Annotated[uuid.UUID, Query()],
    pin: Annotated[int | None, Query(ge=1, description="An exact released version.")] = None,
) -> CreativePackage:
    """**The Stage 05 contract.** The released package, or `pin=`'s exact version."""
    statement = sa.select(CreativePackageRow).where(
        CreativePackageRow.workspace_id == me.workspace_id,
        CreativePackageRow.project_id == project_id,
    )
    if pin is None:
        statement = statement.where(CreativePackageRow.status == CreativePackageStatus.RELEASED)
    else:
        statement = statement.where(
            CreativePackageRow.version == pin, CreativePackageRow.status.in_(PINNABLE)
        )
    row = (
        (await db.execute(statement.order_by(CreativePackageRow.version.desc()))).scalars().first()
    )
    if row is None:
        raise problems.not_found(
            f"Nothing released to load for project {project_id}"
            + (f" at version {pin}" if pin is not None else "")
            + ". A draft is never served here.",
            title="Nothing released",
        )
    return served(row)


@router.get("/creative-packages/{package_id}/diff", response_model=PackageDiff)
async def package_diff(
    package_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    against: Annotated[uuid.UUID, Query(description="The package to compare with.")],
) -> PackageDiff:
    row = await _package(db, me, package_id)
    other = await _package(db, me, against)
    if other.project_id != row.project_id:
        raise problems.not_found(f"No creative package {against} in this package's project.")
    return diff(served(row), served(other))


@router.post(
    "/creative-packages/{package_id}/export",
    response_model=ExportAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(throttle(EXPORT_QUOTA))],
)
async def request_package_export(
    package_id: uuid.UUID,
    me: AnyMember,
    request: Request,
    db: Db,
    export_format: Annotated[
        ExportFormat, Query(alias="format", description="json | editor_zip | pdf | xlsx | md")
    ],
) -> ExportAccepted:
    """Queue one package export. `READ`, as every Stage 01–03 export route is: the
    write-shaped verb is where the work happens, not a privilege (§16)."""
    if export_format not in CREATIVE_PACKAGE_FORMATS:
        raise problems.unprocessable(
            f"A creative package cannot be exported as {export_format.value} yet. Choose one "
            f"of: {', '.join(sorted(item.value for item in CREATIVE_PACKAGE_FORMATS))}.",
            title="Unsupported export format",
        )
    row = await _package(db, me, package_id)
    project = await ProjectRepo(db, me.workspace_id).get(row.project_id)
    export = ExportRepo(db, me.workspace_id).add(
        row.id,
        export_format,
        artifact_type=ExportArtifactType.CREATIVE_PACKAGE,
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
            "package_id": str(row.id),
            "creative_run_id": str(row.creative_run_id),
            "format": export_format.value,
            "package_status": row.status.value,
            "version": row.version,
        },
        ip=client_ip(request),
    )
    # Committed before the job is queued: the worker looks the row up by id.
    await db.commit()
    await enqueue_export(export.id)
    return ExportAccepted(
        job_id=export.id,
        export=ExportJob(
            id=export.id,
            report_id=export.artifact_id,
            run_id=row.creative_run_id,
            format=export.format,
            status=export.status,
            bytes=export.bytes,
            filename=package_filename_for(
                export.format,
                project_name=project.name if project else None,
                version=row.version,
                generated_at=export.created_at,
            ),
            error=export.error,
            created_at=export.created_at,
            ready_at=export.ready_at,
        ),
    )
