"""Sign in, sign out, and everything a user does to their own account."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable
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
    ReauthRequest,
    ReauthResponse,
    SessionListResponse,
    SessionSummary,
    SwitchWorkspaceRequest,
    UserSummary,
    WorkspaceMembershipSummary,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import bootstrap, passwords, workspaces
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
from agent.db.models import User, UserRole, UserStatus, Workspace
from agent.db.repos import account_by_email, utcnow
from agent.db.session import get_session
from agent.guidelines.signature import ReauthTokens

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


async def _me(
    db: AsyncSession,
    user: User,
    *,
    workspace: Workspace,
    role: UserRole,
    via_superadmin: bool = False,
) -> MeResponse:
    """The whole of what the shell needs to render, in one round trip.

    The switcher is part of it. Fetching the workspace list separately would
    mean the first paint after a sign-in has a workspace name and no way to
    leave it, and the shell would flash a single-workspace layout at people
    who have six.
    """
    reachable = await workspaces.list_for_user(db, user)
    return MeResponse(
        id=user.id,
        email=user.email,
        name=user.name,
        role=role,
        permissions=sorted(
            permissions_for(role, superadmin=user.is_superadmin), key=lambda p: p.value
        ),
        workspace_id=workspace.id,
        workspace_name=workspace.name,
        is_superadmin=user.is_superadmin,
        via_superadmin=via_superadmin,
        workspaces=[
            WorkspaceMembershipSummary(
                id=entry.workspace.id,
                name=entry.workspace.name,
                role=entry.role,
                is_member=entry.is_member,
            )
            for entry in reachable
        ],
    )


def _me_from(db: AsyncSession, principal: Principal) -> Awaitable[MeResponse]:
    return _me(
        db,
        principal.user,
        workspace=principal.workspace,
        role=principal.role,
        via_superadmin=principal.via_superadmin,
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
    # Built rather than validated from the row: `role` and `status` on this
    # response are the membership's, and the account object no longer carries
    # either. The first admin's are known without a query.
    return UserSummary(
        id=admin.id,
        email=admin.email,
        name=admin.name,
        role=UserRole.ADMIN,
        status=UserStatus.ACTIVE,
        last_login_at=admin.last_login_at,
        created_at=admin.created_at,
    )


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

    # The account, not a member: the password lives on the account and which
    # workspace this person lands in is a separate question, answered below
    # only once they have proved who they are.
    user = await account_by_email(db, body.email)

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

    if user is None:  # pragma: no cover — narrowed above
        raise problems.unauthenticated()

    # Credentials were correct, so this is no longer a sign-in failure however
    # it ends. An account with nowhere to go is a real state — every
    # membership revoked, or a workspace archived out from under them — and
    # saying so is the only way the person knows to ask someone for access
    # rather than to keep retrying a password that works.
    workspace_id = await workspaces.default_workspace_id(db, user)
    if workspace_id is None:
        await limiter.clear(body.email, ip)
        # Recorded, because it is the visible half of a decision somebody
        # made: an admin removed this person's last workspace, and the only
        # evidence that it landed is them turning up at the door.
        await _audit_login_failure(db, AuditAction.LOGIN_NO_WORKSPACE, body.email, ip)
        raise problems.Problem(
            status_code=status.HTTP_403_FORBIDDEN,
            title="No workspace",
            detail=(
                "Your sign-in worked, but you are not a member of any workspace. "
                "Ask an administrator to add you to one."
            ),
            type_=problems.TYPE_FORBIDDEN,
        )
    access = await workspaces.resolve_access(db, user=user, workspace_id=workspace_id)

    if passwords.needs_rehash(stored_hash):
        user.password_hash = passwords.hash_password(body.password)

    await limiter.clear(body.email, ip)
    user.last_login_at = utcnow()
    user.last_workspace_id = access.workspace.id
    record = await store.create(
        user_id=user.id,
        workspace_id=access.workspace.id,
        ip=ip,
        user_agent=user_agent(request),
    )
    write_audit(
        db,
        workspace_id=access.workspace.id,
        actor_id=user.id,
        action=AuditAction.LOGIN,
        target_type=AuditTarget.USER,
        target_id=user.id,
        meta={"via_superadmin": True} if access.via_superadmin else {},
        ip=ip,
    )
    await db.commit()

    _set_session_cookies(response, record, settings)
    log.info("auth.login", user_id=str(user.id), role=access.role)
    return await _me(
        db,
        user,
        workspace=access.workspace,
        role=access.role,
        via_superadmin=access.via_superadmin,
    )


@router.post("/auth/workspace", response_model=MeResponse, summary="Switch workspace")
async def switch_workspace(
    me_: SignedIn,
    body: SwitchWorkspaceRequest,
    request: Request,
    db: Db,
    store: Store,
    settings: SettingsDep,
    response: Response,
) -> MeResponse:
    """Move this browser into another workspace.

    The session is replaced rather than edited. Changing the workspace id in
    place would leave one session id spanning two tenants in every log, cache
    key and reconnecting SSE stream that captured it earlier in the request's
    life; a new id makes "which workspace was this session in" answerable from
    the id alone. Their other browsers are left where they are — switching tab
    A is not a statement about tab B.
    """
    try:
        access = await workspaces.resolve_access(db, user=me_.user, workspace_id=body.workspace_id)
    except workspaces.WorkspaceAccessError as exc:
        raise problems.Problem(
            status_code=status.HTTP_404_NOT_FOUND
            if exc.reason == "missing"
            else status.HTTP_403_FORBIDDEN,
            title="Workspace unavailable",
            detail=exc.detail,
            type_=problems.TYPE_FORBIDDEN,
        ) from exc

    if access.workspace.id == me_.workspace_id:
        return await _me_from(db, me_)

    ip = client_ip(request)
    me_.user.last_workspace_id = access.workspace.id
    write_audit(
        db,
        workspace_id=access.workspace.id,
        actor_id=me_.user.id,
        action=AuditAction.WORKSPACE_ENTERED,
        target_type=AuditTarget.WORKSPACE,
        target_id=access.workspace.id,
        meta={"from": str(me_.workspace_id), "via_superadmin": access.via_superadmin},
        ip=ip,
    )
    await db.commit()

    await store.revoke(me_.session.sid, user_id=me_.user.id)
    record = await store.create(
        user_id=me_.user.id,
        workspace_id=access.workspace.id,
        ip=ip,
        user_agent=user_agent(request),
    )
    _set_session_cookies(response, record, settings)
    log.info(
        "auth.workspace_switched",
        user_id=str(me_.user.id),
        workspace_id=str(access.workspace.id),
        via_superadmin=access.via_superadmin,
    )
    return await _me(
        db,
        me_.user,
        workspace=access.workspace,
        role=access.role,
        via_superadmin=access.via_superadmin,
    )


async def _audit_login_failure(
    db: AsyncSession, action: AuditAction, email: str, ip: str | None
) -> None:
    """Record a failed attempt, if there is a workspace to record it against.

    Committed on its own: the request is about to raise, and an audit row that
    rolled back with the failure would leave no trace of the attempt.

    A failed sign-in has no workspace of its own — nobody has proved who they
    are yet — so it lands in the oldest one, which is the installation's own
    log. Filing it against a guessed account's workspace would leak, by the
    row's very location, which workspace that address belongs to.
    """
    workspace = await bootstrap.first_workspace(db)
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
    return await _me_from(db, me_)


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


@router.post(
    "/auth/reauth",
    response_model=ReauthResponse,
    summary="Prove presence before a non-delegable act",
)
async def reauth(
    me_: SignedIn,
    body: ReauthRequest,
    request: Request,
    db: Db,
    redis: RedisDep,
    settings: SettingsDep,
) -> ReauthResponse:
    """Mint a single-use, short-lived proof that the caller is present (PRD §5.2 layer 3).

    A session says somebody signed in at some point. A signature needs the
    person to be here *now*, so `CLAIM_SIGN` and `ATTEST_SUBMIT` demand a token
    minted against the current password within the last
    `SIGNATURE_REAUTH_TTL_SECONDS`.

    This endpoint is a password oracle by construction, so it carries the same
    lockout the sign-in path does. Without it, an attacker holding a stolen
    session cookie could grind the password here at no cost — and the prize is
    the one permission an administrator cannot exercise.
    """
    limiter = LoginRateLimiter(redis)
    ip = client_ip(request)
    state = await limiter.check(me_.user.email, ip)
    if state.locked:
        raise _lockout_problem(state)

    if not passwords.verify(me_.user.password_hash or passwords.dummy_hash(), body.password):
        await limiter.record_failure(me_.user.email, ip)
        await _audit_login_failure(db, AuditAction.LOGIN_FAILED, me_.user.email, ip)
        await db.commit()
        raise problems.Problem(
            status_code=status.HTTP_401_UNAUTHORIZED,
            title="Password incorrect",
            detail="That password is not correct.",
            type_=problems.TYPE_UNAUTHENTICATED,
        )

    tokens = ReauthTokens(redis, ttl_seconds=settings.signature_reauth_ttl_seconds)
    token, _token_id = await tokens.mint(me_.user.id)
    return ReauthResponse(token=token, expires_in=settings.signature_reauth_ttl_seconds)
