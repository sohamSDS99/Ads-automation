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
from agent.auth.rbac import Permission, permissions_for
from agent.auth.sessions import SessionRecord, SessionStore
from agent.db.models import User, UserStatus
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
    """The caller. Everything a route needs to authorize and to audit."""

    user: User
    session: SessionRecord
    permissions: frozenset[Permission]

    @property
    def workspace_id(self) -> uuid.UUID:
        """The workspace every repository this caller opens must be scoped to."""
        return self.user.workspace_id


async def current_user(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_session)],
    store: Annotated[SessionStore, Depends(get_session_store)],
) -> Principal:
    """Resolve the session cookie to a live, active user.

    The `user` row is read on every request on purpose. Caching the role in the
    session would mean a demotion or a disable only took effect the next time
    the session was rebuilt; reading it here makes both immediate, which is what
    the acceptance criteria measure.
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

    bind_actor_role(user.role.value)
    return Principal(user=user, session=record, permissions=permissions_for(user.role))


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
                role=principal.user.role,
                missing_permission=permission.value,
            )
            raise problems.forbidden(missing_permission=permission.value)
        return principal

    _guard.__ara_permission__ = permission  # type: ignore[attr-defined]
    _guard.__name__ = f"require_{permission.value}"
    return _guard
