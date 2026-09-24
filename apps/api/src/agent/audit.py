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

    # Stage 02. Four rows, because PRD §17 PS4 requires acceptance, the start,
    # the override and the freeze each to be separately answerable later.
    RESEARCH_ACCEPTED = "research.accepted"
    RESEARCH_ACCEPTANCE_SUPERSEDED = "research.acceptance_superseded"
    #: Carries `override_reason`. Written *alongside* RESEARCH_ACCEPTED rather
    #: than instead of it: "who overrode a no_go, and what did they write" is a
    #: question with its own audience.
    RESEARCH_NO_GO_OVERRIDDEN = "research.no_go_overridden"
    PLAN_STARTED = "plan.started"
    GUIDELINE_STARTED = "guideline.started"
    CREATIVE_STARTED = "creative.started"
    #: Stage 04 PRD §10.3. The upload IS the rights attestation, so this row is
    #: who attested what; retiring is the only other thing that ever happens to
    #: a reference — they are never deleted.
    MEDIA_REFERENCE_UPLOADED = "media_reference.uploaded"
    MEDIA_REFERENCE_RETIRED = "media_reference.retired"
    #: The seal. PS4 requires the freeze to write an audit row in the same
    #: transaction as the change, which is what makes "who agreed to spend
    #: this, on what basis, on what date" answerable without a thread.
    PLAN_FROZEN = "plan.frozen"

    #: Stage 03. A signature is the only audit row in the system that records an
    #: act an administrator cannot perform, so it is worth being able to find.
    CLAIM_SIGNED = "claim.signed"
    CLAIM_SIGNATURE_REVOKED = "claim.signature_revoked"
    CLAIM_EDITED = "claim.edited"
    #: The seal, Stage 03's twin of PLAN_FROZEN. §12.4 requires publish to write
    #: an audit row in the same transaction as the change, which is what makes
    #: "who published this rulebook, over which signature, on what date"
    #: answerable a year later without a thread.
    GUIDELINE_PUBLISHED = "guideline.published"
    #: A previously published rulebook displaced by a newer MAJOR. Written per
    #: superseded version rather than once for the publish: a reader asking why
    #: v2 stopped governing wants a row about v2.
    GUIDELINE_SUPERSEDED = "guideline.superseded"
    HUMAN_TASK_OPENED = "human_task.opened"
    HUMAN_TASK_SUBMITTED = "human_task.submitted"
    #: Moving a person-task to somebody else. Carries the reason, which is
    #: mandatory, and every signature the handover voided. Deliberately not a
    #: flavour of `USER_ROLE_CHANGED`: this is the act that takes a
    #: non-delegable duty off one named person and puts it on another, and it
    #: is the row an auditor asking "why did somebody else sign" needs to find.
    HUMAN_TASK_REASSIGNED = "human_task.reassigned"
    #: The sign-off matrix changed outside a gate decision. The only path that
    #: can displace a legal owner, so it carries the outgoing owner, the
    #: incoming one, the mandatory reason and the signatures it voided.
    SIGNOFF_MATRIX_CHANGED = "signoff_matrix.changed"
    #: An amendment a person decided on, as opposed to the `policy_amendment.*`
    #: rows the watcher writes with no actor. Two vocabularies on purpose: "who
    #: applied this" and "what applied itself" are different questions.
    AMENDMENT_APPLIED = "policy_amendment.applied_by_person"
    AMENDMENT_DISMISSED = "policy_amendment.dismissed_by_person"
    #: A previously frozen plan displaced by a newer version. Written per
    #: superseded plan rather than once for the freeze: a reader asking why
    #: v2 stopped being current wants a row about v2.
    PLAN_SUPERSEDED = "plan.superseded"
    #: The research behind a plan stopped being the current acceptance (§4.4).
    #: Distinct from PLAN_SUPERSEDED: that one is "a newer *plan* displaced
    #: this", this one is "a newer *research report* did", and only the second
    #: leaves a frozen plan valid and downloadable.
    PLAN_SOURCE_SUPERSEDED = "plan.source_superseded"
    #: ...and the way back. Withdrawing a newer acceptance revives the one
    #: before it, so a plan can stop being stale. Recorded because a flag that
    #: silently cleared itself is indistinguishable from one that was never set.
    PLAN_SOURCE_RESTORED = "plan.source_restored"

    USER_INVITED = "user.invited"
    #: A fresh link for somebody who never accepted. The old one stops working,
    #: so this is a credential event and not a duplicate of `USER_INVITED`.
    INVITE_REISSUED = "user.invite_reissued"
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
    MEDIA_REFERENCE = "media_reference"
    SOURCE = "source"
    EVIDENCE = "evidence"
    EXPORT = "export"
    APPROVAL = "approval"
    SCHEDULE = "schedule"
    BACKUP = "backup"
    RESEARCH_ACCEPTANCE = "research_acceptance"
    CAMPAIGN_PLAN = "campaign_plan"
    CLAIM = "claim"
    CLAIM_SIGNATURE = "claim_signature"
    HUMAN_TASK = "human_task"
    CONTENT_GUIDELINE = "content_guideline"
    SIGNOFF_MATRIX = "signoff_matrix"
    POLICY_AMENDMENT = "policy_amendment"


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
