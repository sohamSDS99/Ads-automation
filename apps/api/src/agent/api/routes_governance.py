"""The sign-off matrix, and who decides the two guideline gates (PRD §11, §16).

This file holds the **only** path that can displace a legal owner.

`guidelines/signoff.apply_decision` deliberately refuses to: a gate decision is
not a governance act, and a stale G6 answered days later must not quietly
install a new sole signer. So the ceremony lives here, and it has three parts
that are not negotiable —

1. the number of signatures the change will void is **counted by the server**
   and returned before anybody confirms (§15.3 C.6),
2. a written reason is **required** whenever the legal owner changes,
3. the void, the re-queue, the new matrix row and the audit entry all land in
   **one transaction**, so a voided signature without its reason is not a state
   this system can reach.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_governance import (
    GuidelineApproversPatch,
    MatrixChangePreview,
    OwnerSlot,
    SignOffMatrixOut,
    SignOffMatrixState,
    SignOffMatrixUpdate,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.db.models import (
    ClaimRecord,
    ClaimSignature,
    ClaimStatus,
    Membership,
    Project,
    SignOffMatrix,
    User,
    UserStatus,
)
from agent.db.session import get_session
from agent.gates import SETTINGS_ASSIGNEES
from agent.guidelines.signoff import SIGNING_ROLES, SignOffError, assert_eligible
from agent.nodes.content.stage_3_1 import VISUAL_GATE
from agent.nodes.content.stage_3_5 import SIGNOFF_GATE

log = structlog.get_logger(__name__)

router = APIRouter(tags=["governance"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
SettingsWriter = Annotated[Principal, Depends(require(Permission.SETTINGS_WRITE))]
ProjectWriter = Annotated[Principal, Depends(require(Permission.PROJECT_WRITE))]

#: The gate keys §16 names, mapped to the node ids the settings blob is keyed
#: by. The interface talks in `G5`/`G6` because that is what the rulebook and
#: the publish dialog call them; `project.settings` has been keyed by node id
#: since Stage 01 and `orchestrator.approvals.assignee_for` reads it that way.
#: Translating here keeps one writer for that key instead of two.
GATE_NODES = {VISUAL_GATE: "3.1.3", SIGNOFF_GATE: "3.5.1"}


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------


@router.get(
    "/projects/{project_id}/signoff-matrix",
    response_model=SignOffMatrixState,
    summary="Who owns brand, legal and performance sign-off",
)
async def get_signoff_matrix(project_id: uuid.UUID, me: AnyMember, db: Db) -> SignOffMatrixState:
    """The current matrix, or plainly the absence of one.

    A project with no matrix answers `200` with `matrix: null` rather than
    `404`. "G6 has not been decided yet" is a normal early state of every
    project, and a 404 would make the screen render an error where the truthful
    thing to show is a sentence and a link to the gate.
    """
    await _project(db, project_id, me)
    current = await _current(db, project_id)
    roster = await _roster(db, me.workspace_id)
    can_edit = Permission.SETTINGS_WRITE in me.permissions

    return SignOffMatrixState(
        matrix=await _render(db, current, can_edit=can_edit) if current is not None else None,
        eligible_owners=[_slot(user, membership) for user, membership in roster],
        # Law 23 in the payload: only `approver` can hold the legal signature,
        # so a form that offered an admin would be offering an impossibility.
        eligible_legal_owners=[
            _slot(user, membership)
            for user, membership in roster
            if membership.role in SIGNING_ROLES
        ],
        can_edit=can_edit,
    )


@router.get(
    "/projects/{project_id}/signoff-matrix/preview",
    response_model=MatrixChangePreview,
    summary="What changing the matrix would void",
)
async def preview_matrix_change(
    project_id: uuid.UUID,
    me: SettingsWriter,
    db: Db,
    legal_owner_id: Annotated[uuid.UUID, Query()],
) -> MatrixChangePreview:
    """Count before you cut.

    Not in §16's list, and added for the same reason as the task preview: the
    editor must state the exact number of signatures it will void *before* the
    confirm control is reachable, and only the code that does the voiding can
    say what that number is.
    """
    await _project(db, project_id, me)
    current = await _current(db, project_id)
    return await _preview(db, project_id, current=current, legal_owner_id=legal_owner_id)


# ---------------------------------------------------------------------------
# writing — the ceremony
# ---------------------------------------------------------------------------


@router.put(
    "/projects/{project_id}/signoff-matrix",
    response_model=SignOffMatrixState,
    summary="Set the sign-off matrix (voids affected signatures)",
)
async def put_signoff_matrix(
    project_id: uuid.UUID,
    body: SignOffMatrixUpdate,
    me: SettingsWriter,
    request: Request,
    db: Db,
) -> SignOffMatrixState:
    """Supersede the current matrix with a new version.

    Rows are versioned, never edited: `uq_signoff_matrix_current` is a partial
    unique index over `superseded_at IS NULL`, so the old row is stamped and a
    new one inserted inside one transaction. Two admins saving at once do not
    both win — the second gets a unique-violation rather than a second current
    matrix, which is exactly what that index is for.
    """
    await _project(db, project_id, me)

    # SELECT ... FOR UPDATE, so the count returned to the confirming person and
    # the void that follows see the same set of signatures.
    current = await _current(db, project_id, for_update=True)

    roster = await _roster(db, me.workspace_id)
    owners = {
        "brand_owner_id": body.brand_owner_id,
        "legal_owner_id": body.legal_owner_id,
        "performance_owner_id": body.performance_owner_id,
    }
    try:
        assert_eligible(owners, roster=roster)
    except SignOffError as exc:
        raise problems.unprocessable(str(exc), title="Ineligible owner") from exc

    preview = await _preview(db, project_id, current=current, legal_owner_id=body.legal_owner_id)

    if current is not None and _unchanged(current, owners):
        # Nothing to write, so nothing to version and nothing to audit. A new
        # row here would burn a version number and read, a year later, as a
        # governance change that never happened.
        return SignOffMatrixState(
            matrix=await _render(db, current, can_edit=True),
            eligible_owners=[_slot(user, m) for user, m in roster],
            eligible_legal_owners=[_slot(user, m) for user, m in roster if m.role in SIGNING_ROLES],
            can_edit=True,
        )

    reason = (body.reason or "").strip()
    if preview.reason_required and not reason:
        raise problems.unprocessable(
            f"Changing the legal owner voids {preview.voided_count} signature(s) and "
            f"returns {preview.requeued_count} claim(s) to the queue. That needs a "
            "written reason — it is the only record of why somebody else now carries "
            "the liability.",
            title="Reason required",
        )

    now = sa.func.now()
    for signature in preview_signatures(preview, await _live_signatures(db, project_id)):
        signature.voided_at = now
        signature.voided_by = me.user.id
        signature.void_reason = f"sign-off matrix changed: {reason}"
    if preview.requeued_claim_ids:
        await db.execute(
            sa.update(ClaimRecord)
            .where(ClaimRecord.id.in_(preview.requeued_claim_ids))
            .values(status=ClaimStatus.PENDING_SIGNOFF, current_signature_id=None)
        )

    if current is not None:
        current.superseded_at = now
    row = SignOffMatrix(
        workspace_id=me.workspace_id,
        project_id=project_id,
        brand_owner_id=body.brand_owner_id,
        legal_owner_id=body.legal_owner_id,
        performance_owner_id=body.performance_owner_id,
        version=(current.version + 1) if current is not None else 1,
        previous_id=current.id if current is not None else None,
        set_by=me.user.id,
    )
    db.add(row)
    write_audit(
        db,
        workspace_id=me.workspace_id,
        action=AuditAction.SIGNOFF_MATRIX_CHANGED,
        target_type=AuditTarget.SIGNOFF_MATRIX,
        actor_id=me.user.id,
        target_id=project_id,
        meta={
            "version": row.version,
            "brand_owner_id": str(body.brand_owner_id),
            "legal_owner_id": str(body.legal_owner_id),
            "performance_owner_id": str(body.performance_owner_id),
            "legal_owner_changed": preview.legal_owner_changes,
            "from_legal_owner_id": (
                str(preview.from_legal_owner_id) if preview.from_legal_owner_id else None
            ),
            "reason": reason or None,
            "voided_signatures": [str(item) for item in preview.voided_signature_ids],
            "requeued_claims": [str(item) for item in preview.requeued_claim_ids],
        },
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(row)
    log.info(
        "signoff_matrix.changed",
        project_id=str(project_id),
        version=row.version,
        voided=preview.voided_count,
    )
    return SignOffMatrixState(
        matrix=await _render(db, row, can_edit=True),
        eligible_owners=[_slot(user, m) for user, m in roster],
        eligible_legal_owners=[_slot(user, m) for user, m in roster if m.role in SIGNING_ROLES],
        can_edit=True,
    )


@router.patch(
    "/projects/{project_id}/guideline-approvers",
    response_model=dict[str, str | None],
    summary="Who decides G5 and G6",
)
async def patch_guideline_approvers(
    project_id: uuid.UUID,
    body: GuidelineApproversPatch,
    me: ProjectWriter,
    request: Request,
    db: Db,
) -> dict[str, str | None]:
    """Assign the two guideline gates by gate key rather than by node id.

    This writes the same `project.settings` key `PATCH /projects/{id}` writes,
    translated through `GATE_NODES`. It is a second *name* for one writer, not
    a second writer: `orchestrator.approvals.assignee_for` reads that blob, and
    a route that kept its own copy would be a gate assignable in two places
    that disagree.
    """
    project = await _project(db, project_id, me)
    fields = {VISUAL_GATE: body.G5, SIGNOFF_GATE: body.G6}
    provided = {gate: user_id for gate, user_id in fields.items() if gate in body.model_fields_set}
    if not provided:
        raise problems.unprocessable(
            "Send G5, G6, or both. An empty patch changes nothing.", title="Nothing to set"
        )

    for gate, user_id in provided.items():
        if user_id is None:
            continue
        user = await _active_member(db, user_id, me)
        if gate == SIGNOFF_GATE and user[1].role not in SIGNING_ROLES:
            # G6 decides who may sign. An approver editing this gate's proposal
            # is bound by `assert_eligible` anyway, but refusing here means the
            # person who cannot usefully answer it never gets it in their inbox.
            raise problems.unprocessable(
                f"{user[0].email} holds `{user[1].role.value}`. G6 names the legal owner, "
                "and only an `approver` can hold that signature.",
                title="Ineligible approver",
            )

    settings = dict(project.settings or {})
    assignees = dict(settings.get(SETTINGS_ASSIGNEES) or {})
    for gate, user_id in provided.items():
        node_id = GATE_NODES[gate]
        if user_id is None:
            assignees.pop(node_id, None)
        else:
            assignees[node_id] = str(user_id)
    settings[SETTINGS_ASSIGNEES] = assignees
    project.settings = settings

    write_audit(
        db,
        workspace_id=me.workspace_id,
        action=AuditAction.PROJECT_UPDATED,
        target_type=AuditTarget.PROJECT,
        actor_id=me.user.id,
        target_id=project_id,
        meta={"fields": {"guideline_approvers": sorted(provided)}},
        ip=client_ip(request),
    )
    await db.commit()
    return {gate: assignees.get(node_id) for gate, node_id in GATE_NODES.items()}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def preview_signatures(
    preview: MatrixChangePreview, live: list[ClaimSignature]
) -> list[ClaimSignature]:
    """The rows the preview named, re-read from the live set.

    The preview carries ids; the write needs ORM objects. Intersecting rather
    than re-querying by signer means the write can only void what the person
    was told about, even if a signature landed in between.
    """
    wanted = set(preview.voided_signature_ids)
    return [row for row in live if row.id in wanted]


async def _project(db: AsyncSession, project_id: uuid.UUID, me: Principal) -> Project:
    row = (
        await db.execute(
            sa.select(Project).where(
                Project.id == project_id, Project.workspace_id == me.workspace_id
            )
        )
    ).scalar_one_or_none()
    if row is None:
        raise problems.not_found(f"No project {project_id}.")
    return row


async def _current(
    db: AsyncSession, project_id: uuid.UUID, *, for_update: bool = False
) -> SignOffMatrix | None:
    stmt = sa.select(SignOffMatrix).where(
        SignOffMatrix.project_id == project_id, SignOffMatrix.superseded_at.is_(None)
    )
    if for_update:
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalars().first()


async def _live_signatures(db: AsyncSession, project_id: uuid.UUID) -> list[ClaimSignature]:
    return list(
        (
            await db.execute(
                sa.select(ClaimSignature).where(
                    ClaimSignature.project_id == project_id,
                    ClaimSignature.voided_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )


async def _preview(
    db: AsyncSession,
    project_id: uuid.UUID,
    *,
    current: SignOffMatrix | None,
    legal_owner_id: uuid.UUID,
) -> MatrixChangePreview:
    """What changing the legal owner to `legal_owner_id` would cost.

    Zero is a real answer and is returned as one. A confirmation dialog on a
    harmless change trains people to click through the dangerous one, so the
    editor uses `voided_count == 0` to skip the ceremony entirely.
    """
    changes = current is not None and current.legal_owner_id != legal_owner_id
    if not changes:
        return MatrixChangePreview(
            legal_owner_changes=False,
            from_legal_owner_id=current.legal_owner_id if current is not None else None,
            to_legal_owner_id=legal_owner_id,
        )

    assert current is not None
    signatures = [
        row
        for row in await _live_signatures(db, project_id)
        if row.signer_id == current.legal_owner_id
    ]
    signature_ids = {row.id for row in signatures}
    claims = (
        list(
            (
                await db.execute(
                    sa.select(ClaimRecord.id).where(
                        ClaimRecord.current_signature_id.in_(signature_ids)
                    )
                )
            )
            .scalars()
            .all()
        )
        if signature_ids
        else []
    )
    names = await _names(db, {current.legal_owner_id, legal_owner_id})
    return MatrixChangePreview(
        legal_owner_changes=True,
        from_legal_owner_id=current.legal_owner_id,
        from_legal_owner_name=names.get(current.legal_owner_id),
        to_legal_owner_id=legal_owner_id,
        to_legal_owner_name=names.get(legal_owner_id),
        voided_signature_ids=[row.id for row in signatures],
        requeued_claim_ids=list(claims),
        voided_count=len(signatures),
        requeued_count=len(claims),
        # A reason is demanded for every legal-owner change, including one that
        # voids nothing. The act is "somebody else now carries the liability",
        # and that is worth a sentence whether or not anything was signed yet.
        reason_required=True,
    )


def _unchanged(current: SignOffMatrix, owners: dict[str, uuid.UUID]) -> bool:
    return (
        current.brand_owner_id == owners["brand_owner_id"]
        and current.legal_owner_id == owners["legal_owner_id"]
        and current.performance_owner_id == owners["performance_owner_id"]
    )


async def _roster(db: AsyncSession, workspace_id: uuid.UUID) -> list[tuple[User, Membership]]:
    result = await db.execute(
        sa.select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.workspace_id == workspace_id,
            Membership.status == UserStatus.ACTIVE,
            User.status == UserStatus.ACTIVE,
        )
        .order_by(User.name, User.email)
    )
    return [(row[0], row[1]) for row in result.all()]


async def _active_member(
    db: AsyncSession, user_id: uuid.UUID, me: Principal
) -> tuple[User, Membership]:
    row = (
        await db.execute(
            sa.select(User, Membership)
            .join(Membership, Membership.user_id == User.id)
            .where(
                User.id == user_id,
                Membership.workspace_id == me.workspace_id,
                Membership.status == UserStatus.ACTIVE,
                User.status == UserStatus.ACTIVE,
            )
        )
    ).first()
    if row is None:
        raise problems.unprocessable(
            f"{user_id} is not an active member of this workspace.", title="Not a member"
        )
    return row[0], row[1]


async def _names(db: AsyncSession, ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    if not ids:
        return {}
    rows = (await db.execute(sa.select(User).where(User.id.in_(ids)))).scalars().all()
    return {row.id: row.name or row.email for row in rows}


def _slot(user: User, membership: Membership) -> OwnerSlot:
    return OwnerSlot(user_id=user.id, name=user.name, email=user.email, role=membership.role.value)


async def _render(db: AsyncSession, matrix: SignOffMatrix, *, can_edit: bool) -> SignOffMatrixOut:
    ids = {
        matrix.brand_owner_id,
        matrix.legal_owner_id,
        matrix.performance_owner_id,
        matrix.set_by,
    }
    rows = (await db.execute(sa.select(User).where(User.id.in_(ids)))).scalars().all()
    by_id = {row.id: row for row in rows}

    def slot(user_id: uuid.UUID) -> OwnerSlot:
        user = by_id.get(user_id)
        return OwnerSlot(
            user_id=user_id,
            name=user.name if user else None,
            email=user.email if user else None,
        )

    set_by = by_id.get(matrix.set_by)
    return SignOffMatrixOut(
        id=matrix.id,
        project_id=matrix.project_id,
        brand_owner=slot(matrix.brand_owner_id),
        legal_owner=slot(matrix.legal_owner_id),
        performance_owner=slot(matrix.performance_owner_id),
        version=matrix.version,
        set_by=matrix.set_by,
        set_by_name=(set_by.name or set_by.email) if set_by else None,
        set_at=matrix.set_at,
        can_edit=can_edit,
    )
