"""Concrete repositories. Every P0b query starts here, already workspace-scoped.

`WorkspaceScopedRepo.select()` is the only query constructor, so forgetting the
`workspace_id` filter is not something a route can do by accident (PRD §6).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa

from agent.db.models import AuditLog, Invite, User, UserRole, UserStatus
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


def utcnow() -> datetime:
    return datetime.now(UTC)
