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

    LOGIN = "user.login"
    LOGIN_FAILED = "user.login_failed"
    LOGIN_LOCKED = "user.login_locked"
    LOGOUT = "user.logout"
    PASSWORD_CHANGED = "user.password_changed"  # noqa: S105 — an action name, not a secret
    SESSION_REVOKED = "user.session_revoked"

    PROJECT_CREATED = "project.created"
    PROJECT_UPDATED = "project.updated"

    CREDENTIAL_CREATED = "credential.created"
    CREDENTIAL_TESTED = "credential.tested"
    CREDENTIAL_DELETED = "credential.deleted"

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


class AuditTarget(StrEnum):
    WORKSPACE = "workspace"
    USER = "user"
    INVITE = "invite"
    SESSION = "session"
    RUN = "run"
    PROJECT = "project"
    CREDENTIAL = "credential"
    EVIDENCE = "evidence"
    EXPORT = "export"
    APPROVAL = "approval"


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
    failed login, and (from P4) the scheduler.
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
