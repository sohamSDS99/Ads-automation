"""Optional SMTP delivery for invites and approval gates.

SMTP is a nice-to-have, never a dependency. With `SMTP_HOST` unset the app is
fully functional: `send_invite` returns the link and the API hands it back to
the admin to copy. With SMTP configured and the send failing, the behaviour is
identical — a mail server outage must never be able to block an invite, so
nothing in here raises (PRD §19.1 item 12).

The same rule governs approval notices, and PRD §16 states it directly: "SMTP
unconfigured or failing → invites and approval notices degrade to copyable
in-app links + inbox badges; never blocks the flow." A gate whose email did not
send is still a gate — it is in `GET /approvals` either way.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from email.message import EmailMessage

import structlog

from agent.config import Settings

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class InviteDelivery:
    """What happened to one invite email, and the link either way."""

    link: str
    delivered: bool
    reason: str | None = None


def _compose(*, to: str, link: str, workspace_name: str, inviter: str) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = f"You've been invited to {workspace_name}"
    message.set_content(
        f"{inviter} invited you to the {workspace_name} research workspace.\n\n"
        f"Set your password and sign in:\n{link}\n\n"
        "The link works once and expires in 7 days. "
        "If you weren't expecting this, you can ignore it.\n"
    )
    return message


async def send_invite(
    settings: Settings,
    *,
    to: str,
    link: str,
    workspace_name: str,
    inviter: str,
) -> InviteDelivery:
    """Try to email the invite. Always returns; the link is in the result either way."""
    if not settings.smtp_configured:
        log.info("invite.email_skipped", to=to, reason="smtp-not-configured")
        return InviteDelivery(link=link, delivered=False, reason="smtp-not-configured")

    message = _compose(to=to, link=link, workspace_name=workspace_name, inviter=inviter)
    message["From"] = settings.smtp_from or ""

    try:
        import aiosmtplib

        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=(
                settings.smtp_password.get_secret_value() if settings.smtp_password else None
            ),
            start_tls=settings.smtp_starttls,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — an invite must survive a broken mail server
        log.warning("invite.email_failed", to=to, error=str(exc))
        return InviteDelivery(link=link, delivered=False, reason="send-failed")

    log.info("invite.email_sent", to=to)
    return InviteDelivery(link=link, delivered=True)


def _compose_approval(*, to: str, link: str, project_name: str, node_name: str) -> EmailMessage:
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = f"Approval needed: {node_name} ({project_name})"
    message.set_content(
        f"A research run for {project_name} has paused and is waiting on you.\n\n"
        f"Step: {node_name}\n\n"
        f"Review the proposal and decide:\n{link}\n\n"
        "The run stays paused until someone decides — nothing is auto-approved.\n"
    )
    return message


async def send_approval_request(
    settings: Settings,
    *,
    to: str,
    link: str,
    project_name: str,
    node_name: str,
) -> InviteDelivery:
    """Tell an approver a gate is waiting. Always returns; never raises."""
    if not settings.smtp_configured:
        log.info("approval.email_skipped", to=to, reason="smtp-not-configured")
        return InviteDelivery(link=link, delivered=False, reason="smtp-not-configured")

    message = _compose_approval(to=to, link=link, project_name=project_name, node_name=node_name)
    message["From"] = settings.smtp_from or ""

    try:
        import aiosmtplib

        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=(
                settings.smtp_password.get_secret_value() if settings.smtp_password else None
            ),
            start_tls=settings.smtp_starttls,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — a gate must survive a broken mail server
        log.warning("approval.email_failed", to=to, error=str(exc))
        return InviteDelivery(link=link, delivered=False, reason="send-failed")

    log.info("approval.email_sent", to=to)
    return InviteDelivery(link=link, delivered=True)


def _humanise_wait(waiting_since: datetime | None, now: datetime) -> str:
    """How long this has been sitting, in the units a person would use."""
    if waiting_since is None:  # pragma: no cover — created_at has a server default
        return "a while"
    hours = max((now - waiting_since).total_seconds() / 3600, 0)
    if hours < 1:
        return "under an hour"
    if hours < 48:
        whole = int(round(hours))
        return f"{whole} hour" if whole == 1 else f"{whole} hours"
    days = int(hours // 24)
    return f"{days} days"


def _compose_reminder(
    *,
    to: str,
    link: str,
    project_name: str,
    node_name: str,
    milestone: str,
    due_at: datetime | None,
    waiting_since: datetime | None,
    now: datetime,
) -> EmailMessage:
    overdue = milestone == "due"
    message = EmailMessage()
    message["To"] = to
    message["Subject"] = (
        f"Still waiting: {node_name} ({project_name})"
        if overdue
        else f"Reminder: {node_name} ({project_name})"
    )
    waited = _humanise_wait(waiting_since, now)
    when = f"Your team's target for this was {due_at:%d %b, %H:%M UTC}.\n" if due_at else ""
    lead = (
        f"A research run for {project_name} has been paused for {waited}, "
        "which is past the time your team set for deciding it.\n"
        if overdue
        else f"A research run for {project_name} has been paused for {waited}, "
        "which is about half the time your team set for deciding it.\n"
    )
    message.set_content(
        f"{lead}\n"
        f"Step: {node_name}\n"
        f"{when}\n"
        f"Review the proposal and decide:\n{link}\n\n"
        "Nothing is auto-approved and nothing has been decided for you — "
        "the run stays paused until a person answers.\n"
    )
    return message


async def send_approval_reminder(
    settings: Settings,
    *,
    to: str,
    link: str,
    project_name: str,
    node_name: str,
    milestone: str,
    due_at: datetime | None,
    waiting_since: datetime | None,
    now: datetime,
) -> InviteDelivery:
    """Nudge an approver at 50% or 100% of the gate's SLA. Always returns; never raises."""
    if not settings.smtp_configured:
        log.info("approval.reminder_skipped", to=to, reason="smtp-not-configured")
        return InviteDelivery(link=link, delivered=False, reason="smtp-not-configured")

    message = _compose_reminder(
        to=to,
        link=link,
        project_name=project_name,
        node_name=node_name,
        milestone=milestone,
        due_at=due_at,
        waiting_since=waiting_since,
        now=now,
    )
    message["From"] = settings.smtp_from or ""

    try:
        import aiosmtplib

        await aiosmtplib.send(
            message,
            hostname=settings.smtp_host,
            port=settings.smtp_port,
            username=settings.smtp_user,
            password=(
                settings.smtp_password.get_secret_value() if settings.smtp_password else None
            ),
            start_tls=settings.smtp_starttls,
            timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — a reminder must survive a broken mail server
        log.warning("approval.reminder_failed", to=to, error=str(exc))
        return InviteDelivery(link=link, delivered=False, reason="send-failed")

    log.info("approval.reminder_sent", to=to, milestone=milestone)
    return InviteDelivery(link=link, delivered=True)
