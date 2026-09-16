"""Optional SMTP delivery for invites.

SMTP is a nice-to-have, never a dependency. With `SMTP_HOST` unset the app is
fully functional: `send_invite` returns the link and the API hands it back to
the admin to copy. With SMTP configured and the send failing, the behaviour is
identical — a mail server outage must never be able to block an invite, so
nothing in here raises (PRD §19.1 item 12).
"""

from __future__ import annotations

from dataclasses import dataclass
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
