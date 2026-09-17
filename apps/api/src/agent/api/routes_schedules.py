"""Recurring runs (PRD §14). The editor behind `/settings`'s schedule panel.

Writes require `SETTINGS_WRITE` rather than `RUN_EXECUTE`. A schedule is a
standing instruction to spend money on this workspace's behalf with nobody
watching, which is the settings-holder's decision and not the same act as
launching one run you are present for. PRD §13.4 E puts the editor on the admin
settings screen, and the server rule matches the screen rather than being looser
than it.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_schedules import (
    PREVIEW_COUNT,
    ScheduleCreate,
    ScheduleList,
    SchedulePreview,
    SchedulePreviewRequest,
    ScheduleResponse,
    ScheduleUpdate,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.db.models import Project, Run, Schedule
from agent.db.repos import ProjectRepo, RunRepo, ScheduleRepo, UserRepo
from agent.db.session import get_session
from agent.scheduling import cron
from agent.scheduling.poller import next_at_for

log = structlog.get_logger(__name__)

router = APIRouter(tags=["schedules"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
SettingsWriter = Annotated[Principal, Depends(require(Permission.SETTINGS_WRITE))]


@router.get("/schedules", response_model=ScheduleList, summary="Recurring runs")
async def list_schedules(
    me: AnyMember, db: Db, project_id: uuid.UUID | None = None
) -> ScheduleList:
    rows = await ScheduleRepo(db, me.workspace_id).all_ordered(project_id=project_id)
    return ScheduleList(items=await _responses(db, me, rows))


@router.post(
    "/schedules/preview",
    response_model=SchedulePreview,
    summary="What an expression means",
)
async def preview_schedule(body: SchedulePreviewRequest, me: AnyMember) -> SchedulePreview:
    """Validate and describe an expression without storing it.

    The editor calls this as the admin types. It is a `POST` because a cron
    expression in a query string is a thicket of escaping for no gain, and it is
    readable by any member because it touches nothing.
    """
    return _preview(body.cron, body.timezone)


@router.post(
    "/schedules",
    response_model=ScheduleResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a recurring run",
)
async def create_schedule(
    body: ScheduleCreate, me: SettingsWriter, request: Request, db: Db
) -> ScheduleResponse:
    project = await ProjectRepo(db, me.workspace_id).get(body.project_id)
    if project is None:
        raise problems.not_found(f"No project {body.project_id}.")

    schedules = ScheduleRepo(db, me.workspace_id)
    schedule = Schedule(
        project_id=project.id,
        created_by=me.user.id,
        cron=body.cron,
        timezone=body.timezone,
        enabled=body.enabled,
    )
    schedules.add(schedule)
    # An enabled row is due from the moment it is written; a disabled one has no
    # next firing at all, so the poller's `next_at IS NOT NULL` filter skips it
    # without a second condition to keep in sync.
    schedule.next_at = _next_at(schedule) if body.enabled else None
    # `id` is a Python-side default applied at flush, so the audit row below
    # would carry a NULL target without this.
    await db.flush()

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.SCHEDULE_CREATED,
        target_type=AuditTarget.SCHEDULE,
        target_id=schedule.id,
        meta={
            "project_id": str(project.id),
            "cron": schedule.cron,
            "timezone": schedule.timezone,
            "enabled": schedule.enabled,
        },
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(schedule)
    return await _response(db, me, schedule)


@router.patch(
    "/schedules/{schedule_id}",
    response_model=ScheduleResponse,
    summary="Edit a recurring run",
)
async def update_schedule(
    schedule_id: uuid.UUID, body: ScheduleUpdate, me: SettingsWriter, request: Request, db: Db
) -> ScheduleResponse:
    schedules = ScheduleRepo(db, me.workspace_id)
    schedule = await schedules.get(schedule_id)
    if schedule is None:
        raise problems.not_found(f"No schedule {schedule_id}.")

    changed: dict[str, object] = {}
    if body.cron is not None and body.cron != schedule.cron:
        changed["cron"] = {"from": schedule.cron, "to": body.cron}
        schedule.cron = body.cron
    if body.timezone is not None and body.timezone != schedule.timezone:
        changed["timezone"] = {"from": schedule.timezone, "to": body.timezone}
        schedule.timezone = body.timezone
    if body.enabled is not None and body.enabled != schedule.enabled:
        changed["enabled"] = body.enabled
        schedule.enabled = body.enabled

    if changed:
        # Recomputed on every edit, not only on a cron change: re-enabling a row
        # whose `next_at` is months in the past would otherwise fire it at once
        # and then again at its real slot.
        schedule.next_at = _next_at(schedule) if schedule.enabled else None
        write_audit(
            db,
            workspace_id=me.workspace_id,
            actor_id=me.user.id,
            action=AuditAction.SCHEDULE_UPDATED,
            target_type=AuditTarget.SCHEDULE,
            target_id=schedule.id,
            meta={"project_id": str(schedule.project_id), "changed": changed},
            ip=client_ip(request),
        )
        await db.commit()
        await db.refresh(schedule)

    return await _response(db, me, schedule)


@router.delete(
    "/schedules/{schedule_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a recurring run",
)
async def delete_schedule(
    schedule_id: uuid.UUID, me: SettingsWriter, request: Request, db: Db
) -> Response:
    schedules = ScheduleRepo(db, me.workspace_id)
    schedule = await schedules.get(schedule_id)
    if schedule is None:
        raise problems.not_found(f"No schedule {schedule_id}.")

    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.SCHEDULE_DELETED,
        target_type=AuditTarget.SCHEDULE,
        target_id=schedule.id,
        meta={
            "project_id": str(schedule.project_id),
            "cron": schedule.cron,
            "timezone": schedule.timezone,
        },
        ip=client_ip(request),
    )
    await db.delete(schedule)
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _next_at(schedule: Schedule) -> datetime:
    """The next firing, or a 422 naming the field the admin has to fix."""
    try:
        return next_at_for(schedule, after=datetime.now(UTC))
    except cron.CronError as exc:  # pragma: no cover — the schema validated it already
        raise problems.unprocessable(str(exc)) from exc


def _preview(expression: str, timezone: str) -> SchedulePreview:
    parsed = cron.parse(expression)
    return SchedulePreview(
        cron=parsed.raw,
        timezone=timezone,
        description=cron.describe(parsed, timezone=timezone),
        upcoming=cron.upcoming(
            parsed, after=datetime.now(UTC), timezone=timezone, count=PREVIEW_COUNT
        ),
    )


async def _responses(
    db: AsyncSession, me: Principal, rows: list[Schedule]
) -> list[ScheduleResponse]:
    if not rows:
        return []
    projects = await _project_names(db, me, [row.project_id for row in rows])
    authors = await UserRepo(db, me.workspace_id).names([row.created_by for row in rows])
    last_runs = await _last_runs(db, me, [row.last_run_id for row in rows if row.last_run_id])
    return [_render(row, projects, authors, last_runs) for row in rows]


async def _response(db: AsyncSession, me: Principal, schedule: Schedule) -> ScheduleResponse:
    rendered = await _responses(db, me, [schedule])
    return rendered[0]


def _render(
    schedule: Schedule,
    projects: dict[uuid.UUID, str],
    authors: dict[uuid.UUID, str],
    last_runs: dict[uuid.UUID, Run],
) -> ScheduleResponse:
    preview = _preview(schedule.cron, schedule.timezone)
    last_run = last_runs.get(schedule.last_run_id) if schedule.last_run_id else None
    return ScheduleResponse(
        id=schedule.id,
        project_id=schedule.project_id,
        project_name=projects.get(schedule.project_id, "—"),
        cron=schedule.cron,
        timezone=schedule.timezone,
        enabled=schedule.enabled,
        description=preview.description,
        upcoming=preview.upcoming,
        next_at=schedule.next_at,
        last_run_id=schedule.last_run_id,
        last_run_at=last_run.started_at if last_run else None,
        last_run_status=last_run.status.value if last_run else None,
        created_by=schedule.created_by,
        created_by_name=authors.get(schedule.created_by, "—"),
    )


async def _project_names(
    db: AsyncSession, me: Principal, ids: list[uuid.UUID]
) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    result = await db.execute(
        sa.select(Project.id, Project.name).where(
            Project.workspace_id == me.workspace_id, Project.id.in_(set(ids))
        )
    )
    return {row.id: row.name for row in result}


async def _last_runs(db: AsyncSession, me: Principal, ids: list[uuid.UUID]) -> dict[uuid.UUID, Run]:
    if not ids:
        return {}
    runs = RunRepo(db, me.workspace_id)
    result = await db.execute(runs.select().where(Run.id.in_(set(ids))))
    return {run.id: run for run in result.scalars()}
