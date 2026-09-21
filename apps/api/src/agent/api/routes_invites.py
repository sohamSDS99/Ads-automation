"""The two public invite routes.

Both are reachable without a session — they are how a session is first obtained
by anyone who is not the bootstrap admin. Neither leaks whether an address has
an account: an unknown token, a used token and an expired token are each
reported as what they are, and none of them says anything about a person.
"""

from __future__ import annotations

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import CSRF_COOKIE_NAME, client_ip, user_agent
from agent.api.routes_auth import _me, _set_session_cookies
from agent.api.schemas_auth import AcceptInviteRequest, InvitePreviewResponse, MeResponse
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import invites, passwords, workspaces
from agent.auth.deps import get_redis_client, get_session_store
from agent.auth.invites import InviteState
from agent.auth.ratelimit import LoginRateLimiter
from agent.auth.sessions import SessionStore
from agent.config import Settings, get_settings
from agent.db.models import UserStatus
from agent.db.repos import UserRepo, account_by_email, utcnow
from agent.db.session import get_session

log = structlog.get_logger(__name__)

router = APIRouter(tags=["invites"])

Db = Annotated[AsyncSession, Depends(get_session)]
Store = Annotated[SessionStore, Depends(get_session_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
RedisDep = Annotated[Redis, Depends(get_redis_client)]


@router.get(
    "/invites/{token}",
    response_model=InvitePreviewResponse,
    summary="What this invite link is for",
)
async def preview_invite(token: str, db: Db) -> InvitePreviewResponse:
    """Describe the invite so the page can render the right screen.

    Returns 200 with a `state` rather than a 404 for a bad token: the three
    failure modes are different screens, and a status code cannot carry which.
    """
    invite = await invites.find_by_token(db, token)
    state = invites.state_of(invite)
    if invite is None or state is not InviteState.VALID:
        return InvitePreviewResponse(state=state.value)

    workspace = await workspaces.get(db, invite.workspace_id)
    account = await account_by_email(db, invite.email)
    return InvitePreviewResponse(
        state=state.value,
        email=invite.email,
        role=invite.role,
        workspace_name=workspace.name if workspace else None,
        expires_at=invite.expires_at,
        # Whether *this* address already has a password, which the page needs
        # to know to ask the right question. It reveals nothing a holder of
        # the token did not already have: the token names the address.
        has_account=account is not None and account.password_hash is not None,
    )


@router.post(
    "/invites/{token}/accept",
    response_model=MeResponse,
    summary="Set a password and join",
)
async def accept_invite(
    token: str,
    body: AcceptInviteRequest,
    request: Request,
    response: Response,
    db: Db,
    store: Store,
    settings: SettingsDep,
    redis: RedisDep,
) -> MeResponse:
    """Consume the invite, set the password, and sign the new member in.

    `accepted_at` is stamped in the same transaction as the password, and the
    partial unique index on open invites means a second concurrent accept of the
    same link finds nothing to consume.
    """
    invite = await invites.find_by_token(db, token)
    state = invites.state_of(invite)
    if invite is None or state is not InviteState.VALID:
        raise problems.Problem(
            status_code=status.HTTP_410_GONE
            if state in (InviteState.EXPIRED, InviteState.ACCEPTED)
            else status.HTTP_404_NOT_FOUND,
            title="Invite unusable",
            detail={
                InviteState.EXPIRED: "This invite has expired. Ask an admin for a new link.",
                InviteState.ACCEPTED: "This invite has already been used. Sign in instead.",
                InviteState.INVALID: "This invite link is not valid.",
            }[state],
            type_=problems.TYPE_INVITE_INVALID,
            state=state.value,
        )

    member = await UserRepo(db, invite.workspace_id).by_email(invite.email)
    if member is None or member.membership.status is not UserStatus.INVITED:
        # The pending membership is written with the invite, so its absence
        # means this workspace was already joined or the person was removed.
        # Either way the link is spent.
        raise problems.Problem(
            status_code=status.HTTP_410_GONE,
            title="Invite unusable",
            detail="This invite has already been used. Sign in instead.",
            type_=problems.TYPE_INVITE_INVALID,
            state=InviteState.ACCEPTED.value,
        )

    user = member.user
    workspace = await workspaces.get(db, invite.workspace_id)
    if workspace is None or workspace.is_archived:  # pragma: no cover — defensive
        raise problems.Problem(
            status_code=status.HTTP_410_GONE,
            title="Invite unusable",
            detail="That workspace has been archived.",
            type_=problems.TYPE_INVITE_INVALID,
            state=InviteState.INVALID.value,
        )

    ip_for_limit = client_ip(request)
    joining_existing_account = user.password_hash is not None
    if joining_existing_account:
        # They already have an account, so this form is a confirmation, not a
        # signup. Accepting the token *and* a new password here would let
        # anyone holding a forwarded link overwrite the password of an account
        # that already works — the link grants this workspace, never the
        # account. Verifying instead is what keeps those two grants apart.
        # Rate-limited on the same counter as `/auth/login`, because it is the
        # same question asked in a different place. Without this, an admin who
        # invited a colleague from another workspace could guess at that
        # colleague's password here at full speed — a workspace admin is not
        # supposed to be able to learn an account credential.
        limiter = LoginRateLimiter(redis)
        lockout = await limiter.check(user.email, ip_for_limit)
        if lockout.locked:
            raise problems.Problem(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                title="Too many attempts",
                detail="Too many failed attempts. Try again shortly.",
                type_=problems.TYPE_RATE_LIMITED,
                headers={"Retry-After": str(lockout.retry_after_seconds)},
                retry_after_seconds=lockout.retry_after_seconds,
            )
        if not passwords.verify(user.password_hash or "", body.password):
            await limiter.record_failure(user.email, ip_for_limit)
            raise problems.Problem(
                status_code=status.HTTP_403_FORBIDDEN,
                title="Password incorrect",
                detail=(
                    "That is not the password for this account. Use the one you already "
                    "sign in with, or reset it first."
                ),
                type_=problems.TYPE_FORBIDDEN,
            )
        await limiter.clear(user.email, ip_for_limit)
    else:
        try:
            user.password_hash = passwords.hash_password(body.password)
        except passwords.PasswordPolicyError as exc:
            raise problems.Problem(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                title="Password rejected",
                detail=str(exc),
                type_=problems.TYPE_VALIDATION,
            ) from exc

    now = utcnow()
    if body.name:
        user.name = body.name
    if user.status is UserStatus.INVITED:
        user.status = UserStatus.ACTIVE
    user.last_login_at = now
    user.last_workspace_id = invite.workspace_id
    member.membership.status = UserStatus.ACTIVE
    invite.accepted_at = now

    ip = ip_for_limit
    write_audit(
        db,
        workspace_id=invite.workspace_id,
        actor_id=user.id,
        action=AuditAction.INVITE_ACCEPTED,
        target_type=AuditTarget.USER,
        target_id=user.id,
        meta={
            "email": user.email,
            "role": member.membership.role.value,
            "invite_id": str(invite.id),
            "existing_account": joining_existing_account,
        },
        ip=ip,
    )
    await db.commit()

    record = await store.create(
        user_id=user.id,
        workspace_id=invite.workspace_id,
        ip=ip,
        user_agent=user_agent(request),
    )
    _set_session_cookies(response, record, settings)
    # Accepting is also a sign-in, so the response carries the same shape as
    # /auth/login and the browser lands signed in rather than at /login.
    response.headers.setdefault("Cache-Control", "no-store")
    log.info("auth.invite_accepted", user_id=str(user.id), role=member.membership.role)
    return await _me(db, user, workspace=workspace, role=member.membership.role)


__all__ = ["router", "CSRF_COOKIE_NAME"]
