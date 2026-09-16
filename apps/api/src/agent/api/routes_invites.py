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
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import CSRF_COOKIE_NAME, client_ip, user_agent
from agent.api.routes_auth import _me, _set_session_cookies
from agent.api.schemas_auth import AcceptInviteRequest, InvitePreviewResponse, MeResponse
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import bootstrap, invites, passwords
from agent.auth.deps import get_session_store
from agent.auth.invites import InviteState
from agent.auth.sessions import SessionStore
from agent.config import Settings, get_settings
from agent.db.models import UserStatus
from agent.db.repos import UserRepo, utcnow
from agent.db.session import get_session

log = structlog.get_logger(__name__)

router = APIRouter(tags=["invites"])

Db = Annotated[AsyncSession, Depends(get_session)]
Store = Annotated[SessionStore, Depends(get_session_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


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

    workspace = await bootstrap.get_workspace(db)
    return InvitePreviewResponse(
        state=state.value,
        email=invite.email,
        role=invite.role,
        workspace_name=workspace.name if workspace else None,
        expires_at=invite.expires_at,
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

    repo = UserRepo(db, invite.workspace_id)
    user = await repo.by_email(invite.email)
    if user is None or user.status is not UserStatus.INVITED:
        # The pending row is written with the invite, so its absence means the
        # account was already set up or removed. Either way the link is spent.
        raise problems.Problem(
            status_code=status.HTTP_410_GONE,
            title="Invite unusable",
            detail="This invite has already been used. Sign in instead.",
            type_=problems.TYPE_INVITE_INVALID,
            state=InviteState.ACCEPTED.value,
        )

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
    user.name = body.name
    user.status = UserStatus.ACTIVE
    user.last_login_at = now
    invite.accepted_at = now

    ip = client_ip(request)
    write_audit(
        db,
        workspace_id=invite.workspace_id,
        actor_id=user.id,
        action=AuditAction.INVITE_ACCEPTED,
        target_type=AuditTarget.USER,
        target_id=user.id,
        meta={"email": user.email, "role": user.role.value, "invite_id": str(invite.id)},
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
    workspace = await bootstrap.get_workspace(db)
    log.info("auth.invite_accepted", user_id=str(user.id), role=user.role)
    return _me(user, workspace.name if workspace else "")


__all__ = ["router", "CSRF_COOKIE_NAME"]
