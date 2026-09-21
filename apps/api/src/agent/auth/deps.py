"""The two dependencies every protected route is built from.

`current_user` answers "who is calling"; `require(Permission)` answers "may they".
A route names a permission and nothing else — it never sees a role string, and
`scripts/check_route_guards.py` fails the build if a route names neither.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

import structlog
from fastapi import Depends, Request
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.logging_middleware import bind_actor_role
from agent.auth import workspaces
from agent.auth.rbac import Permission, permissions_for
from agent.auth.sessions import SessionRecord, SessionStore
from agent.db.models import Membership, User, UserRole, UserStatus, Workspace
from agent.db.session import get_session
from agent.redis_client import get_redis

log = structlog.get_logger(__name__)

# Set by `SessionMiddleware`, which has to resolve the session anyway to check
# CSRF. Routes read it through the dependencies below, never directly.
REQUEST_STATE_SESSION = "auth_session"


def get_redis_client() -> Redis:
    return get_redis()


def get_session_store(redis: Annotated[Redis, Depends(get_redis_client)]) -> SessionStore:
    return SessionStore(redis)


@dataclass(frozen=True, slots=True)
class Principal:
    """The caller, in one workspace. Everything a route needs to authorize and audit.

    A principal is a *pair*, not a person: the same account calling from two
    browsers signed into two workspaces produces two principals with different
    roles and different permissions. Routes that reach for `me.user.role` will
    not find it — there is no such thing any more, and `me.role` (the role on
    this workspace's membership) is the question they meant to ask.
    """

    user: User
    session: SessionRecord
    permissions: frozenset[Permission]
    workspace: Workspace
    membership: Membership | None
    role: UserRole
    #: True when the caller is in this workspace as the system administrator
    #: rather than as a member of it. Written to every audit row they cause.
    via_superadmin: bool = False

    @property
    def workspace_id(self) -> uuid.UUID:
        """The workspace every repository this caller opens must be scoped to."""
        return self.workspace.id

    @property
    def is_superadmin(self) -> bool:
        return self.user.is_superadmin

    def audit_meta(self, **extra: object) -> dict[str, object]:
        """Audit metadata with the visiting-administrator fact attached.

        A system administrator acting inside somebody else's workspace looks
        exactly like that workspace's own admin in the log unless the log says
        otherwise. This is what says otherwise.
        """
        meta: dict[str, object] = dict(extra)
        if self.via_superadmin:
            meta["via_superadmin"] = True
        return meta


async def current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[SessionStore, Depends(get_session_store)],
) -> Principal:
    """Resolve the session cookie to a live account with live access.

    Both halves are re-read on every request on purpose. Caching the role in
    the session would mean a demotion or a disable only took effect the next
    time the session was rebuilt; reading the `user` row and the `membership`
    row here makes both immediate, and makes the workspace id in the session a
    statement of where the browser is rather than permission to be there.
    """
    record: SessionRecord | None = getattr(request.state, REQUEST_STATE_SESSION, None)
    if record is None:
        raise problems.unauthenticated()

    user = await db.get(User, record.user_id)
    if user is None:
        # The row is gone but the session is not. Fail closed and clean up.
        await store.revoke(record.sid, user_id=record.user_id)
        raise problems.unauthenticated()

    if user.status is not UserStatus.ACTIVE:
        await store.revoke_all_for_user(user.id)
        log.info("auth.session_revoked_inactive_user", user_id=str(user.id), status=user.status)
        raise problems.unauthenticated("This account is no longer active.")

    try:
        access = await workspaces.resolve_access(db, user=user, workspace_id=record.workspace_id)
    except workspaces.WorkspaceAccessError as exc:
        # Only this one session dies. The account may still hold other
        # workspaces, and signing them out of those because access to this one
        # ended would be a second punishment for someone else's decision.
        await store.revoke(record.sid, user_id=user.id)
        log.info(
            "auth.session_revoked_no_access",
            user_id=str(user.id),
            workspace_id=str(record.workspace_id),
            reason=exc.reason,
        )
        raise problems.unauthenticated(exc.detail) from exc

    bind_actor_role(access.role.value)
    return Principal(
        user=user,
        session=record,
        permissions=permissions_for(access.role, superadmin=user.is_superadmin),
        workspace=access.workspace,
        membership=access.membership,
        role=access.role,
        via_superadmin=access.via_superadmin,
    )


CurrentUser = Annotated[Principal, Depends(current_user)]


def require(permission: Permission) -> Callable[[Principal], Awaitable[Principal]]:
    """Build the dependency that guards one route.

    The returned callable carries `__ara_permission__` so the CI guard check can
    recognise a guarded route by inspection rather than by naming convention.
    """

    async def _guard(principal: CurrentUser) -> Principal:
        if permission not in principal.permissions:
            log.info(
                "auth.forbidden",
                user_id=str(principal.user.id),
                role=principal.role,
                workspace_id=str(principal.workspace_id),
                missing_permission=permission.value,
            )
            raise problems.forbidden(missing_permission=permission.value)
        return principal

    _guard.__ara_permission__ = permission  # type: ignore[attr-defined]
    _guard.__name__ = f"require_{permission.value}"
    return _guard
