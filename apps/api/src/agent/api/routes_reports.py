"""The report and export API (PRD §14).

    GET  /reports/{run_id}
    POST /reports/{run_id}/export?format=pdf|docx|md|json|csv   → 202 {job_id}
    GET  /exports/{job_id}
    GET  /exports/{job_id}/download

Every route here is guarded by `Permission.READ`, and that is not an oversight.
PRD §4.1 gives "Read reports, evidence, run history" and "Export PDF/DOCX/CSV/
JSON" to all four roles — a `viewer` may export. The write-shaped verb on the
export route is about where the work happens, not about privilege: it creates a
job, so it cannot be a GET, but it grants nothing a GET would not.

The download is the one route that leaves this process. `api` cannot read the
Volume (PRD §5.2 — it attaches to `worker`), so it mints a signed capability for
that one storage key, fetches the object from the worker's internal file server,
and relays the bytes. It never buffers the file: a 15 MB PDF through an API
process is fine once and a memory problem at ten concurrent downloads.
"""

from __future__ import annotations

import uuid
from typing import Annotated
from urllib.parse import quote

import httpx
import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from agent.api import problems
from agent.api.schemas_report import ExportAccepted, ExportJob, ReportResponse
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import Settings, get_settings
from agent.db.models import Export, ExportFormat, ExportStatus, Project, Report, Run
from agent.db.repos import ExportRepo, ProjectRepo, ReportRepo, RunRepo
from agent.db.session import get_session
from agent.export.contract import ResearchReport
from agent.export.jobs import MEDIA_TYPES, filename_for
from agent.export.tokens import sign
from agent.queue import enqueue_export

log = structlog.get_logger(__name__)

router = APIRouter(tags=["reports"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]

#: How long `api` waits on the worker for a download. Generous, because the hop
#: is a file stream over a private network and the worker may be mid-render on
#: another job; short enough that a wedged worker does not hold the connection
#: open indefinitely.
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)


_worker_client: httpx.AsyncClient | None = None


async def get_worker_client() -> httpx.AsyncClient:
    """The client `api` uses to reach the worker's file server.

    One client for the process, opened on first use and closed in the app
    lifespan — the same shape as the arq pool and the Redis client, and for the
    same reason: connection reuse to one known host.

    It is deliberately **not** a `yield` dependency. FastAPI finalises those once
    the response is handed off, which for a StreamingResponse is before the body
    has finished streaming; the client would be closed out from under the
    download. Its lifetime belongs to the process, not the request.

    It is a dependency at all so the integration suite can override it with a
    client bound to the file-server ASGI app, and exercise the real token check
    without a second process listening on a port.
    """
    global _worker_client
    if _worker_client is None:
        _worker_client = httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT)
    return _worker_client


async def close_worker_client() -> None:
    global _worker_client
    if _worker_client is not None:
        await _worker_client.aclose()
        _worker_client = None


WorkerClient = Annotated[httpx.AsyncClient, Depends(get_worker_client)]


# ---------------------------------------------------------------------------
# reading a report
# ---------------------------------------------------------------------------


def _to_job(export: Export, run: Run, *, project_name: str | None) -> ExportJob:
    """One export row as the API describes it.

    `filename` is recomputed rather than parsed back out of `path`: the path is
    a storage key and belongs to the worker, and a client that builds a
    download filename from it would be depending on the layout of the Volume.
    """
    generated_at = export.created_at
    return ExportJob(
        id=export.id,
        report_id=export.report_id,
        run_id=run.id,
        format=export.format,
        status=export.status,
        bytes=export.bytes,
        filename=filename_for(export.format, project_name=project_name, generated_at=generated_at),
        error=export.error,
        created_at=export.created_at,
        ready_at=export.ready_at,
    )


async def _load_report(db: AsyncSession, me: Principal, run_id: uuid.UUID) -> tuple[Report, Run]:
    """The report for a run, or a 404 that distinguishes the two ways it can be missing."""
    run = await RunRepo(db, me.workspace_id).get(run_id)
    if run is None:
        raise problems.not_found(f"No run {run_id}.")
    report = await ReportRepo(db, me.workspace_id).for_run(run_id)
    if report is None:
        # A run that has not reached 1.6.1 has no report yet, and saying so is
        # more useful than "not found" to anyone watching a run in flight.
        raise problems.not_found(
            f"Run {run_id} has not produced a report. Its status is {run.status.value}.",
            title="No report yet",
        )
    return report, run


@router.get(
    "/reports/{run_id}",
    response_model=ReportResponse,
    summary="The research report for a run",
)
async def get_report(run_id: uuid.UUID, me: AnyMember, db: Db) -> ReportResponse:
    report, run = await _load_report(db, me, run_id)
    project = await ProjectRepo(db, me.workspace_id).get(run.project_id)
    exports = await ExportRepo(db, me.workspace_id).for_report(report.id)

    return ReportResponse(
        id=report.id,
        run_id=report.run_id,
        project_id=run.project_id,
        schema_version=report.schema_version,
        created_at=report.created_at,
        # Validated on the way out, not trusted. A payload written by an earlier
        # contract version surfaces here as a 500 with a schema error rather
        # than as a half-rendered report in somebody's browser.
        payload=ResearchReport.model_validate(report.payload),
        markdown=report.markdown,
        exports=[
            _to_job(export, run, project_name=project.name if project else None)
            for export in exports
        ],
    )


# ---------------------------------------------------------------------------
# requesting an export
# ---------------------------------------------------------------------------


@router.post(
    "/reports/{run_id}/export",
    response_model=ExportAccepted,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Generate an export of a report",
)
async def request_export(
    run_id: uuid.UUID,
    me: AnyMember,
    request: Request,
    db: Db,
    export_format: Annotated[
        ExportFormat,
        Query(alias="format", description="pdf | docx | md | json | csv"),
    ],
) -> ExportAccepted:
    report, run = await _load_report(db, me, run_id)
    project = await ProjectRepo(db, me.workspace_id).get(run.project_id)

    export = ExportRepo(db, me.workspace_id).add(report.id, export_format, requested_by=me.user.id)
    await db.flush()

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.EXPORT_REQUESTED,
        target_type=AuditTarget.EXPORT,
        target_id=export.id,
        meta={"run_id": str(run.id), "format": export_format.value},
        ip=request.client.host if request.client else None,
    )
    # Committed before the job is queued. The worker looks this row up by id, so
    # enqueueing first is a race it can lose — and losing it means an export
    # that failed because the row it describes did not exist yet.
    await db.commit()

    await enqueue_export(export.id)

    job = _to_job(export, run, project_name=project.name if project else None)
    return ExportAccepted(job_id=export.id, export=job)


@router.get(
    "/exports/{export_id}",
    response_model=ExportJob,
    summary="Export job status",
)
async def get_export(export_id: uuid.UUID, me: AnyMember, db: Db) -> ExportJob:
    repo = ExportRepo(db, me.workspace_id)
    export = await repo.get(export_id)
    run = await repo.run_for(export_id) if export else None
    if export is None or run is None:
        raise problems.not_found(f"No export {export_id}.")
    project = await ProjectRepo(db, me.workspace_id).get(run.project_id)
    return _to_job(export, run, project_name=project.name if project else None)


# ---------------------------------------------------------------------------
# downloading
# ---------------------------------------------------------------------------


def _content_disposition(filename: str) -> str:
    """RFC 6266, both spellings.

    The ASCII `filename` is what old clients read; `filename*` carries the real
    one. Project names reach this string, so the quoted form is also stripped of
    the two characters that could terminate it early.
    """
    ascii_name = (
        filename.encode("ascii", "ignore").decode("ascii").replace('"', "").replace("\\", "")
    )
    return f"attachment; filename=\"{ascii_name or 'export'}\"; filename*=UTF-8''{quote(filename)}"


async def _open_upstream(
    client: httpx.AsyncClient, url: str, *, export_id: uuid.UUID
) -> httpx.Response:
    """Begin the worker fetch and prove it succeeded before we answer the caller.

    The status is checked here, with nothing yet written to the client, so a
    worker that cannot serve the object produces a clean 502. Checking it inside
    the streaming generator instead would mean the 200 and its headers had
    already gone out, and the only remaining signal would be an aborted
    connection — which most clients save to disk as a truncated file.
    """
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as exc:
        log.error("export.download_unreachable", export_id=str(export_id), error=str(exc))
        raise problems.Problem(
            status_code=status.HTTP_502_BAD_GATEWAY,
            title="Export could not be read",
            detail="The worker that stores this export is unreachable.",
        ) from exc

    if upstream.status_code != httpx.codes.OK:
        await upstream.aread()
        await upstream.aclose()
        log.error(
            "export.download_upstream_failed",
            export_id=str(export_id),
            status=upstream.status_code,
        )
        raise problems.Problem(
            status_code=status.HTTP_502_BAD_GATEWAY,
            title="Export could not be read",
            detail="The worker did not return the export file.",
        )
    return upstream


@router.get(
    "/exports/{export_id}/download",
    summary="Download a generated export",
    response_class=StreamingResponse,
)
async def download_export(
    export_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    client: WorkerClient,
    settings: Annotated[Settings, Depends(get_settings)],
) -> Response:
    repo = ExportRepo(db, me.workspace_id)
    export = await repo.get(export_id)
    run = await repo.run_for(export_id) if export else None
    if export is None or run is None:
        raise problems.not_found(f"No export {export_id}.")

    if export.status is ExportStatus.FAILED:
        raise problems.unprocessable(
            export.error or "This export failed to generate.",
            title="Export failed",
        )
    if not export.is_downloadable or not export.path:
        raise problems.conflict(
            f"This export is {export.status.value}. Poll GET /exports/{export_id} "
            "until it is ready.",
            title="Export is not ready",
            status_value=export.status.value,
        )

    project: Project | None = await ProjectRepo(db, me.workspace_id).get(run.project_id)
    filename = filename_for(
        export.format,
        project_name=project.name if project else None,
        generated_at=export.created_at,
    )

    token = sign(export.path, settings=settings)
    url = f"{settings.worker_internal_url.rstrip('/')}/files/{quote(export.path)}?token={token}"

    upstream = await _open_upstream(client, url, export_id=export_id)

    headers = {
        "Content-Disposition": _content_disposition(filename),
        "Cache-Control": "private, no-store",
    }
    # Only advertise a length we actually know. A wrong Content-Length is worse
    # than none: the browser truncates or hangs waiting for bytes that are not
    # coming.
    if export.bytes:
        headers["Content-Length"] = str(export.bytes)

    log.info("export.download", export_id=str(export_id), user_id=str(me.user.id))
    return StreamingResponse(
        upstream.aiter_bytes(),
        media_type=MEDIA_TYPES[export.format],
        headers=headers,
        # The upstream response outlives this function, so closing it is the
        # response's job, not a context manager's.
        background=BackgroundTask(upstream.aclose),
    )
