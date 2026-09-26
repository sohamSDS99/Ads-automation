"""Package, release and the Stage 05 contract (Stage 04 PRD §16, §12.3, §12.4).

* `POST /creative-packages/{id}/release` — `CREATIVE_RELEASE`. One
  transaction (`creative/release.py`); guarded by `UPDATE … WHERE
  status='ready_to_release'`, so the loser of two concurrent releases gets
  `409`. Offer drift, a claim that expired, or `plan_superseded` at `now` is
  a `409` naming each offending asset (contract rule 5).
* `GET /creative-runs/{id}/package` and `GET /creative-packages/{id}` — one
  package with 4.7.2's critique, the thirteen checks as it ran them and what
  a release would do (`PackageView`): the package screen and the canonical
  released page render these and re-derive none of them (S4-P23).
* `GET /creative-packages/{id}/files/{path}` — one manifest file of a
  released package, as a signed `302` to the file server.
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
from collections.abc import Mapping, Sequence
from typing import Annotated
from urllib.parse import quote

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent import queue
from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.routes_media_library import (
    CONTENT_TTL_SECONDS,
    FILES_PATH,
    REDIRECT_MAX_AGE_SECONDS,
)
from agent.api.schemas_creative_packages import (
    ChecklistItem,
    Gate,
    PackageView,
    ReleasePreview,
    ReleaseRequest,
    ReleaseResponse,
    ReleaseStop,
)
from agent.api.schemas_report import ExportAccepted, ExportJob
from agent.api.throttle import throttle
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.ratelimit import EXPORT_QUOTA
from agent.auth.rbac import Permission
from agent.creative import checklist, release
from agent.creative.package import package_row
from agent.creative.package_diff import PackageDiff, diff
from agent.db.models import CreativePackage as CreativePackageRow
from agent.db.models import (
    CreativePackageStatus,
    ExportArtifactType,
    ExportFormat,
    NodeRun,
    NodeRunStatus,
    Run,
    RunStage,
)
from agent.db.repos import ExportRepo, ProjectRepo, UserRepo
from agent.db.session import get_session
from agent.export.jobs import CREATIVE_PACKAGE_FORMATS, package_filename_for
from agent.export.tokens import sign
from agent.queue import enqueue_export
from agent.schemas.creative_package import (
    CreativeCritique,
    CreativePackage,
    ExceptionRef,
    GateDecision,
)

#: The statuses whose package release minted: the only ones `editor_zip` serves.
RELEASED = frozenset({CreativePackageStatus.RELEASED, CreativePackageStatus.SUPERSEDED})

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


@router.get(
    "/creative-runs/{run_id}/package",
    response_model=PackageView,
    summary="The run's package: 4.7.2's thirteen checks, its critique, and what release would do",
)
async def run_package(run_id: uuid.UUID, me: AnyMember, db: Db) -> PackageView:
    run = await db.scalar(
        sa.select(Run).where(Run.id == run_id, Run.workspace_id == me.workspace_id)
    )
    if run is None or run.stage is not RunStage.CREATIVE:
        raise problems.not_found(f"No creative run {run_id}.")
    row = await package_row(db, run.id)
    if row is None:
        raise problems.not_found(
            f"Run {run.id} has not assembled a package yet: node 4.7.1 assembles it once every "
            "asset is final, and 4.7.2 checks it. Look again when the run reaches Stage 4.7.",
            title="No package yet",
        )
    return await _view(db, me, row)


@router.get(
    "/creative-packages/{package_id}",
    response_model=PackageView,
    summary="One package by id — the canonical page of a released version",
)
async def get_package(package_id: uuid.UUID, me: AnyMember, db: Db) -> PackageView:
    return await _view(db, me, await _package(db, me, package_id))


@router.get(
    "/creative-packages/{package_id}/files/{path:path}",
    status_code=status.HTTP_302_FOUND,
    summary="Redirect to one manifest file of a released package, signed for 300 s",
    responses={302: {"description": "`Location` is the signed file-server URL."}},
)
async def package_file(package_id: uuid.UUID, path: str, me: AnyMember, db: Db) -> Response:
    """Contract rule 7: `api` never serves the bytes; the file server does.

    Only a released (or since superseded) package has files: release writes
    them under `package/{id}/` in its one transaction (§12.4), so a draft's
    manifest names files that do not exist yet. Only a path the manifest
    lists is signed — nothing else under `package/` is reachable from here.
    """
    row = await _package(db, me, package_id)
    if row.status not in PINNABLE:
        raise problems.not_found(
            f"Package {row.id} is {row.status.value.replace('_', ' ')}: its files are written "
            "when it is released. Open the file from the released version.",
            title="Not released yet",
        )
    if path not in {entry["path"] for entry in row.manifest or []}:
        raise problems.not_found(f"Package v{row.version} has no file {path!r} in its manifest.")
    key = f"package/{row.id}/{path}"
    token = sign(key, ttl_seconds=CONTENT_TTL_SECONDS)
    log.info("package_file.signed", package_id=str(row.id), path=path)
    return Response(
        status_code=status.HTTP_302_FOUND,
        headers={
            "Location": f"{FILES_PATH}/{quote(key)}?token={token}",
            "Cache-Control": f"private, max-age={REDIRECT_MAX_AGE_SECONDS}",
        },
    )


async def _view(db: AsyncSession, me: Principal, row: CreativePackageRow) -> PackageView:
    package = served(row)
    critique = await _critique(db, row)
    wanted = {
        *(d.decided_by for d in package.decisions),
        *(e.decided_by for e in package.exceptions),
        row.released_by,
    }
    names = await UserRepo(db, me.workspace_id).names({i for i in wanted if i is not None})
    return PackageView(
        package=package,
        row_version=row.version,
        package_hash=row.package_hash,
        released_at=row.released_at,
        released_by=row.released_by,
        released_by_name=names.get(row.released_by) if row.released_by else None,
        plan_superseded=row.plan_superseded,
        ruleset_superseded=row.ruleset_superseded,
        created_at=row.created_at,
        updated_at=row.updated_at,
        critique=critique,
        checklist=_checklist(critique) if critique is not None else [],
        release=await _release_preview(db, row, package, critique, names),
    )


async def _critique(db: AsyncSession, row: CreativePackageRow) -> CreativeCritique | None:
    """4.7.2's latest successful output for this package, or None if it has not run."""
    output = await db.scalar(
        sa.select(NodeRun.output)
        .where(
            NodeRun.run_id == row.creative_run_id,
            NodeRun.node_id == "4.7.2",
            NodeRun.status == NodeRunStatus.SUCCEEDED,
        )
        .order_by(NodeRun.attempt.desc())
        .limit(1)
    )
    if not output:
        return None
    found = CreativeCritique.model_validate(output)
    return found if found.package_id == row.id else None


def _checklist(critique: CreativeCritique) -> list[ChecklistItem]:
    """The thirteen, in §11's order, each with the findings 4.7.2 recorded for it."""
    items: list[ChecklistItem] = []
    for check, title in checklist.TITLES.items():
        issues = [i for i in critique.issues if i.check == check and i.severity == "blocking"]
        items.append(ChecklistItem(check=check, title=title, passed=not issues, issues=issues))
    return items


#: The gates a package records a decision for, in the order they stop a run.
GATES: tuple[Gate, ...] = ("G7", "G8", "G8b")


async def _release_preview(
    db: AsyncSession,
    row: CreativePackageRow,
    package: CreativePackage,
    critique: CreativeCritique | None,
    names: Mapping[uuid.UUID, str],
) -> ReleasePreview:
    stops = [_gate_stop(gate, package.decisions, names) for gate in GATES]
    stops.append(_h3_stop(package, names))
    if row.status in PINNABLE:
        return ReleasePreview(
            releasable=False,
            version_to_mint=None,
            reason=(
                f"Released as v{row.version}. A released package is immutable."
                if row.status is CreativePackageStatus.RELEASED
                else f"Released as v{row.version}, since superseded by a later version. "
                "A released package is immutable."
            ),
            stops=stops,
        )
    reason: str | None = None
    if row.status is CreativePackageStatus.BLOCKED:
        blocking = critique.blocking if critique is not None else 0
        reason = (
            f"4.7.2 found {blocking} blocking {'issue' if blocking == 1 else 'issues'}. Fix "
            f"{'it' if blocking == 1 else 'them'}, then re-run 4.7 to assemble and check the "
            "package again."
        )
    elif row.status is CreativePackageStatus.DRAFT:
        reason = "4.7.2 has not checked this package yet. It becomes releasable once it passes."
    return ReleasePreview(
        releasable=row.status is CreativePackageStatus.READY_TO_RELEASE,
        version_to_mint=await release.next_version(db, row.project_id),
        reason=reason,
        stops=stops,
    )


def _gate_stop(
    gate: Gate, decisions: Sequence[GateDecision], names: Mapping[uuid.UUID, str]
) -> ReleaseStop:
    """The gate's last recorded decision; `not_required` when it never opened."""
    found = [d for d in decisions if d.gate_key == gate]
    if not found:
        return ReleaseStop(
            gate=gate,
            status="not_required",
            detail="Never opened: this package ships no AI media."
            if gate != "G7"
            else "Never opened.",
        )
    last = found[-1]
    return ReleaseStop(
        gate=gate,
        status=last.status,
        decided_by=last.decided_by,
        decided_by_name=names.get(last.decided_by) if last.decided_by else None,
        decided_at=last.decided_at,
        detail=last.note,
    )


def _h3_stop(package: CreativePackage, names: Mapping[uuid.UUID, str]) -> ReleaseStop:
    """H3 as 4.6.3 left it, decided by whoever cleared or rejected its exceptions."""
    task = package.human_tasks[-1] if package.human_tasks else None
    exceptions = package.exceptions
    decided = [
        e
        for _, e in sorted(
            ((e.decided_at, e) for e in exceptions if e.decided_at is not None),
            key=lambda pair: pair[0],
        )
    ]
    # Only the legal owner's clear or reject is an H3 decision; a withdrawal
    # licenses nothing and is counted in `detail`, not named as the decider.
    signed: list[ExceptionRef] = [e for e in decided if e.status in ("cleared", "rejected")]
    last = signed[-1] if signed else None
    counts = {
        state: sum(1 for e in exceptions if e.status == state)
        for state in ("cleared", "rejected", "withdrawn", "open")
    }
    detail = (
        f"{len(exceptions)} {'exception' if len(exceptions) == 1 else 'exceptions'}: "
        + ", ".join(f"{n} {state}" for state, n in counts.items() if n)
        if exceptions
        else "No exceptions to clear."
    )
    return ReleaseStop(
        gate="H3",
        status=task.status if task is not None else "not_required",
        decided_by=last.decided_by if last is not None else None,
        decided_by_name=names.get(last.decided_by) if last and last.decided_by else None,
        decided_at=last.decided_at if last is not None else None,
        detail=detail,
    )


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
    if export_format is ExportFormat.EDITOR_ZIP and row.status not in RELEASED:
        # §14: a draft that imports into Google Ads Editor is the most
        # dangerous artifact this stage could produce. Refused before a job.
        raise problems.conflict(
            f"Package {row.id} is {row.status.value.replace('_', ' ')}; only a released package "
            "can be exported for Google Ads Editor. Release it, then export again.",
            title="Package not released",
            code="package_not_released",
            status=row.status.value,
        )
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
