"""Creating a profile and the single-use link that turns it into an account.

One function, three callers: the Team screen inviting into the workspace you
are in, the system administrator adding somebody to any workspace, and the
founding admin handed a brand-new workspace. All three used to be near-copies
of each other, and the copies had already started to drift — only one of them
improved a placeholder name, only one of them refused an archived workspace.

The shape of the thing is worth stating once, because every caller depends on
all of it:

* **An address gets one account, ever.** If the email is new an account is
  created holding no password; if it is not, the existing account is reused
  and only a membership is added. `user.email` is unique, so this is enforced
  by the database rather than by the branch below.
* **The membership is written now**, as `invited`. The admin sees the pending
  person in the member list immediately, and `accept_invite` has a row to
  consume — it will not activate a membership that was never opened.
* **The commit happens here, before the email is sent.** The invite exists
  whether or not the mail server does, and a link nobody received can still be
  copied out of the response. That ordering is the whole reason this function
  owns its transaction rather than leaving it to the caller.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth import invites, workspaces
from agent.config import Settings
from agent.db.models import Invite, User, UserRole, UserStatus, Workspace
from agent.db.repos import InviteRepo, UserRepo, account_by_email
from agent.notify.email import send_invite

log = structlog.get_logger(__name__)


class InvitationError(RuntimeError):
    """The invite cannot be created. Carries what to say and how to say it."""

    def __init__(self, detail: str, *, title: str) -> None:
        super().__init__(detail)
        self.detail = detail
        self.title = title


@dataclass(frozen=True, slots=True)
class Invitation:
    """What was created, and what became of the email."""

    invite: Invite
    account: User
    workspace: Workspace
    role: UserRole
    link: str
    #: True when the address already had a usable account — they are joining a
    #: second workspace rather than signing up, and the link will ask them to
    #: confirm with the password they already have.
    had_account: bool
    email_delivered: bool


def default_name(email: str) -> str:
    """A stand-in until the person tells us what they are called.

    The local part with its separators turned into spaces — `jo.patel` becomes
    "Jo Patel" — because a member list of raw addresses is unreadable, and an
    admin should not have to invent a colleague's name to add them. Whatever
    this produces is replaced the moment they accept.
    """
    local = email.split("@", 1)[0]
    words = [
        part for part in local.replace(".", " ").replace("_", " ").replace("-", " ").split() if part
    ]
    return " ".join(word[:1].upper() + word[1:] for word in words) or email


def invite_link(settings: Settings, token: str) -> str:
    return f"{settings.app_base_url}/invite/{token}"


async def invite_member(
    db: AsyncSession,
    *,
    workspace: Workspace,
    email: str,
    role: UserRole,
    invited_by: User,
    settings: Settings,
    name: str | None = None,
    ip: str | None = None,
    audit_meta: dict[str, object] | None = None,
) -> Invitation:
    """Create the profile, the pending membership and the link. Commits.

    Raises `InvitationError` for every refusal a person could cause, so the
    route layer turns one exception type into a problem document instead of
    re-deriving the same four conflicts at each call site.
    """
    if workspace.is_archived:
        raise InvitationError(
            f"{workspace.name} is archived. Restore it before adding people to it.",
            title="Workspace archived",
        )

    user_repo = UserRepo(db, workspace.id)
    invite_repo = InviteRepo(db, workspace.id)

    existing = await user_repo.by_email(email)
    if existing is not None and existing.membership.status is UserStatus.INVITED:
        raise InvitationError(
            f"{email} already has an unaccepted invite to {workspace.name}.",
            title="Invite already open",
        )
    if existing is not None:
        raise InvitationError(
            f"{email} is already a member of {workspace.name}.", title="Already a member"
        )
    if await invite_repo.open_for_email(email) is not None:
        raise InvitationError(
            f"{email} already has an unaccepted invite to {workspace.name}.",
            title="Invite already open",
        )

    token = invites.new_token()
    account = await account_by_email(db, email)
    if account is None:
        account = User(
            email=email,
            name=name or default_name(email),
            password_hash=None,
            status=UserStatus.INVITED,
        )
        db.add(account)
        await db.flush()
    elif name and account.status is UserStatus.INVITED:
        # They have never signed in, so the placeholder is still a placeholder
        # and an admin who bothered to type a name should get to improve it.
        account.name = name

    workspaces.add_member(
        db,
        workspace_id=workspace.id,
        user_id=account.id,
        role=role,
        status=UserStatus.INVITED,
        invited_by=invited_by.id,
    )
    invite_repo.add(
        invites.build(
            workspace_id=workspace.id,
            email=email,
            role=role,
            invited_by=invited_by.id,
            token=token,
        )
    )

    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise InvitationError(
            f"{email} already has an account or an open invite.", title="Already invited"
        ) from exc

    invite = await invite_repo.open_for_email(email)
    if invite is None:  # pragma: no cover — just flushed
        raise InvitationError("The invite could not be created.", title="Invite failed")

    had_account = account.password_hash is not None
    write_audit(
        db,
        workspace_id=workspace.id,
        actor_id=invited_by.id,
        action=AuditAction.USER_INVITED,
        target_type=AuditTarget.INVITE,
        target_id=invite.id,
        meta={
            "email": email,
            "role": role.value,
            "existing_account": had_account,
            **(audit_meta or {}),
        },
        ip=ip,
    )
    await db.commit()

    link = invite_link(settings, token)
    delivery = await send_invite(
        settings,
        to=email,
        link=link,
        workspace_name=workspace.name,
        inviter=invited_by.name,
    )
    log.info(
        "invite.created",
        email=email,
        workspace_id=str(workspace.id),
        role=role.value,
        existing_account=had_account,
        email_delivered=delivery.delivered,
    )
    return Invitation(
        invite=invite,
        account=account,
        workspace=workspace,
        role=role,
        link=link,
        had_account=had_account,
        email_delivered=delivery.delivered,
    )


async def void_open_invite(
    db: AsyncSession, *, workspace_id: uuid.UUID, email: str
) -> Invite | None:
    """Delete the open invite for this address in this workspace, if there is one.

    Deleted rather than flagged, and that is the security posture rather than
    laziness: the row's only content is the SHA-256 of a live credential, and
    a superseded credential's hash has no reason to stay in the database.
    What happened is in `audit_log`, which is where history belongs.

    It also has to be a delete because of `uq_invite_open_email` — unique on
    `(workspace_id, email) WHERE accepted_at IS NULL`. Leaving the row behind
    is what made removing a pending person burn their address permanently:
    every later invite to it answered 409 "Invite already open" against an
    invite whose membership no longer existed.
    """
    invite = await InviteRepo(db, workspace_id).open_for_email(email)
    if invite is not None:
        await db.delete(invite)
        await db.flush()
    return invite


async def reissue_invite(
    db: AsyncSession,
    *,
    workspace: Workspace,
    member: User,
    invited_by: User,
    role: UserRole,
    settings: Settings,
    ip: str | None = None,
    audit_meta: dict[str, object] | None = None,
) -> Invitation:
    """Mint a fresh link for somebody who has not accepted yet. Commits.

    The reason this exists is that the token is stored only as a hash, so a
    lost link cannot be looked up — `agent.auth.invites` says exactly that and
    then nothing implemented the other half. When the link *is* the delivery
    mechanism, as it is on an internal tool with no mail server, a link the
    admin failed to copy left the person stranded in `invited` forever with no
    way forward and no way back.

    The previous link stops working the moment this returns. That is the point
    of a single-use token and it is stated on the screen: two live links to one
    account is one more than anybody intended.
    """
    if workspace.is_archived:
        raise InvitationError(
            f"{workspace.name} is archived. Restore it before inviting anyone into it.",
            title="Workspace archived",
        )

    membership = await workspaces.membership_for(db, workspace_id=workspace.id, user_id=member.id)
    if membership is None:
        raise InvitationError(
            f"{member.email} is not a member of {workspace.name}.", title="Not a member"
        )
    if membership.status is not UserStatus.INVITED:
        raise InvitationError(
            f"{member.email} has already accepted. There is no link to reissue — "
            "they sign in with the password they set.",
            title="Already accepted",
        )

    superseded = await void_open_invite(db, workspace_id=workspace.id, email=member.email)

    token = invites.new_token()
    db.add(
        invites.build(
            workspace_id=workspace.id,
            email=member.email,
            role=role,
            invited_by=invited_by.id,
            token=token,
        )
    )
    await db.flush()

    invite = await InviteRepo(db, workspace.id).open_for_email(member.email)
    if invite is None:  # pragma: no cover — just flushed
        raise InvitationError("The link could not be reissued.", title="Reissue failed")

    write_audit(
        db,
        workspace_id=workspace.id,
        actor_id=invited_by.id,
        action=AuditAction.INVITE_REISSUED,
        target_type=AuditTarget.INVITE,
        target_id=invite.id,
        meta={
            "email": member.email,
            "role": role.value,
            "superseded": str(superseded.id) if superseded is not None else None,
            **(audit_meta or {}),
        },
        ip=ip,
    )
    await db.commit()

    link = invite_link(settings, token)
    delivery = await send_invite(
        settings,
        to=member.email,
        link=link,
        workspace_name=workspace.name,
        inviter=invited_by.name,
    )
    log.info(
        "invite.reissued",
        email=member.email,
        workspace_id=str(workspace.id),
        superseded=str(superseded.id) if superseded is not None else None,
    )
    return Invitation(
        invite=invite,
        account=member,
        workspace=workspace,
        role=role,
        link=link,
        had_account=member.password_hash is not None,
        email_delivered=delivery.delivered,
    )
