"""Export generation, as an arq job in the worker (PRD §12).

§12 is explicit that this does not happen on the request path: `POST
/reports/{id}/export` returns `202 {job_id}` and the file is produced here. Two
reasons, and the second is the one that forces it. A 200-creative PDF takes
tens of seconds, which is a gateway timeout waiting to happen. And the Volume
attaches to `worker` (§5.2) — the API process has no disk to write to.

The row is the job. `Export.id` is what the client polls and what it later
downloads, so there is one identifier for one thing, and the record survives a
Redis flush that would otherwise lose a finished export.

Failure is recorded, never swallowed: a job that dies leaves `status='failed'`
and a sentence a person can act on, because the alternative is an export that
sits at `queued` forever with the reason in a log nobody is reading.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CampaignPlan as CampaignPlanRow,
)
from agent.db.models import (
    Export,
    ExportArtifactType,
    ExportFormat,
    ExportStatus,
    Project,
    Report,
    Run,
    User,
)
from agent.db.session import get_sessionmaker
from agent.export.budget_xlsx import render_budget_xlsx
from agent.export.contract import ResearchReport
from agent.export.docx import render_docx
from agent.export.editor_csv import render_editor_csv
from agent.export.markdown import render_markdown
from agent.export.pdf import render_pdf
from agent.export.plan_contract import CampaignPlan
from agent.export.plan_docx import render_plan_docx
from agent.export.plan_markdown import render_plan_markdown
from agent.export.plan_pdf import render_plan_pdf
from agent.export.tabular import render_csv, render_json_from_payload
from agent.orchestrator.events import EventType, RunEventStream
from agent.redis_client import get_redis
from agent.storage.backend import StorageBackend, StorageError, get_storage

log = structlog.get_logger(__name__)

GENERATE_EXPORT = "generate_export"

#: Content types, by format. These reach the browser on the download hop, so
#: `text/markdown` rather than `text/plain`: a reader that knows the difference
#: renders it, and one that does not treats it as text either way.
MEDIA_TYPES: dict[ExportFormat, str] = {
    ExportFormat.PDF: "application/pdf",
    ExportFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ExportFormat.MD: "text/markdown; charset=utf-8",
    ExportFormat.JSON: "application/json; charset=utf-8",
    ExportFormat.CSV: "text/csv; charset=utf-8",
    ExportFormat.EDITOR_CSV: "text/csv; charset=utf-8",
    ExportFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

EXTENSIONS: dict[ExportFormat, str] = {
    ExportFormat.PDF: "pdf",
    ExportFormat.DOCX: "docx",
    ExportFormat.MD: "md",
    ExportFormat.JSON: "json",
    ExportFormat.CSV: "csv",
    ExportFormat.EDITOR_CSV: "csv",
    ExportFormat.XLSX: "xlsx",
}

#: The formats a *research report* can be rendered as. Not every member of
#: `ExportFormat`: `editor_csv` and `xlsx` are shapes of a campaign plan and
#: their renderers arrive in S2-P5. Enforced at the route rather than left to
#: fail in the worker, because a queued job that can never succeed is a worse
#: answer than a 422 that names the five formats that work.
RESEARCH_REPORT_FORMATS: frozenset[ExportFormat] = frozenset(
    {
        ExportFormat.PDF,
        ExportFormat.DOCX,
        ExportFormat.MD,
        ExportFormat.JSON,
        ExportFormat.CSV,
    }
)

#: The formats a *campaign plan* can be rendered as (Stage 02 PRD §14). Six,
#: and not the research report's `csv`: a plan's tabular deliverable is the
#: Editor bundle, and a single flat CSV of a campaign tree is a shape nothing
#: can import. Enforced at the route rather than left to fail in the worker.
CAMPAIGN_PLAN_FORMATS: frozenset[ExportFormat] = frozenset(
    {
        ExportFormat.PDF,
        ExportFormat.DOCX,
        ExportFormat.MD,
        ExportFormat.JSON,
        ExportFormat.EDITOR_CSV,
        ExportFormat.XLSX,
    }
)

#: Which formats each artifact supports, so a route can answer "not that one"
#: from one place. A queued job that can never succeed is a worse answer than
#: a 422 naming the formats that work.
FORMATS_FOR: dict[ExportArtifactType, frozenset[ExportFormat]] = {
    ExportArtifactType.RESEARCH_REPORT: RESEARCH_REPORT_FORMATS,
    ExportArtifactType.CAMPAIGN_PLAN: CAMPAIGN_PLAN_FORMATS,
}

_UNSAFE = re.compile(r"[^a-z0-9]+")


class ExportError(RuntimeError):
    """The export could not be produced. The message is shown to the user."""


@dataclass(frozen=True, slots=True)
class RenderedExport:
    """What a render produced, before it is stored."""

    filename: str
    media_type: str
    payload: bytes


def slugify(value: str, *, fallback: str = "report") -> str:
    """A filename-safe fragment. Project names arrive from users, so nothing is assumed."""
    slug = _UNSAFE.sub("-", value.strip().lower()).strip("-")
    return slug[:60] or fallback


def filename_for(fmt: ExportFormat, *, project_name: str | None, generated_at: datetime) -> str:
    """`paid-ads-research-northwind-safety-2026-03-04.pdf`.

    The date is the report's, not today's: re-downloading a March report in June
    should not produce a file called June.
    """
    parts = ["paid-ads-research"]
    if project_name:
        parts.append(slugify(project_name))
    parts.append(generated_at.date().isoformat())
    return "-".join(parts) + f".{EXTENSIONS[fmt]}"


def plan_filename_for(
    fmt: ExportFormat, *, project_name: str | None, version: int, generated_at: datetime
) -> str:
    """`campaign-plan-northwind-safety-v3-2026-09-22.xlsx`.

    The version is in the name because the whole point of a frozen plan is that
    there are several, and two files called `campaign-plan-acme.pdf` in one
    downloads folder is how the wrong one gets circulated. An unfrozen plan has
    no version yet and says `draft` instead — which is the same warning the
    document carries inside it.
    """
    parts = ["campaign-plan"]
    if project_name:
        parts.append(slugify(project_name, fallback="plan"))
    parts.append(f"v{version}" if version > 0 else "draft")
    parts.append(generated_at.date().isoformat())
    return "-".join(parts) + f".{EXTENSIONS[fmt]}"


#: The prefix every rendered export lives under. Named so the retention job
#: prunes the same tree this writes to rather than a string that looks like it.
EXPORT_PREFIX = "exports"


def storage_key(run_id: uuid.UUID, filename: str) -> str:
    """PRD §12: output lands at `$STORAGE_DIR/exports/{run_id}/`."""
    return f"{EXPORT_PREFIX}/{run_id}/{filename}"


def render(
    fmt: ExportFormat,
    report: ResearchReport,
    *,
    project_name: str | None,
    stored_markdown: str | None = None,
    stored_payload: dict[str, Any] | None = None,
    storage: StorageBackend | None = None,
) -> RenderedExport:
    """Produce one format's bytes.

    `stored_markdown` and `stored_payload` are preferred over re-rendering when
    they are available. The run wrote them; serving anything else would let a
    later template edit quietly change a report someone has already acted on.
    """
    filename = filename_for(fmt, project_name=project_name, generated_at=report.generated_at)

    if fmt is ExportFormat.MD:
        markdown = stored_markdown or render_markdown(report, project_name=project_name)
        payload = markdown.encode("utf-8")
    elif fmt is ExportFormat.JSON:
        payload = render_json_from_payload(
            stored_payload if stored_payload is not None else report.model_dump(mode="json")
        )
    elif fmt is ExportFormat.CSV:
        payload = render_csv(report)
    elif fmt is ExportFormat.DOCX:
        payload = render_docx(report, project_name=project_name)
    elif fmt is ExportFormat.PDF:
        payload = render_pdf(
            report,
            project_name=project_name,
            image_loader=_image_loader(storage),
        )
    else:  # pragma: no cover — ExportFormat is closed and every member is above
        raise ExportError(f"No renderer for format {fmt!r}.")

    return RenderedExport(filename=filename, media_type=MEDIA_TYPES[fmt], payload=payload)


def render_plan(
    fmt: ExportFormat,
    plan: CampaignPlan,
    *,
    project_name: str | None,
    stored_markdown: str | None = None,
    stored_payload: dict[str, Any] | None = None,
    frozen_by_name: str = "",
) -> RenderedExport:
    """Produce one plan format's bytes (Stage 02 PRD §14).

    `stored_markdown` and `stored_payload` are preferred over re-rendering for
    the same reason they are on the research side: the run wrote them, and
    serving anything else would let a later template edit quietly change a plan
    somebody has already signed. On a **frozen** plan that is not a preference
    but a guarantee — the database refuses to rewrite either column (law 17),
    so the stored bytes are the ones that were frozen.
    """
    filename = plan_filename_for(
        fmt, project_name=project_name, version=plan.version, generated_at=plan.generated_at
    )

    if fmt is ExportFormat.MD:
        markdown = stored_markdown or render_plan_markdown(
            plan, project_name=project_name, frozen_by_name=frozen_by_name
        )
        payload = markdown.encode("utf-8")
    elif fmt is ExportFormat.JSON:
        payload = render_json_from_payload(
            stored_payload if stored_payload is not None else plan.model_dump(mode="json")
        )
    elif fmt is ExportFormat.EDITOR_CSV:
        payload = render_editor_csv(plan, project_name=project_name)
    elif fmt is ExportFormat.XLSX:
        payload = render_budget_xlsx(plan, project_name=project_name)
    elif fmt is ExportFormat.DOCX:
        payload = render_plan_docx(plan, project_name=project_name, frozen_by_name=frozen_by_name)
    elif fmt is ExportFormat.PDF:
        payload = render_plan_pdf(plan, project_name=project_name, frozen_by_name=frozen_by_name)
    else:
        raise ExportError(
            f"A campaign plan cannot be exported as {fmt.value}. "
            f"Available: {', '.join(sorted(item.value for item in CAMPAIGN_PLAN_FORMATS))}."
        )

    return RenderedExport(filename=filename, media_type=MEDIA_TYPES[fmt], payload=payload)


def _image_loader(storage: StorageBackend | None) -> Any:
    """Adapt the storage backend to the PDF renderer's loader contract."""
    if storage is None:
        return None

    def load(key: str) -> bytes | None:
        try:
            return storage.get(key)
        except StorageError:
            return None

    return load


async def _finish(
    session: AsyncSession,
    export: Export,
    *,
    status: ExportStatus,
    path: str | None = None,
    size: int | None = None,
    error: str | None = None,
) -> None:
    export.status = status
    export.path = path
    export.bytes = size
    export.error = error
    export.ready_at = datetime.now(UTC) if status is ExportStatus.READY else None
    await session.commit()


async def generate_export(ctx: dict[str, Any], export_id: str) -> dict[str, Any]:
    """Render one export and record where it landed.

    Idempotent: an export already `ready` returns its existing record rather
    than rendering a second copy, so an arq retry after a lost ack cannot
    double-write the Volume.
    """
    identifier = uuid.UUID(export_id)
    storage = get_storage()

    async with get_sessionmaker()() as session:
        export = await session.get(Export, identifier)
        if export is None:
            log.warning("export.missing", export_id=export_id)
            return {"export_id": export_id, "status": "missing"}

        if export.status is ExportStatus.READY and export.path:
            log.info("export.already_ready", export_id=export_id)
            return {"export_id": export_id, "status": export.status.value, "path": export.path}

        if export.artifact_type is ExportArtifactType.CAMPAIGN_PLAN:
            return await _generate_plan_export(session, export, storage=storage)

        report = await session.get(Report, export.artifact_id)
        if report is None:
            await _finish(
                session,
                export,
                status=ExportStatus.FAILED,
                error="The report this export belongs to no longer exists.",
            )
            return {"export_id": export_id, "status": ExportStatus.FAILED.value}

        run = await session.get(Run, report.run_id)
        project = await session.get(Project, run.project_id) if run else None

        export.status = ExportStatus.RUNNING
        await session.commit()

        try:
            parsed = ResearchReport.model_validate(report.payload)
            rendered = render(
                export.format,
                parsed,
                project_name=project.name if project else None,
                stored_markdown=report.markdown,
                stored_payload=report.payload,
                storage=storage,
            )
            key = storage_key(report.run_id, rendered.filename)
            storage.put(key, rendered.payload, content_type=rendered.media_type)
        except Exception as exc:  # noqa: BLE001 — every failure is recorded, then reported
            log.exception(
                "export.failed",
                export_id=export_id,
                format=export.format.value,
                error=str(exc),
            )
            await _finish(
                session,
                export,
                status=ExportStatus.FAILED,
                error=f"{type(exc).__name__}: {exc}",
            )
            return {"export_id": export_id, "status": ExportStatus.FAILED.value, "error": str(exc)}

        await _finish(
            session,
            export,
            status=ExportStatus.READY,
            path=key,
            size=len(rendered.payload),
        )
        log.info(
            "export.ready",
            export_id=export_id,
            format=export.format.value,
            bytes=len(rendered.payload),
            key=key,
        )

        # Best effort. A run whose console nobody has open still has a finished
        # export; the durable answer is the row, and this is only the fast one.
        try:
            await RunEventStream(get_redis(), report.run_id).publish(
                EventType.EXPORT_READY,
                export_id=str(export.id),
                report_id=str(report.id),
                format=export.format.value,
                bytes=len(rendered.payload),
                filename=rendered.filename,
            )
        except Exception as exc:  # noqa: BLE001 — never fail a finished export on a notification
            log.warning("export.event_failed", export_id=export_id, error=str(exc))

        return {
            "export_id": export_id,
            "status": ExportStatus.READY.value,
            "path": key,
            "bytes": len(rendered.payload),
        }


async def _generate_plan_export(
    session: AsyncSession, export: Export, *, storage: StorageBackend
) -> dict[str, Any]:
    """Render one campaign-plan export (Stage 02 PRD §14).

    Split from `generate_export` rather than folded into it because the two
    artifacts resolve differently — a report through `Report.run_id`, a plan
    through `CampaignPlan.plan_run_id` — and one function branching on every
    line would make neither readable. The failure handling is identical and
    deliberately so: every failure is recorded on the row, never swallowed.
    """
    plan_row = await session.get(CampaignPlanRow, export.artifact_id)
    if plan_row is None:
        await _finish(
            session,
            export,
            status=ExportStatus.FAILED,
            error="The campaign plan this export belongs to no longer exists.",
        )
        return {"export_id": str(export.id), "status": ExportStatus.FAILED.value}

    project = await session.get(Project, plan_row.project_id)
    frozen_by = (
        await session.get(User, plan_row.frozen_by) if plan_row.frozen_by is not None else None
    )

    export.status = ExportStatus.RUNNING
    await session.commit()

    try:
        parsed = CampaignPlan.model_validate(plan_row.payload)
        rendered = render_plan(
            export.format,
            parsed,
            project_name=project.name if project else None,
            stored_markdown=plan_row.markdown,
            stored_payload=plan_row.payload,
            frozen_by_name=frozen_by.name if frozen_by else "",
        )
        key = storage_key(plan_row.plan_run_id, rendered.filename)
        storage.put(key, rendered.payload, content_type=rendered.media_type)
    except Exception as exc:  # noqa: BLE001 — every failure is recorded, then reported
        log.exception(
            "export.plan_failed",
            export_id=str(export.id),
            format=export.format.value,
            error=str(exc),
        )
        await _finish(
            session, export, status=ExportStatus.FAILED, error=f"{type(exc).__name__}: {exc}"
        )
        return {
            "export_id": str(export.id),
            "status": ExportStatus.FAILED.value,
            "error": str(exc),
        }

    await _finish(session, export, status=ExportStatus.READY, path=key, size=len(rendered.payload))
    log.info(
        "export.plan_ready",
        export_id=str(export.id),
        format=export.format.value,
        bytes=len(rendered.payload),
        key=key,
    )

    try:
        await RunEventStream(get_redis(), plan_row.plan_run_id).publish(
            EventType.EXPORT_READY,
            export_id=str(export.id),
            plan_id=str(plan_row.id),
            format=export.format.value,
            bytes=len(rendered.payload),
            filename=rendered.filename,
        )
    except Exception as exc:  # noqa: BLE001 — never fail a finished export on a notification
        log.warning("export.event_failed", export_id=str(export.id), error=str(exc))

    return {
        "export_id": str(export.id),
        "status": ExportStatus.READY.value,
        "path": key,
        "bytes": len(rendered.payload),
    }
