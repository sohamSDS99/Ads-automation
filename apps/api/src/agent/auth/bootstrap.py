"""First boot: the first workspace, and the administrator of the whole system.

This is the only path that creates a user without an invite, and it closes
permanently the moment one account exists (PRD §6.1.1). It runs twice over an
installation's life at most: once from `lifespan` if the bootstrap env vars are
set, and once from `POST /auth/bootstrap` if they were not.

What it creates is now three rows rather than two: a workspace, the account,
and the membership that puts the account in the workspace as its admin. The
account is also the system administrator — `is_superadmin` — because somebody
has to be able to create the *second* workspace, and on a fresh installation
there is nobody else to be it.
"""

from __future__ import annotations

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import passwords, workspaces
from agent.config import Settings
from agent.db.models import User, UserRole, UserStatus, Workspace

log = structlog.get_logger(__name__)

DEFAULT_WORKSPACE_NAME = "Research Workspace"


class BootstrapUnavailable(RuntimeError):
    """Bootstrap was attempted after the installation already had an account."""


async def first_workspace(db: AsyncSession) -> Workspace | None:
    """The oldest workspace on the installation.

    The last remnant of the singleton, and deliberately narrow in what it is
    used for: seeding a session that has no workspace yet, and naming the
    installation in a log line. Anything that authorizes a request resolves its
    workspace from the caller's session instead (`agent.auth.workspaces`).
    """
    result = await db.execute(
        sa.select(Workspace).order_by(Workspace.created_at, Workspace.id).limit(1)
    )
    return result.scalar_one_or_none()


async def has_any_user(db: AsyncSession) -> bool:
    result = await db.execute(sa.select(sa.func.count()).select_from(User))
    return int(result.scalar_one()) > 0


async def create_first_admin(
    db: AsyncSession,
    *,
    email: str,
    password: str,
    name: str = "Administrator",
    workspace_name: str = DEFAULT_WORKSPACE_NAME,
    ip: str | None = None,
) -> User:
    """Create the first workspace and the system administrator, or raise.

    The caller commits. Validation of the password happens before anything is
    written, so a weak `BOOTSTRAP_ADMIN_PASSWORD` fails loudly with an empty
    database rather than leaving a half-built installation behind.
    """
    if await has_any_user(db):
        raise BootstrapUnavailable("This installation already has an account.")

    password_hash = passwords.hash_password(password)

    workspace = Workspace(name=workspace_name)
    db.add(workspace)
    await db.flush()

    admin = User(
        email=email,
        name=name,
        password_hash=password_hash,
        status=UserStatus.ACTIVE,
        is_superadmin=True,
        last_workspace_id=workspace.id,
    )
    db.add(admin)
    await db.flush()

    workspace.created_by = admin.id
    workspaces.add_member(
        db,
        workspace_id=workspace.id,
        user_id=admin.id,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
    )
    await db.flush()

    write_audit(
        db,
        workspace_id=workspace.id,
        action=AuditAction.BOOTSTRAP,
        target_type=AuditTarget.USER,
        target_id=admin.id,
        meta={"email": email, "role": UserRole.ADMIN.value, "superadmin": True},
        ip=ip,
    )
    return admin


async def bootstrap_from_environment(db: AsyncSession, settings: Settings) -> User | None:
    """Startup hook. Silent no-op when the env vars are unset or an account exists."""
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
        # Refusing here is the point: an installation whose only admin has a
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
