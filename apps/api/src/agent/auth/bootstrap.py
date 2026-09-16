"""First boot: the Workspace singleton and the first admin.

This is the only path that creates a user without an invite, and it closes
permanently the moment one user exists (PRD §6.1.1). It runs twice over an
installation's life at most: once from `lifespan` if the bootstrap env vars are
set, and once from `POST /auth/bootstrap` if they were not.
"""

from __future__ import annotations

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import passwords
from agent.config import Settings
from agent.db.models import User, UserRole, UserStatus, Workspace

log = structlog.get_logger(__name__)

DEFAULT_WORKSPACE_NAME = "Research Workspace"


class BootstrapUnavailable(RuntimeError):
    """Bootstrap was attempted after the workspace already had a user."""


async def get_workspace(db: AsyncSession) -> Workspace | None:
    result = await db.execute(sa.select(Workspace).limit(1))
    return result.scalar_one_or_none()


async def ensure_workspace(db: AsyncSession) -> Workspace:
    """Return the singleton, creating it if this is the first boot.

    Two API replicas starting at once both reach here. The
    `uq_workspace_singleton` index lets exactly one insert land; the loser
    catches the IntegrityError and reads the winner's row.
    """
    existing = await get_workspace(db)
    if existing is not None:
        return existing

    workspace = Workspace(name=DEFAULT_WORKSPACE_NAME)
    db.add(workspace)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raced = await get_workspace(db)
        if raced is None:  # pragma: no cover — only reachable if the index is gone
            raise
        return raced
    return workspace


async def has_any_user(db: AsyncSession) -> bool:
    result = await db.execute(sa.select(sa.func.count()).select_from(User))
    return int(result.scalar_one()) > 0


async def create_first_admin(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    name: str = "Administrator",
    ip: str | None = None,
) -> User:
    """Create the workspace and its first admin, or raise if one already exists.

    The caller commits. Validation of the password happens before anything is
    written, so a weak `BOOTSTRAP_ADMIN_PASSWORD` fails loudly with an empty
    database rather than leaving a half-built workspace behind.
    """
    if await has_any_user(db):
        raise BootstrapUnavailable("This workspace already has users.")

    password_hash = passwords.hash_password(password)
    workspace = await ensure_workspace(db)

    admin = User(
        workspace_id=workspace.id,
        email=email,
        name=name,
        password_hash=password_hash,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    db.add(admin)
    await db.flush()

    write_audit(
        db,
        workspace_id=workspace.id,
        action=AuditAction.BOOTSTRAP,
        target_type=AuditTarget.USER,
        target_id=admin.id,
        meta={"email": email, "role": UserRole.ADMIN.value},
        ip=ip,
    )
    return admin


async def bootstrap_from_environment(db: AsyncSession, settings: Settings) -> User | None:
    """Startup hook. Silent no-op when the env vars are unset or a user exists."""
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        return None
    if await has_any_user(db):
        return None

    try:
        admin = await create_first_admin(
            db,
            email=settings.bootstrap_admin_email,
            password=settings.bootstrap_admin_password.get_secret_value(),
        )
    except passwords.PasswordPolicyError as exc:
        # Refusing here is the point: a workspace whose only admin has a
        # rejected password would be unreachable.
        log.error("bootstrap.password_rejected", reason=str(exc))
        await db.rollback()
        return None
    except IntegrityError:
        # Another replica won the race. Its admin is the admin.
        await db.rollback()
        return None

    await db.commit()
    log.info("bootstrap.admin_created", email=settings.bootstrap_admin_email)
    return admin
