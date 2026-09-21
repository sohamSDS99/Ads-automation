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
