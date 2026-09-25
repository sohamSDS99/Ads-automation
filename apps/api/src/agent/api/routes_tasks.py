"""Person-tasks over HTTP (PRD §8.4, §16).

Three layers stand between a caller and a submitted attestation, and they are
not redundant:

1. **Role.** `require(Permission.ATTEST_SUBMIT)` — held by `approver` alone, and
   by no administrator: `permissions_for()` subtracts `NON_DELEGABLE` even on
   the superadmin branch.
2. **Identity.** `HumanTask.assignee_id` must be the caller. A person-task has
   no role fallback. `tasks.complete()` asserts this a second time, so a future
   route that forgot would still be refused by the helper.
3. **Presence.** A step-up token minted against the current password within the
   last `SIGNATURE_REAUTH_TTL_SECONDS`. A live session proves somebody signed in
   once; an attestation needs them here now.

The order matters and mirrors `routes_claims.sign_claims`: identity is asserted
before the step-up token is spent, so somebody who is not the assignee cannot
burn a proof that belongs to the person who is.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, File, Query, Request, UploadFile, status
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_tasks import (
    HumanTaskAttachment,
    HumanTaskList,
    HumanTaskReassign,
    HumanTaskSubmit,
    HumanTaskSummary,
    ReassignPreview,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, get_redis_client, require
from agent.auth.rbac import Permission
from agent.config import Settings, get_settings
from agent.db.models import (
    ClaimRecord,
    ClaimSignature,
    ClaimStatus,
    HumanTask,
    HumanTaskHandover,
    HumanTaskStatus,
    Membership,
    Project,
    User,
    UserStatus,
)
from agent.db.session import get_session
from agent.guidelines import tasks as task_service
from agent.guidelines.signature import ReauthError, ReauthTokens
from agent.storage import get_storage

log = structlog.get_logger(__name__)

router = APIRouter(tags=["human-tasks"])

Db = Annotated[AsyncSession, Depends(get_session)]
RedisDep = Annotated[Redis, Depends(get_redis_client)]
SettingsDep = Annotated[Settings, Depends(get_settings)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
Attester = Annotated[Principal, Depends(require(Permission.ATTEST_SUBMIT))]
UserManager = Annotated[Principal, Depends(require(Permission.USER_MANAGE))]

#: Formats an attestation may carry as evidence. Narrow on purpose: this is a
#: legal record, and "whatever the browser sent" is not an acceptable answer to
#: what is in it.
ATTACHMENT_MEDIA_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/webp",
        "text/plain",
        "text/csv",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


@router.get("/human-tasks", response_model=HumanTaskList, summary="Person-tasks in this workspace")
async def list_human_tasks(
    me: AnyMember,
    db: Db,
    mine: Annotated[bool, Query(description="Only tasks assigned to the caller")] = False,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> HumanTaskList:
    """List person-tasks. `READ`, because seeing that a task exists is not acting on it.

    Everyone can see every task and who it belongs to. That is deliberate: a
    person-task blocks a publish or a launch, and a task whose existence is
    hidden from the people it is blocking is a task that stalls with nobody
    able to say why.
    """
    where = [HumanTask.workspace_id == me.workspace_id]
    if mine:
        where.append(HumanTask.assignee_id == me.user.id)
    if project_id is not None:
        where.append(HumanTask.project_id == project_id)
    if status_filter:
        if status_filter == "open":
            where.append(HumanTask.status.in_(task_service.OUTSTANDING))
        else:
            try:
                where.append(HumanTask.status == HumanTaskStatus(status_filter))
            except ValueError:
                raise problems.unprocessable(
                    f"{status_filter!r} is not a task status. Valid values: "
                    f"{', '.join(sorted(item.value for item in HumanTaskStatus))}, or 'open' "
                    "for anything still waiting on its person.",
                    title="Unknown status",
                ) from None

    rows = (
        await db.execute(
            sa.select(HumanTask, User, Project)
            .join(User, User.id == HumanTask.assignee_id)
            .outerjoin(Project, Project.id == HumanTask.project_id)
            .where(*where)
            .order_by(HumanTask.created_at.desc())
        )
    ).all()

    # Counted separately and never derived from `rows`: the badge must mean the
    # same thing whatever filter the screen happened to apply, and a filtered
    # list that lowered the badge would read as "nothing waiting on you".
    mine_open = int(
        (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(HumanTask)
                .where(
                    HumanTask.workspace_id == me.workspace_id,
                    HumanTask.assignee_id == me.user.id,
                    HumanTask.status.in_(task_service.OUTSTANDING),
                )
            )
        ).scalar()
        or 0
    )

    return HumanTaskList(
        items=[_summary(task, user, project, me) for task, user, project in rows],
        mine_open=mine_open,
    )


@router.get("/human-tasks/{task_id}", response_model=HumanTaskSummary, summary="One person-task")
async def get_human_task(task_id: uuid.UUID, me: AnyMember, db: Db) -> HumanTaskSummary:
    task, user, project = await _task(db, task_id, me)
    return _summary(task, user, project, me)


# ---------------------------------------------------------------------------
# submitting — the non-delegable act
# ---------------------------------------------------------------------------


@router.post(
    "/human-tasks/{task_id}/submit",
    response_model=HumanTaskSummary,
    summary="Submit an attestation (the named assignee only)",
)
async def submit_human_task(
    task_id: uuid.UUID,
    body: HumanTaskSubmit,
    me: Attester,
    request: Request,
    db: Db,
    redis: RedisDep,
    settings: SettingsDep,
) -> HumanTaskSummary:
    """Record that the named person performed the act and attests to it."""
    task, user, project = await _task(db, task_id, me)

    if task.task_key == "H3":
        # Stage 04 §16 rule 4. H3 is not an attestation: it is one transaction
        # over a hashed exception set that writes claims, a signature and a
        # MINOR, and resumes the run. Completing its task here would do none of
        # that and leave 4.6.3 parked forever. Refused before the identity
        # check and before any proof is spent, whoever asks.
        raise problems.conflict(
            "H3 is decided through POST /creative-runs/{run_id}/exceptions/clear, "
            "not by submitting the task.",
            title="Use the exceptions route",
            code="use_exceptions_clear",
            run_id=str(task.guideline_run_id) if task.guideline_run_id else None,
        )

    # -- layer 2: identity, before any proof is spent ------------------------
    if task.assignee_id != me.user.id:
        raise problems.Problem(
            status_code=status.HTTP_403_FORBIDDEN,
            title="Not the assignee",
            detail=(
                f"This task is assigned to {user.name or user.email}. A person-task has no "
                "role fallback and no administrator override — it is performed by the "
                "named person or it is reassigned, with a reason, to somebody else."
            ),
            type_=problems.TYPE_FORBIDDEN,
        )
    if task.status is HumanTaskStatus.COMPLETED:
        # Not an error worth a 409's ceremony on a re-submit of the same thing:
        # the assignee pressing submit twice should see it done, not a failure.
        return _summary(task, user, project, me)
    if task.status not in task_service.OUTSTANDING:
        # `not_required` and `expired` are the other terminal states. Naming the
        # one it is in beats a generic refusal: "this task expired" and "3.3.1
        # found nothing requiring verification" send a person to two different
        # next actions.
        raise problems.conflict(
            f"This task is `{task.status.value}`, so there is nothing left to attest to.",
            title="Task closed",
        )

    # -- the checklist is the gate ------------------------------------------
    required = _required_keys(task.required_artifacts)
    confirmed = set(body.artifacts_confirmed)
    missing = [key for key in required if key not in confirmed]
    if missing:
        raise problems.unprocessable(
            f"{len(missing)} required artifact(s) were not confirmed: {', '.join(missing)}. "
            "An attestation that skipped part of its checklist is not the attestation "
            "that was asked for.",
            title="Checklist incomplete",
        )
    if required and not task.attachment_paths:
        raise problems.unprocessable(
            "This task requires supporting documents and none have been uploaded.",
            title="No attachments",
        )

    # -- layer 3: presence ---------------------------------------------------
    tokens = ReauthTokens(redis, ttl_seconds=settings.signature_reauth_ttl_seconds)
    reauth_token = str(body.payload.pop("reauth_token", "") or "")
    try:
        token_id = await tokens.consume(me.user.id, reauth_token)
    except ReauthError as exc:
        raise problems.Problem(
            status_code=status.HTTP_401_UNAUTHORIZED,
            title="Step-up required",
            detail=(
                f"{exc}. Confirm your password again — an attestation records that you "
                "were present when you made it."
            ),
            type_=problems.TYPE_UNAUTHENTICATED,
        ) from exc

    payload: dict[str, Any] = dict(body.payload)
    payload["artifacts_confirmed"] = sorted(confirmed)
    if body.reference:
        payload["reference"] = body.reference
    payload["reauth_token_id"] = token_id

    try:
        await task_service.complete(db, task, by=me.user.id, payload=payload)
    except PermissionError as exc:  # pragma: no cover — layer 2 already refused
        raise problems.forbidden(missing_permission=Permission.ATTEST_SUBMIT.value) from exc

    write_audit(
        db,
        workspace_id=me.workspace_id,
        action=AuditAction.HUMAN_TASK_SUBMITTED,
        target_type=AuditTarget.HUMAN_TASK,
        actor_id=me.user.id,
        target_id=task.id,
        meta={
            "task_key": task.task_key,
            "blocking_for": task.blocking_for.value,
            "artifacts_confirmed": sorted(confirmed),
            "attachments": len(task.attachment_paths or []),
            "reauth_token_id": token_id,
        },
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(task)
    log.info("human_task.submitted", task_id=str(task.id), task_key=task.task_key)
    return _summary(task, user, project, me)


@router.post(
    "/human-tasks/{task_id}/attachments",
    response_model=HumanTaskAttachment,
    status_code=status.HTTP_201_CREATED,
    summary="Attach a supporting document (the named assignee only)",
)
async def upload_attachment(
    task_id: uuid.UUID,
    me: Attester,
    db: Db,
    settings: SettingsDep,
    file: Annotated[UploadFile, File(description="PDF, image, text, CSV or Office document")],
) -> HumanTaskAttachment:
    """Store one file against a task. Assignee only, same as the submission.

    No step-up here, and that is the right line: uploading evidence is not the
    attestation. The attestation is the submit, and that is where presence is
    proved. Demanding a password per file would push people toward one big
    upload, which is worse evidence.
    """
    task, user, _project = await _task(db, task_id, me)
    if task.assignee_id != me.user.id:
        raise problems.Problem(
            status_code=status.HTTP_403_FORBIDDEN,
            title="Not the assignee",
            detail=(
                f"This task is assigned to {user.name or user.email}. Only they may attach "
                "the documents their attestation rests on."
            ),
            type_=problems.TYPE_FORBIDDEN,
        )
    if task.status not in task_service.OUTSTANDING:
        raise problems.conflict(
            "This task is no longer open, so its evidence cannot change.",
            title="Task closed",
        )

    content = await file.read()
    if not content:
        raise problems.unprocessable("That file is empty.")
    if len(content) > settings.document_max_bytes:
        raise problems.Problem(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            title="File too large",
            detail=(
                f"That file is {len(content) // 1024} KB; the limit is "
                f"{settings.document_max_bytes // 1024} KB."
            ),
            type_=problems.TYPE_VALIDATION,
        )
    media_type = (file.content_type or "").split(";")[0].strip().lower()
    if media_type and media_type not in ATTACHMENT_MEDIA_TYPES:
        raise problems.unprocessable(
            f"{media_type} is not a format this system accepts as attestation evidence.",
            title="Unsupported format",
        )

    filename = _safe_name(file.filename or "attachment")
    key = f"human-tasks/{task.id}/{uuid.uuid4().hex}-{filename}"
    get_storage(settings).put(key, content, content_type=media_type or None)

    # Rebound rather than appended in place: `attachment_paths` is an ARRAY
    # column and SQLAlchemy does not track mutation of the list it handed out,
    # so `.append()` here would store nothing and report success.
    task.attachment_paths = [*(task.attachment_paths or []), key]
    await db.commit()
    log.info("human_task.attachment", task_id=str(task.id), bytes=len(content))
    return HumanTaskAttachment(path=key, filename=filename, size_bytes=len(content))


# ---------------------------------------------------------------------------
# reassignment — the governance act
# ---------------------------------------------------------------------------


@router.get(
    "/human-tasks/{task_id}/reassign-preview",
    response_model=ReassignPreview,
    summary="What reassigning this task would void",
)
async def preview_reassign(
    task_id: uuid.UUID,
    to_user_id: Annotated[uuid.UUID, Query()],
    me: UserManager,
    db: Db,
) -> ReassignPreview:
    """Count the damage before anybody confirms it.

    Not in §16's endpoint list, and added deliberately: §15.3 C.6 requires the
    reassign dialog to state *exactly* how many signatures it will void before
    confirmation, and the only way for that number to be true is for the server
    that performs the voiding to be the one that counts it.
    """
    task, from_user, _project = await _task(db, task_id, me)
    to_user = await _member(db, to_user_id, me)
    signatures, claims = await _dependents(db, task, to_user_id=to_user_id)
    return ReassignPreview(
        task_id=task.id,
        from_user_id=from_user.id,
        from_user_name=from_user.name or from_user.email,
        to_user_id=to_user.id,
        to_user_name=to_user.name or to_user.email,
        voided_signature_ids=[row.id for row in signatures],
        requeued_claim_ids=[row.id for row in claims],
        voided_count=len(signatures),
        requeued_count=len(claims),
    )


@router.post(
    "/human-tasks/{task_id}/reassign",
    response_model=ReassignPreview,
    summary="Hand a person-task to somebody else",
)
async def reassign_human_task(
    task_id: uuid.UUID,
    body: HumanTaskReassign,
    me: UserManager,
    request: Request,
    db: Db,
) -> ReassignPreview:
    """Move the duty, void what the outgoing person signed, and say so.

    An administrator may do this and may not sign. That asymmetry is the whole
    mechanism: reassignment is visible, reasoned and audit-logged, where a
    self-granted signature would be none of those things.
    """
    task, from_user, _project = await _task(db, task_id, me)
    to_user = await _member(db, body.to_user_id, me)

    if to_user.id == task.assignee_id:
        raise problems.unprocessable(
            f"This task is already assigned to {to_user.name or to_user.email}.",
            title="No change",
        )
    if task.status is HumanTaskStatus.COMPLETED:
        raise problems.conflict(
            "This task is already done. Reassigning it would not undo the attestation "
            "that was made, so the honest act is a new task.",
            title="Task complete",
        )

    signatures, claims = await _dependents(db, task, to_user_id=to_user.id)

    now = sa.func.now()
    for signature in signatures:
        signature.voided_at = now
        signature.voided_by = me.user.id
        signature.void_reason = (
            f"legal owner reassigned from {from_user.email} to {to_user.email}: {body.reason}"
        )
    for claim in claims:
        claim.status = ClaimStatus.PENDING_SIGNOFF
        claim.current_signature_id = None

    task.assignee_id = to_user.id
    db.add(
        HumanTaskHandover(
            task_id=task.id,
            from_user=from_user.id,
            to_user=to_user.id,
            reason=body.reason,
            performed_by=me.user.id,
            voided_signature_ids=[row.id for row in signatures],
        )
    )
    write_audit(
        db,
        workspace_id=me.workspace_id,
        action=AuditAction.HUMAN_TASK_REASSIGNED,
        target_type=AuditTarget.HUMAN_TASK,
        actor_id=me.user.id,
        target_id=task.id,
        meta={
            "task_key": task.task_key,
            "from_user": str(from_user.id),
            "to_user": str(to_user.id),
            "reason": body.reason,
            "voided_signatures": [str(row.id) for row in signatures],
            "requeued_claims": [str(row.id) for row in claims],
        },
        ip=client_ip(request),
    )
    await db.commit()
    log.info(
        "human_task.reassigned",
        task_id=str(task.id),
        voided=len(signatures),
        requeued=len(claims),
    )
    return ReassignPreview(
        task_id=task.id,
        from_user_id=from_user.id,
        from_user_name=from_user.name or from_user.email,
        to_user_id=to_user.id,
        to_user_name=to_user.name or to_user.email,
        voided_signature_ids=[row.id for row in signatures],
        requeued_claim_ids=[row.id for row in claims],
        voided_count=len(signatures),
        requeued_count=len(claims),
    )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _task(
    db: AsyncSession, task_id: uuid.UUID, me: Principal
) -> tuple[HumanTask, User, Project | None]:
    row = (
        await db.execute(
            sa.select(HumanTask, User, Project)
            .join(User, User.id == HumanTask.assignee_id)
            .outerjoin(Project, Project.id == HumanTask.project_id)
            .where(HumanTask.id == task_id, HumanTask.workspace_id == me.workspace_id)
        )
    ).first()
    if row is None:
        raise problems.not_found(f"No person-task {task_id}.")
    return row[0], row[1], row[2]


async def _member(db: AsyncSession, user_id: uuid.UUID, me: Principal) -> User:
    """An active account with an active membership of this workspace.

    Both statuses, for the reason `signoff._roster` gives: a disabled account
    with a live membership would otherwise become the named holder of a duty
    nobody can discharge.
    """
    row = (
        await db.execute(
            sa.select(User)
            .join(Membership, Membership.user_id == User.id)
            .where(
                User.id == user_id,
                Membership.workspace_id == me.workspace_id,
                Membership.status == UserStatus.ACTIVE,
                User.status == UserStatus.ACTIVE,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise problems.unprocessable(
            f"{user_id} is not an active member of this workspace, so a person-task "
            "cannot be assigned to them.",
            title="Not a member",
        )
    return row


async def _dependents(
    db: AsyncSession, task: HumanTask, *, to_user_id: uuid.UUID
) -> tuple[list[ClaimSignature], list[ClaimRecord]]:
    """Live signatures this handover voids, and the claims they licensed.

    Only H1 has dependents. An H2 attestation blocks a launch and licenses
    nothing, so reassigning it voids no signature — and reporting a count of
    zero is the honest answer rather than a reason to hide the dialog.
    """
    if task.task_key != "H1":
        return [], []
    signatures = list(
        (
            await db.execute(
                sa.select(ClaimSignature).where(
                    ClaimSignature.project_id == task.project_id,
                    ClaimSignature.signer_id == task.assignee_id,
                    ClaimSignature.voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not signatures:
        return [], []
    signature_ids = {row.id for row in signatures}
    claims = list(
        (
            await db.execute(
                sa.select(ClaimRecord).where(
                    ClaimRecord.current_signature_id.in_(signature_ids),
                )
            )
        )
        .scalars()
        .all()
    )
    return signatures, claims


def _required_keys(required_artifacts: dict[str, Any] | None) -> list[str]:
    """The checklist, however 3.3.2 happened to phrase it.

    Two shapes are in the wild — `{"items": [...]}` from the node and a bare
    mapping from a fixture — and a submission that silently accepted an
    unrecognised shape as "nothing required" would turn the checklist off.
    """
    if not required_artifacts:
        return []
    items = required_artifacts.get("items")
    if isinstance(items, list):
        return [
            str(item.get("key", item)) if isinstance(item, dict) else str(item) for item in items
        ]
    return [str(key) for key in required_artifacts]


def _safe_name(filename: str) -> str:
    """A storage-safe leaf. Path separators out, length bounded.

    The key is built from this, so a filename of `../../etc/passwd` must not be
    able to choose where the object lands.
    """
    leaf = filename.replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in leaf).strip("-.")
    return (cleaned or "attachment")[:120]


def _summary(
    task: HumanTask, user: User, project: Project | None, me: Principal
) -> HumanTaskSummary:
    """One row, with the server's answer to "may this caller act" already on it."""
    is_assignee = task.assignee_id == me.user.id
    return HumanTaskSummary(
        id=task.id,
        project_id=task.project_id,
        project_name=project.name if project is not None else None,
        guideline_run_id=task.guideline_run_id,
        node_id=task.node_id,
        task_key=task.task_key,
        title=task.title,
        instructions=task.instructions,
        assignee_id=task.assignee_id,
        assignee_name=user.name,
        assignee_email=user.email,
        required_artifacts=task.required_artifacts or {},
        attachment_paths=list(task.attachment_paths or []),
        submitted_payload=_redacted(task.submitted_payload),
        status=task.status.value,
        blocking_for=task.blocking_for.value,
        completed_by=task.completed_by,
        completed_at=task.completed_at,
        due_at=task.due_at,
        created_at=task.created_at,
        # Three conditions, all of them the server's: holds the permission, is
        # the named person, and the task is still open.
        can_submit=(
            is_assignee
            and Permission.ATTEST_SUBMIT in me.permissions
            and task.status in task_service.OUTSTANDING
        ),
        can_reassign=(
            Permission.USER_MANAGE in me.permissions and task.status in task_service.OUTSTANDING
        ),
    )


def _redacted(payload: dict[str, Any] | None) -> dict[str, Any] | None:
    """Never return the step-up token id to a browser.

    It is not a secret in the sense the token is — it is an identifier of one
    already-spent proof — but §16 rule 6 draws the line at the whole family,
    and a field nobody renders costs nothing to withhold.
    """
    if not payload:
        return payload
    return {key: value for key, value in payload.items() if key != "reauth_token_id"}
