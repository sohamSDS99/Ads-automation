"""Concrete repositories. Every P0b query starts here, already workspace-scoped.

`WorkspaceScopedRepo.select()` is the only query constructor, so forgetting the
`workspace_id` filter is not something a route can do by accident (PRD §6).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    AuditLog,
    Export,
    ExportFormat,
    Invite,
    Project,
    Report,
    Run,
    User,
    UserRole,
    UserStatus,
)
from agent.db.repo import WorkspaceScopedRepo


class UserRepo(WorkspaceScopedRepo[User]):
    model = User

    async def by_email(self, email: str) -> User | None:
        result = await self.session.execute(self.select().where(User.email == email))
        return result.scalar_one_or_none()

    async def all_ordered(self) -> list[User]:
        result = await self.session.execute(self.select().order_by(User.created_at.asc()))
        return list(result.scalars().all())

    async def lock_active_admins(self) -> list[User]:
        """Row-lock every active admin, then return them.

        The lock is what makes the last-admin rule hold under concurrency: two
        simultaneous demotions serialise here, so the second one sees the first
        one's effect instead of both counting the same two admins and both
        succeeding (PRD §6.1, Authorization 4).
        """
        result = await self.session.execute(
            self.select()
            .where(User.role == UserRole.ADMIN, User.status == UserStatus.ACTIVE)
            .with_for_update()
        )
        return list(result.scalars().all())


class InviteRepo(WorkspaceScopedRepo[Invite]):
    model = Invite

    async def open_for_email(self, email: str) -> Invite | None:
        result = await self.session.execute(
            self.select().where(Invite.email == email, Invite.accepted_at.is_(None))
        )
        return result.scalar_one_or_none()


class AuditRepo(WorkspaceScopedRepo[AuditLog]):
    model = AuditLog

    async def page(
        self,
        *,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        before: tuple[datetime, uuid.UUID] | None = None,
        limit: int = 50,
    ) -> list[tuple[AuditLog, str | None]]:
        """One page of the audit log, newest first, with the actor's email joined in.

        Ordering is `(created_at DESC, id DESC)` and the cursor carries both, so
        rows written inside the same transaction — which share a timestamp —
        still paginate without repeats or gaps.
        """
        stmt = (
            sa.select(AuditLog, User.email)
            .outerjoin(User, User.id == AuditLog.actor_id)
            .where(AuditLog.workspace_id == self.workspace_id)
            .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
            .limit(limit)
        )
        if actor_id is not None:
            stmt = stmt.where(AuditLog.actor_id == actor_id)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        if since is not None:
            stmt = stmt.where(AuditLog.created_at >= since)
        if until is not None:
            stmt = stmt.where(AuditLog.created_at <= until)
        if before is not None:
            cursor_created_at, cursor_id = before
            stmt = stmt.where(
                sa.tuple_(AuditLog.created_at, AuditLog.id)
                < sa.tuple_(sa.literal(cursor_created_at), sa.literal(cursor_id))
            )
        result = await self.session.execute(stmt)
        return [(row[0], row[1]) for row in result.all()]


class ProjectRepo(WorkspaceScopedRepo[Project]):
    model = Project


class RunRepo(WorkspaceScopedRepo[Run]):
    model = Run

    async def for_project(self, project_id: uuid.UUID, *, limit: int = 50) -> list[Run]:
        result = await self.session.execute(
            self.select()
            .where(Run.project_id == project_id)
            .order_by(Run.started_at.desc().nullslast(), Run.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


class ReportRepo:
    """Reports, scoped through the run that produced them.

    `Report` has no `workspace_id` column, so it cannot subclass
    `WorkspaceScopedRepo` — that base class refuses a model it cannot scope,
    which is what stops an unscoped query being written by accident. The scope
    is still mandatory here; it just arrives over a join, exactly as
    `db/repo.py` instructs ("reach it through its owning Project or Run").
    """

    def __init__(self, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def _scoped(self) -> sa.Select[tuple[Report]]:
        return (
            sa.select(Report)
            .join(Run, Run.id == Report.run_id)
            .where(Run.workspace_id == self.workspace_id)
        )

    async def for_run(self, run_id: uuid.UUID) -> Report | None:
        result = await self.session.execute(self._scoped().where(Report.run_id == run_id))
        return result.scalar_one_or_none()

    async def get(self, report_id: uuid.UUID) -> Report | None:
        result = await self.session.execute(self._scoped().where(Report.id == report_id))
        return result.scalar_one_or_none()

    async def run_for(self, report_id: uuid.UUID) -> Run | None:
        """The run behind a report — the project id and the SSE channel live on it."""
        result = await self.session.execute(
            sa.select(Run)
            .join(Report, Report.run_id == Run.id)
            .where(Report.id == report_id, Run.workspace_id == self.workspace_id)
        )
        return result.scalar_one_or_none()


class ExportRepo:
    """Export jobs, scoped through report → run, for the same reason as above."""

    def __init__(self, session: AsyncSession, workspace_id: uuid.UUID) -> None:
        self.session = session
        self.workspace_id = workspace_id

    def _scoped(self) -> sa.Select[tuple[Export]]:
        return (
            sa.select(Export)
            .join(Report, Report.id == Export.report_id)
            .join(Run, Run.id == Report.run_id)
            .where(Run.workspace_id == self.workspace_id)
        )

    async def get(self, export_id: uuid.UUID) -> Export | None:
        result = await self.session.execute(self._scoped().where(Export.id == export_id))
        return result.scalar_one_or_none()

    async def for_report(self, report_id: uuid.UUID, *, limit: int = 50) -> list[Export]:
        result = await self.session.execute(
            self._scoped()
            .where(Export.report_id == report_id)
            .order_by(Export.created_at.desc(), Export.id.desc())
            .limit(limit)
        )
        return list(result.scalars().all())

    async def run_for(self, export_id: uuid.UUID) -> Run | None:
        result = await self.session.execute(
            sa.select(Run)
            .join(Report, Report.run_id == Run.id)
            .join(Export, Export.report_id == Report.id)
            .where(Export.id == export_id, Run.workspace_id == self.workspace_id)
        )
        return result.scalar_one_or_none()

    def add(self, report_id: uuid.UUID, fmt: ExportFormat, *, requested_by: uuid.UUID) -> Export:
        """Stage a queued export. The row exists before the file does (PRD §12)."""
        export = Export(report_id=report_id, format=fmt, requested_by=requested_by)
        self.session.add(export)
        return export


def utcnow() -> datetime:
    return datetime.now(UTC)
