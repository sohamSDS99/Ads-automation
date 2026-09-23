"""The policy amendment inbox (PRD §8.6, §15.3 G, §16).

Reading is `READ`; deciding is `GUIDELINE_PUBLISH`. That split is the point of
the screen — everybody can see that the rulebook is about to change and why,
and the person who publishes rulebooks is the one who says yes.

Applying and dismissing delegate to `policy/lifecycle.py`. The route validates,
audits and commits; it does not decide what an amendment class means. §8.6's
consequence table has one implementation, and adding a second here is how the
watcher and the inbox would come to disagree about what `signature_affecting`
does.
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
from agent.api.schemas_amendments import (
    AmendmentDecided,
    AmendmentDismiss,
    AmendmentList,
    AmendmentSummary,
    VoidedSignature,
)
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.db.models import (
    AmendmentStatus,
    ClaimRecord,
    ClaimSignature,
    PolicyAmendment,
    PolicySource,
    Project,
    RuleSet,
    User,
)
from agent.db.session import get_session
from agent.policy import lifecycle

log = structlog.get_logger(__name__)

router = APIRouter(tags=["amendments"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
Publisher = Annotated[Principal, Depends(require(Permission.GUIDELINE_PUBLISH))]

#: Statuses still waiting on a person.
OPEN = (AmendmentStatus.OPEN, AmendmentStatus.NEEDS_REVIEW)


@router.get(
    "/policy-amendments", response_model=AmendmentList, summary="Reasons the rulebook should change"
)
async def list_amendments(
    me: AnyMember,
    db: Db,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
    project_id: Annotated[uuid.UUID | None, Query()] = None,
) -> AmendmentList:
    where = [PolicyAmendment.workspace_id == me.workspace_id]
    if project_id is not None:
        where.append(PolicyAmendment.project_id == project_id)
    if status_filter:
        if status_filter == "open":
            where.append(PolicyAmendment.status.in_(OPEN))
        else:
            try:
                where.append(PolicyAmendment.status == AmendmentStatus(status_filter))
            except ValueError:
                raise problems.unprocessable(
                    f"{status_filter!r} is not an amendment status. Valid values: "
                    f"{', '.join(sorted(item.value for item in AmendmentStatus))}, or 'open'.",
                    title="Unknown status",
                ) from None

    rows = (
        await db.execute(
            sa.select(PolicyAmendment, Project, PolicySource)
            .outerjoin(Project, Project.id == PolicyAmendment.project_id)
            .outerjoin(PolicySource, PolicySource.id == PolicyAmendment.source_id)
            .where(*where)
            .order_by(PolicyAmendment.detected_at.desc())
        )
    ).all()

    open_count = int(
        (
            await db.execute(
                sa.select(sa.func.count())
                .select_from(PolicyAmendment)
                .where(
                    PolicyAmendment.workspace_id == me.workspace_id,
                    PolicyAmendment.status.in_(OPEN),
                )
            )
        ).scalar()
        or 0
    )

    items = [
        await _summary(db, amendment, project, source, me) for amendment, project, source in rows
    ]
    return AmendmentList(items=items, open_count=open_count)


@router.post(
    "/policy-amendments/{amendment_id}/apply",
    response_model=AmendmentDecided,
    summary="Apply an amendment and mint a MINOR",
)
async def apply_amendment(
    amendment_id: uuid.UUID, me: Publisher, request: Request, db: Db
) -> AmendmentDecided:
    amendment, project, source = await _amendment(db, amendment_id, me)
    try:
        outcome = await lifecycle.apply_by_person(db, amendment, decided_by=me.user.id)
    except lifecycle.AmendmentDecisionError as exc:
        raise problems.conflict(str(exc), title="Cannot apply") from exc

    write_audit(
        db,
        workspace_id=me.workspace_id,
        action=AuditAction.AMENDMENT_APPLIED,
        target_type=AuditTarget.POLICY_AMENDMENT,
        actor_id=me.user.id,
        target_id=amendment.id,
        meta={
            "change_kind": amendment.change_kind.value,
            "origin": amendment.origin.value,
            "ruleset_version": outcome.ruleset_version,
            "voided_signatures": [str(item) for item in outcome.voided_signature_ids],
        },
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(amendment)
    return AmendmentDecided(
        amendment=await _summary(db, amendment, project, source, me),
        ruleset_version=outcome.ruleset_version,
    )


@router.post(
    "/policy-amendments/{amendment_id}/dismiss",
    response_model=AmendmentDecided,
    summary="Dismiss an amendment, with a reason",
)
async def dismiss_amendment(
    amendment_id: uuid.UUID,
    body: AmendmentDismiss,
    me: Publisher,
    request: Request,
    db: Db,
) -> AmendmentDecided:
    amendment, project, source = await _amendment(db, amendment_id, me)
    try:
        await lifecycle.dismiss_by_person(db, amendment, decided_by=me.user.id, reason=body.reason)
    except lifecycle.AmendmentDecisionError as exc:
        raise problems.conflict(str(exc), title="Cannot dismiss") from exc

    write_audit(
        db,
        workspace_id=me.workspace_id,
        action=AuditAction.AMENDMENT_DISMISSED,
        target_type=AuditTarget.POLICY_AMENDMENT,
        actor_id=me.user.id,
        target_id=amendment.id,
        meta={
            "change_kind": amendment.change_kind.value,
            "origin": amendment.origin.value,
            "reason": body.reason,
        },
        ip=client_ip(request),
    )
    await db.commit()
    await db.refresh(amendment)
    return AmendmentDecided(amendment=await _summary(db, amendment, project, source, me))


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _amendment(
    db: AsyncSession, amendment_id: uuid.UUID, me: Principal
) -> tuple[PolicyAmendment, Project | None, PolicySource | None]:
    row = (
        await db.execute(
            sa.select(PolicyAmendment, Project, PolicySource)
            .outerjoin(Project, Project.id == PolicyAmendment.project_id)
            .outerjoin(PolicySource, PolicySource.id == PolicyAmendment.source_id)
            .where(
                PolicyAmendment.id == amendment_id,
                PolicyAmendment.workspace_id == me.workspace_id,
            )
        )
    ).first()
    if row is None:
        raise problems.not_found(f"No amendment {amendment_id}.")
    return row[0], row[1], row[2]


async def _summary(
    db: AsyncSession,
    amendment: PolicyAmendment,
    project: Project | None,
    source: PolicySource | None,
    me: Principal,
) -> AmendmentSummary:
    voided = await _voided(db, amendment)
    # The claims this amendment re-queued are exactly the ones its voided
    # signatures covered. Deriving them instead from "every unsigned claim in
    # the project" would sweep in claims that were never signed in the first
    # place and report them as something this row took away.
    requeued = await _requeued(db, amendment, voided)

    applied_version: str | None = None
    if amendment.applied_ruleset_id is not None:
        applied_version = (
            await db.execute(
                sa.select(RuleSet.ruleset_version).where(RuleSet.id == amendment.applied_ruleset_id)
            )
        ).scalar_one_or_none()

    reviewer = None
    if amendment.reviewed_by is not None:
        reviewer = (
            await db.execute(sa.select(User).where(User.id == amendment.reviewed_by))
        ).scalar_one_or_none()

    return AmendmentSummary(
        id=amendment.id,
        project_id=amendment.project_id,
        project_name=project.name if project is not None else None,
        origin=amendment.origin.value,
        detected_at=amendment.detected_at,
        change_kind=amendment.change_kind.value,
        status=amendment.status.value,
        source_id=amendment.source_id,
        source_label=source.label if source is not None else None,
        source_url=source.url if source is not None else None,
        diff=amendment.diff,
        proposed_rule_changes=amendment.proposed_rule_changes,
        rationale=amendment.rationale,
        applied_ruleset_version=applied_version,
        reviewed_by=amendment.reviewed_by,
        reviewed_by_name=(reviewer.name or reviewer.email) if reviewer else None,
        reviewed_at=amendment.reviewed_at,
        review_note=amendment.review_note,
        voided_signatures=voided,
        requeued_claim_ids=requeued,
        # Status is part of the answer, not a separate client-side check: an
        # `auto_applied` row offering Apply is the same defect as a forbidden
        # one offering it.
        can_decide=(Permission.GUIDELINE_PUBLISH in me.permissions and amendment.status in OPEN),
    )


async def _voided(db: AsyncSession, amendment: PolicyAmendment) -> list[VoidedSignature]:
    """The signatures this amendment took away, named.

    Read from `amendment.voided_signature_ids` rather than by re-deriving from
    the signature table: what matters is what *this* amendment voided, and a
    signature voided later for a different reason is not this row's doing.
    """
    ids = list(amendment.voided_signature_ids or [])
    if not ids:
        return []
    rows = (
        await db.execute(
            sa.select(ClaimSignature, User)
            .outerjoin(User, User.id == ClaimSignature.signer_id)
            .where(ClaimSignature.id.in_(ids))
        )
    ).all()
    return [
        VoidedSignature(
            signature_id=signature.id,
            signer_id=signature.signer_id,
            signer_name=(user.name or user.email) if user else None,
            signed_at=signature.signed_at,
            voided_at=signature.voided_at,
            void_reason=signature.void_reason,
            claim_count=len(signature.claim_ids or []),
        )
        for signature, user in rows
    ]


async def _requeued(
    db: AsyncSession, amendment: PolicyAmendment, voided: list[VoidedSignature]
) -> list[uuid.UUID]:
    """Claims that lost their licence when this amendment voided its signatures.

    `ClaimSignature.claim_ids` is the set the signer actually covered, so it
    survives the re-queue — `ClaimRecord.current_signature_id` is nulled by
    then and cannot be joined back.

    Filtered to claims that are still unsigned: one that has since been signed
    again is no longer outstanding, and listing it would overstate the work
    this amendment left behind.
    """
    if not voided:
        return []
    covered: set[uuid.UUID] = set()
    rows = (
        (
            await db.execute(
                sa.select(ClaimSignature.claim_ids).where(
                    ClaimSignature.id.in_([item.signature_id for item in voided])
                )
            )
        )
        .scalars()
        .all()
    )
    for claim_ids in rows:
        covered.update(claim_ids or [])
    if not covered:
        return []
    return list(
        (
            await db.execute(
                sa.select(ClaimRecord.id).where(
                    ClaimRecord.id.in_(covered),
                    ClaimRecord.current_signature_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
