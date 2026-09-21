"""The audit trail. One row per mutating action, in the acting transaction.

`write_audit` deliberately does not commit. It stages the row on the caller's
session so the audit entry and the change it describes share a transaction: if
the change rolls back the record of it goes too, and there is no window in which
one exists without the other (PRD §6.1, Authorization 5).
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AuditLog


class AuditAction(StrEnum):
    """Every action P0b can record. Later phases extend this, never reuse loosely."""

    BOOTSTRAP = "workspace.bootstrap"
    WORKSPACE_UPDATED = "workspace.updated"

    # Multi-workspace. Each of these is written to the log of the workspace it
    # happened to, including `WORKSPACE_ENTERED`: "who came in here, and when"
    # is a question that belongs to the workspace being entered, not to the
    # one the visitor came from.
    WORKSPACE_CREATED = "workspace.created"
    WORKSPACE_ARCHIVED = "workspace.archived"
    WORKSPACE_RESTORED = "workspace.restored"
    WORKSPACE_ENTERED = "workspace.entered"
    MEMBER_REMOVED = "user.removed_from_workspace"
    SUPERADMIN_GRANTED = "user.superadmin_granted"
    SUPERADMIN_REVOKED = "user.superadmin_revoked"
    ACCOUNT_STATUS_CHANGED = "user.account_status_changed"

    LOGIN = "user.login"
    LOGIN_FAILED = "user.login_failed"
    LOGIN_LOCKED = "user.login_locked"
    #: Correct credentials, nowhere to go — every membership revoked or every
    #: workspace archived. Not a failure of authentication, so it is its own
    #: action rather than another `LOGIN_FAILED`.
    LOGIN_NO_WORKSPACE = "user.login_no_workspace"
    LOGOUT = "user.logout"
    PASSWORD_CHANGED = "user.password_changed"  # noqa: S105 — an action name, not a secret
    SESSION_REVOKED = "user.session_revoked"

    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"

    # Sources. Not `credential.*`: nobody stores a credential any more, and the
    # recorded act is the decision to *use* one the deployment already holds.
    # The three retired `credential.*` actions are not reused for it — an audit
    # log read a year from now must not show a key being stored on a build that
    # could not store one.
    SOURCE_CONNECTED = "source.connected"
    SOURCE_DISCONNECTED = "source.disconnected"
    SOURCE_TESTED = "source.tested"

    RUN_LAUNCHED = "run.launched"
    RUN_CANCELLED = "run.cancelled"
    RUN_RETRIED = "run.retried"

    USER_INVITED = "user.invited"
    INVITE_ACCEPTED = "user.invite_accepted"
    USER_ROLE_CHANGED = "user.role_changed"
    USER_STATUS_CHANGED = "user.status_changed"

    # P2 — connectors and evidence.
    CSV_UPLOADED = "source.csv_uploaded"
    EVIDENCE_WRITTEN = "evidence.written"

    # Business-context documents. Separate from `CSV_UPLOADED`, which records a
    # mapped CRM import: these two write different evidence for different
    # reasons, and an admin reading the log should be able to tell which
    # happened without opening `meta`.
    DOCUMENT_UPLOADED = "project.document_uploaded"
    DOCUMENT_DELETED = "project.document_deleted"

    # P5a — report exports. Requesting one is the recorded act; downloading the
    # same file twice is not a second decision, and a row per click would bury
    # the log the admin actually reads.
    EXPORT_REQUESTED = "export.requested"
    # P3 — approval gates. `APPROVAL_REQUESTED` is the one action here with no
    # human actor: the executor opens the gate, and the row exists so the audit
    # log tells the whole story of a decision rather than only its second half.
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_DECIDED = "approval.decided"
    APPROVAL_REASSIGNED = "approval.reassigned"

    # P8 — unattended work. `RUN_REAPED` and `BACKUP_WRITTEN` have no human
    # actor by construction, which is the case §15 NF5c carves out: an action
    # still resolves to *something*, and `meta` says which job it was.
    SCHEDULE_CREATED = "schedule.created"
    SCHEDULE_UPDATED = "schedule.updated"
    SCHEDULE_DELETED = "schedule.deleted"
    RUN_REAPED = "run.reaped"
    APPROVAL_REMINDED = "approval.reminded"
    BACKUP_WRITTEN = "backup.written"
    RETENTION_PRUNED = "retention.pruned"


class AuditTarget(StrEnum):
    WORKSPACE = "workspace"
    USER = "user"
    INVITE = "invite"
    SESSION = "session"
    RUN = "run"
    PROJECT = "project"
    SOURCE = "source"
    EVIDENCE = "evidence"
    EXPORT = "export"
    APPROVAL = "approval"
    SCHEDULE = "schedule"
    BACKUP = "backup"


def write_audit(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    action: AuditAction,
    target_type: AuditTarget,
    actor_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    meta: dict[str, Any] | None = None,
    ip: str | None = None,
) -> AuditLog:
    """Stage an audit row. The caller commits it along with its own change.

    `actor_id` is None only for actions with no signed-in actor: bootstrap, a
    failed login, and (from P8) every job that runs on the worker's cron —
    scheduled runs, the reaper, SLA reminders, backups and retention.
    """
    row = AuditLog(
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=action.value,
        target_type=target_type.value,
        target_id=target_id,
        meta=meta or {},
        ip=ip,
    )
    db.add(row)
    return row
