"""Sign in, sign out, and everything a user does to their own account."""

from __future__ import annotations

import hashlib
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import (
    CSRF_COOKIE_NAME,
    client_ip,
    csrf_cookie_kwargs,
    session_cookie_kwargs,
    user_agent,
)
from agent.api.schemas_auth import (
    BootstrapRequest,
    ChangePasswordRequest,
    CsrfResponse,
    LoginRequest,
    MeResponse,
    SessionListResponse,
    SessionSummary,
    UserSummary,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import bootstrap, passwords
from agent.auth.deps import (
    REQUEST_STATE_SESSION,
    Principal,
    get_redis_client,
    get_session_store,
    require,
)
from agent.auth.ratelimit import LockoutState, LoginRateLimiter
from agent.auth.rbac import Permission, permissions_for
from agent.auth.sessions import SessionRecord, SessionStore, new_csrf_token
from agent.config import Settings, get_settings
from agent.db.models import User, UserStatus
from agent.db.repos import UserRepo, utcnow
from agent.db.session import get_session

log = structlog.get_logger(__name__)

router = APIRouter(tags=["auth"])

Db = Annotated[AsyncSession, Depends(get_session)]
Store = Annotated[SessionStore, Depends(get_session_store)]
RedisDep = Annotated[Redis, Depends(get_redis_client)]
SettingsDep = Annotated[Settings, Depends(get_settings)]

#: Every route on this module needs no more than READ: these are the things any
#: signed-in user may do to their own account.
SignedIn = Annotated[Principal, Depends(require(Permission.READ))]

#: A session id must never appear in a response body — that would put an XSS one
#: step from account takeover. The digest names a session for revocation and is
#: worthless as a cookie.
SESSION_HANDLE_LENGTH = 16


def session_handle(sid: str) -> str:
    return hashlib.sha256(sid.encode()).hexdigest()[:SESSION_HANDLE_LENGTH]


def _set_session_cookies(response: Response, record: SessionRecord, settings: Settings) -> None:
    response.set_cookie(settings.session_cookie_name, record.sid, **session_cookie_kwargs(settings))
    response.set_cookie(CSRF_COOKIE_NAME, record.csrf_token, **csrf_cookie_kwargs(settings))


def _me(user: User, workspace_name: str) -> MeResponse:
    return MeResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        role=user.role,
        permissions=sorted(permissions_for(user.role), key=lambda p: p.value),
        workspace_id=user.workspace_id,
        workspace_name=workspace_name,
    )


def _lockout_problem(state: LockoutState) -> problems.Problem:
    minutes = max(1, round(state.retry_after_seconds / 60))
    return problems.Problem(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        title="Too many sign-in attempts",
        detail=(
            f"Too many failed sign-in attempts. Try again in {minutes} "
            f"minute{'s' if minutes != 1 else ''}."
        ),
        type_=problems.TYPE_RATE_LIMITED,
        headers={"Retry-After": str(state.retry_after_seconds)},
        retry_after_seconds=state.retry_after_seconds,
    )


# --- public ------------------------------------------------------------------


@router.get("/auth/csrf", response_model=CsrfResponse, summary="Mint a CSRF token")
async def csrf(request: Request, response: Response, settings: SettingsDep) -> CsrfResponse:
    """Prime the double-submit cookie before an unauthenticated POST.

    `/login` and `/invite/[token]` are the only pages reachable without a
    session, so they are the only ones that need this. Every other page already
    holds a CSRF cookie bound to its session.
    """
    record: SessionRecord | None = getattr(request.state, REQUEST_STATE_SESSION, None)
    token = record.csrf_token if record is not None else new_csrf_token()
    response.set_cookie(CSRF_COOKIE_NAME, token, **csrf_cookie_kwargs(settings))
    return CsrfResponse(csrf_token=token)


@router.post(
    "/auth/bootstrap",
    response_model=UserSummary,
    status_code=status.HTTP_201_CREATED,
    summary="Create the workspace and its first admin",
)
async def bootstrap_workspace(
    _body: BootstrapRequest,
    request: Request,
    db: Db,
    settings: SettingsDep,
) -> UserSummary:
    if not settings.bootstrap_admin_email or not settings.bootstrap_admin_password:
        raise problems.conflict(
            "Set BOOTSTRAP_ADMIN_EMAIL and BOOTSTRAP_ADMIN_PASSWORD, then restart the API.",
            title="Bootstrap not configured",
        )
    try:
        admin = await bootstrap.create_first_admin(
            db,
            email=settings.bootstrap_admin_email,
            password=settings.bootstrap_admin_password.get_secret_value(),
            ip=client_ip(request),
        )
    except bootstrap.BootstrapUnavailable as exc:
        raise problems.conflict(str(exc), title="Already bootstrapped") from exc
    except passwords.PasswordPolicyError as exc:
        raise problems.Problem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            title="Bootstrap password rejected",
            detail=str(exc),
            type_=problems.TYPE_VALIDATION,
        ) from exc

    await db.commit()
    return UserSummary.model_validate(admin)


@router.post("/auth/login", response_model=MeResponse, summary="Sign in")
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    db: Db,
    store: Store,
    redis: RedisDep,
    settings: SettingsDep,
) -> MeResponse:
    """Verify a password and open a session.

    Every failure below takes the same shape and roughly the same time: an
    unknown email is verified against a fixed dummy hash, and a disabled account
    reports exactly what a wrong password reports. Neither the response nor the
    clock says whether an account exists (PRD §6.1.5).
    """
    ip = client_ip(request)
    limiter = LoginRateLimiter(redis)

    state = await limiter.check(body.email, ip)
    if state.locked:
        await _audit_login_failure(db, AuditAction.LOGIN_LOCKED, body.email, ip)
        raise _lockout_problem(state)

    workspace = await bootstrap.get_workspace(db)
    user: User | None = None
    if workspace is not None:
        user = await UserRepo(db, workspace.id).by_email(body.email)

    stored_hash = user.password_hash if user is not None and user.password_hash else None
    password_ok = passwords.verify(stored_hash or passwords.dummy_hash(), body.password)
    account_usable = user is not None and user.status is UserStatus.ACTIVE

    if not (password_ok and account_usable and stored_hash is not None):
        # The attempt was within the allowance, so it is answered as a failed
        # sign-in, not as a lockout. The lock is applied by the `check` above on
        # the *next* attempt — which is what makes the sixth the one refused.
        await limiter.record_failure(body.email, ip)
        await _audit_login_failure(db, AuditAction.LOGIN_FAILED, body.email, ip)
        raise problems.Problem(
            status_code=status.HTTP_401_UNAUTHORIZED,
            title="Sign-in failed",
            detail="That email and password don't match an active account.",
            type_=problems.TYPE_UNAUTHENTICATED,
        )

    if user is None or workspace is None:  # pragma: no cover — narrowed above
        raise problems.unauthenticated()

    if passwords.needs_rehash(stored_hash):
        user.password_hash = passwords.hash_password(body.password)

    await limiter.clear(body.email, ip)
    user.last_login_at = utcnow()
    record = await store.create(
        user_id=user.id,
        workspace_id=workspace.id,
        ip=ip,
        user_agent=user_agent(request),
    )
    write_audit(
        db,
        workspace_id=workspace.id,
        actor_id=user.id,
        action=AuditAction.LOGIN,
        target_type=AuditTarget.USER,
        target_id=user.id,
        ip=ip,
    )
    await db.commit()

    _set_session_cookies(response, record, settings)
    log.info("auth.login", user_id=str(user.id), role=user.role)
    return _me(user, workspace.name)


async def _audit_login_failure(
    db: AsyncSession, action: AuditAction, email: str, ip: str | None
) -> None:
    """Record a failed attempt, if there is a workspace to record it against.

    Committed on its own: the request is about to raise, and an audit row that
    rolled back with the failure would leave no trace of the attempt.
    """
    workspace = await bootstrap.get_workspace(db)
    if workspace is None:
        return
    write_audit(
        db,
        workspace_id=workspace.id,
        action=action,
        target_type=AuditTarget.USER,
        meta={"email": email},
        ip=ip,
    )
    await db.commit()


# --- authenticated -----------------------------------------------------------


@router.post(
    "/auth/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Sign out of this browser",
)
async def logout(
    me_: SignedIn,
    request: Request,
    db: Db,
    store: Store,
    settings: SettingsDep,
) -> Response:
    await store.revoke(me_.session.sid, user_id=me_.user.id)
    write_audit(
        db,
        workspace_id=me_.workspace_id,
        actor_id=me_.user.id,
        action=AuditAction.LOGOUT,
        target_type=AuditTarget.SESSION,
        target_id=me_.user.id,
        ip=client_ip(request),
    )
    await db.commit()

    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(settings.session_cookie_name, path="/")
    response.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return response


@router.get("/auth/me", response_model=MeResponse, summary="The signed-in caller")
async def me(me_: SignedIn, db: Db) -> MeResponse:
    workspace = await bootstrap.get_workspace(db)
    return _me(me_.user, workspace.name if workspace else "")


@router.post(
    "/auth/password",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Change your own password",
)
async def change_password(
    me_: SignedIn,
    body: ChangePasswordRequest,
    request: Request,
    db: Db,
    store: Store,
    settings: SettingsDep,
) -> Response:
    """Rotate the caller's password and re-issue exactly one session.

    Every other session is revoked — that is the whole point of changing a
    password after a suspected compromise.
    """
    if not passwords.verify(me_.user.password_hash or "", body.current_password):
        raise problems.Problem(
            status_code=status.HTTP_403_FORBIDDEN,
            title="Current password incorrect",
            detail="The current password you entered is not correct.",
            type_=problems.TYPE_FORBIDDEN,
        )

    try:
        me_.user.password_hash = passwords.hash_password(body.new_password)
    except passwords.PasswordPolicyError as exc:
        raise problems.Problem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            title="Password rejected",
            detail=str(exc),
            type_=problems.TYPE_VALIDATION,
        ) from exc

    ip = client_ip(request)
    write_audit(
        db,
        workspace_id=me_.workspace_id,
        actor_id=me_.user.id,
        action=AuditAction.PASSWORD_CHANGED,
        target_type=AuditTarget.USER,
        target_id=me_.user.id,
        ip=ip,
    )
    await db.commit()

    await store.revoke_all_for_user(me_.user.id)
    fresh = await store.create(
        user_id=me_.user.id,
        workspace_id=me_.workspace_id,
        ip=ip,
        user_agent=user_agent(request),
    )
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    _set_session_cookies(response, fresh, settings)
    return response


@router.get(
    "/auth/sessions",
    response_model=SessionListResponse,
    summary="Your signed-in browsers",
)
async def list_sessions(me_: SignedIn, store: Store) -> SessionListResponse:
    records = await store.list_for_user(me_.user.id)
    return SessionListResponse(
        sessions=[
            SessionSummary(
                id=session_handle(record.sid),
                created_at=record.created_at,
                last_seen_at=record.last_seen_at,
                absolute_expires_at=record.absolute_expires_at,
                ip=record.ip,
                user_agent=record.user_agent,
                current=record.sid == me_.session.sid,
            )
            for record in records
        ]
    )


@router.delete(
    "/auth/sessions/{session_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke one of your sessions",
)
async def revoke_session(
    me_: SignedIn,
    session_id: str,
    request: Request,
    db: Db,
    store: Store,
) -> Response:
    """Revoke by digest.

    The handle is only ever matched against this caller's own sessions, so the
    route cannot revoke somebody else's even with a guessed digest.
    """
    mine = await store.list_for_user(me_.user.id)
    target = next((r for r in mine if session_handle(r.sid) == session_id), None)
    if target is None:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND,
            title="No such session",
            detail="That session has already ended.",
        )

    await store.revoke(target.sid, user_id=me_.user.id)
    write_audit(
        db,
        workspace_id=me_.workspace_id,
        actor_id=me_.user.id,
        action=AuditAction.SESSION_REVOKED,
        target_type=AuditTarget.SESSION,
        target_id=me_.user.id,
        meta={"session": session_id, "self": target.sid == me_.session.sid},
        ip=client_ip(request),
    )
    await db.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
