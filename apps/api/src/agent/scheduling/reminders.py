"""Approval SLA reminders, at half the allowance and at all of it.

PRD §16: "Approval never answered → run sits in `awaiting_approval`
indefinitely; assignee's inbox badge + reminder email at 50% and 100% of SLA;
admin can reassign. **No auto-approve** — a gate exists because a human must
decide."

That last sentence sets the shape of everything here. A reminder changes nothing
about the gate: the run stays parked, the approval stays `pending`, and the
100% reminder is a second nudge rather than a deadline. An SLA in this product
is a promise the workspace made to itself, not a timer that decides on a
person's behalf.

Two rules keep it from becoming noise:

* **Each milestone fires once, ever.** The record is on the approval row
  (`approval.reminders_sent`), not in Redis, because a reminder that is re-sent
  after a Redis flush reads to the recipient as the system being broken.
* **A reminder is never the reason a run fails.** SMTP is optional in this
  product (PRD §16, "SMTP unconfigured or failing"), so a send that does not
  happen is logged, recorded as sent, and the inbox badge carries the signal —
  which is exactly the degradation the invite flow already uses.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.config import Settings, get_settings
from agent.db.models import Approval, ApprovalStatus, Project, Run, User
from agent.notify.email import send_approval_reminder
from agent.orchestrator import approvals as gate_api
from agent.orchestrator.registry import get_registry

log = structlog.get_logger(__name__)

#: The two milestones, as a fraction of the allowance. Named rather than
#: computed so the stored record reads as "half" and "due", not as "0.5".
MILESTONES: tuple[tuple[str, float], ...] = (("half", 0.5), ("due", 1.0))


@dataclass(frozen=True, slots=True)
class ReminderOutcome:
    sent: tuple[tuple[uuid.UUID, str], ...] = ()
    #: Gates past 100% whose recipients could not be resolved at all.
    unaddressed: tuple[uuid.UUID, ...] = ()

    @property
    def total(self) -> int:
        return len(self.sent)


async def send_due_reminders(
    db: AsyncSession, *, now: datetime | None = None, settings: Settings | None = None
) -> ReminderOutcome:
    """Nudge every pending gate that has crossed a milestone it has not been nudged for."""
    moment = now or datetime.now(UTC)
    config = settings or get_settings()

    sent: list[tuple[uuid.UUID, str]] = []
    unaddressed: list[uuid.UUID] = []

    # Ids, not rows. `_record` commits, which expires every object in the
    # session — so a second pending gate held from the first query would fire a
    # lazy load on the next iteration, and a `MissingGreenlet` raised inside a
    # cron job is caught by nothing. Each gate is re-read in its own turn.
    for approval_id in await _pending_with_sla(db, moment):
        approval = await db.get(Approval, approval_id)
        if approval is None:  # pragma: no cover — it was there a moment ago
            continue
        milestone = _crossed(approval, moment)
        if milestone is None:  # pragma: no cover — the query already filtered on this
            continue
        run = await db.get(Run, approval.run_id)
        if run is None:  # pragma: no cover — FK is ON DELETE CASCADE
            continue

        recipients = await gate_api.notify_targets(db, approval, run.workspace_id)
        if not recipients:
            # Nobody holds the role any more. Recorded so it is not retried
            # every minute, and warned about because a gate nobody can answer
            # holds its run until an admin reassigns it (PRD §16).
            log.warning(
                "approval.reminder_unaddressed",
                approval_id=str(approval.id),
                required_role=approval.required_role.value,
                milestone=milestone,
            )
            unaddressed.append(approval.id)
            await _record(db, approval, run, milestone, delivered=False, recipients=[])
            continue

        context = await _context(db, approval, run)
        delivered = await _deliver(config, approval, recipients, context, milestone, moment)
        await _record(
            db,
            approval,
            run,
            milestone,
            delivered=delivered,
            recipients=[user.email for user in recipients],
        )
        # `approval_id`, not `approval.id`: `_record` has committed and the
        # instance is expired.
        sent.append((approval_id, milestone))

    outcome = ReminderOutcome(sent=tuple(sent), unaddressed=tuple(unaddressed))
    if outcome.total or outcome.unaddressed:
        log.info(
            "approval.reminders",
            sent=outcome.total,
            unaddressed=len(outcome.unaddressed),
        )
    return outcome


async def _pending_with_sla(db: AsyncSession, moment: datetime) -> list[uuid.UUID]:
    """Ids of pending gates whose allowance has at least half run out."""
    result = await db.execute(
        sa.select(Approval).where(
            Approval.status == ApprovalStatus.PENDING,
            Approval.due_at.is_not(None),
        )
    )
    rows = list(result.scalars().all())
    return [row.id for row in rows if _crossed(row, moment) is not None]


def _crossed(approval: Approval, moment: datetime) -> str | None:
    """The highest milestone this gate has reached and not yet been nudged for.

    Highest, not earliest: a worker that was down for a day should send the
    "due" reminder, not the "half" one followed by the "due" one a minute later.
    Skipping straight to `due` marks `half` as handled too, so the earlier
    milestone can never fire retroactively.
    """
    if approval.due_at is None:
        return None
    already = _already_sent(approval)
    elapsed = _elapsed_fraction(approval, moment)
    if elapsed is None:
        return None
    for name, fraction in reversed(MILESTONES):
        if elapsed >= fraction and name not in already:
            return name
    return None


def _elapsed_fraction(approval: Approval, moment: datetime) -> float | None:
    """How far through the allowance we are, where 1.0 is `due_at`.

    `created_at` is the start of the allowance and `due_at` its end, so the
    fraction is derived from the row rather than from re-reading the project's
    SLA — which an admin may have changed since the gate opened.
    """
    if approval.due_at is None or approval.created_at is None:
        return None
    window = (approval.due_at - approval.created_at).total_seconds()
    if window <= 0:
        return 1.0
    return (moment - approval.created_at).total_seconds() / window


def _already_sent(approval: Approval) -> set[str]:
    """Milestones already nudged for. Tolerant of a hand-edited column.

    Widened to `object` before the check on purpose: the column is typed
    `list[str]`, so mypy calls the guard unreachable — but JSONB holds whatever
    psql was last pointed at it, and a `null` there should cost a reminder
    rather than raise inside a cron job.
    """
    stored: object = approval.reminders_sent
    if not isinstance(stored, list):
        return set()
    return {str(item) for item in stored}


async def _record(
    db: AsyncSession,
    approval: Approval,
    run: Run,
    milestone: str,
    *,
    delivered: bool,
    recipients: list[str],
) -> None:
    """Mark this milestone — and every earlier one — as handled, and audit it."""
    handled = _already_sent(approval) | _upto(milestone)
    # Reassigned, not mutated: SQLAlchemy does not track in-place edits of a
    # JSONB list, so appending to it would leave the column unchanged and the
    # same reminder would go out again on the next tick.
    approval.reminders_sent = sorted(handled)
    # Read before the commit below expires the instance.
    approval_id, node_id, run_id = approval.id, approval.node_id, approval.run_id

    write_audit(
        db,
        workspace_id=run.workspace_id,
        actor_id=None,
        action=AuditAction.APPROVAL_REMINDED,
        target_type=AuditTarget.APPROVAL,
        target_id=approval_id,
        meta={
            "milestone": milestone,
            "delivered": delivered,
            "recipients": recipients,
            "node_id": node_id,
            "run_id": str(run_id),
        },
    )
    await db.commit()


def _upto(milestone: str) -> set[str]:
    """This milestone and every earlier one."""
    names = [name for name, _ in MILESTONES]
    return set(names[: names.index(milestone) + 1])


@dataclass(frozen=True, slots=True)
class ReminderContext:
    project_name: str
    node_name: str


async def _context(db: AsyncSession, approval: Approval, run: Run) -> ReminderContext:
    """The two names the email needs, resolved the way the console resolves them."""
    project = await db.get(Project, run.project_id)
    spec = next(
        (item for item in get_registry().specs() if item.id == approval.node_id),
        None,
    )
    return ReminderContext(
        project_name=project.name if project else "a project",
        # Falls back to the node id rather than to a placeholder: an approver
        # who has seen the console recognises "1.1.5", and "a step" tells them
        # nothing about which question is waiting.
        node_name=spec.name if spec else approval.node_id,
    )


async def _deliver(
    settings: Settings,
    approval: Approval,
    recipients: list[User],
    context: ReminderContext,
    milestone: str,
    moment: datetime,
) -> bool:
    link = f"{settings.app_base_url.rstrip('/')}/approvals/{approval.id}"
    delivered = False
    for user in recipients:
        result = await send_approval_reminder(
            settings,
            to=user.email,
            link=link,
            project_name=context.project_name,
            node_name=context.node_name,
            milestone=milestone,
            due_at=approval.due_at,
            waiting_since=approval.created_at,
            now=moment,
        )
        delivered = delivered or result.delivered
    return delivered
