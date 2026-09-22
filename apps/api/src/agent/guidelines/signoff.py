"""An approved G6 becomes the `signoff_matrix` row.

A gate node does not re-execute on approval: `approvals.decide` writes
`edited_proposal or proposal` straight onto the `NodeRun` and nothing runs
again. So the owners an approver confirmed only become a row if the *decision*
writes one — the same reason the budget gate's envelope arithmetic lives at the
decision rather than in node 2.2.4.

Everything here treats the proposal as untrusted input. It is: an approver can
edit it in a form before approving, so what arrives is free JSON that happens to
have come from a model a moment earlier. A missing owner or a string that is not
a user id is refused with a sentence rather than allowed to become an
`IntegrityError` from three layers down.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    Approval,
    ApprovalStatus,
    Membership,
    Run,
    SignOffMatrix,
    User,
    UserRole,
    UserStatus,
)

__all__ = ["SignOffError", "apply_decision", "assert_eligible", "matrix_from_proposal"]

#: Roles that may hold the legal signature (law 23). `admin` is deliberately
#: absent. Lives here rather than in the node because **both** the node that
#: proposes owners and the decision that records them have to apply it, and two
#: copies of a rule this load-bearing are two things that can drift apart.
SIGNING_ROLES = frozenset({UserRole.APPROVER})

#: The three owners §11 requires, in the order a card renders them.
OWNER_FIELDS = ("brand_owner_id", "legal_owner_id", "performance_owner_id")


class SignOffError(ValueError):
    """A decided G6 that cannot become a row. The message is shown verbatim."""


def assert_eligible(
    owners: Mapping[str, uuid.UUID], *, roster: Sequence[tuple[User, Membership]]
) -> None:
    """Every owner is a real, active member, and the legal owner may sign.

    The one rule, applied in both places an owner can be chosen: node 3.5.1
    when a model proposes them, and the G6 decision when a human edits them.
    The second is the one that actually has to hold — an approver can rewrite
    the proposal in a form before approving it, so a check that lived only in
    the node would guard the path nobody attacks.

    `legal_owner_id` is the single identity `CLAIM_SIGN` is narrowed to, so an
    ineligible one is not a cosmetic error: it is a project whose claims can
    never be signed, recorded as though sign-off were arranged.
    """
    known = {user.id: membership for user, membership in roster}
    for field in OWNER_FIELDS:
        chosen = owners[field]
        if chosen not in known:
            raise SignOffError(
                f"{chosen} is not a member of this workspace, so they cannot be named "
                f"as {field.removesuffix('_id').replace('_', ' ')}."
            )
    legal = known[owners["legal_owner_id"]]
    if legal.role not in SIGNING_ROLES:
        raise SignOffError(
            f"The legal owner holds `{legal.role.value}`. The claim signature is held by "
            "`approver` and not by `admin`, and is narrowed to one named identity, so no "
            "signature could ever be routed to them."
        )


def matrix_from_proposal(
    payload: dict[str, Any],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    set_by: uuid.UUID,
    previous: SignOffMatrix | None,
    on_file: bool,
) -> SignOffMatrix | None:
    """The row an approved proposal becomes, or `None` when there is nothing to write.

    `None` is the reuse path and not a failure: 3.5.1 emits `reused` when a
    current matrix already names the owners, and a second row would collide with
    `uq_signoff_matrix_current` — the partial unique index that makes "exactly
    one current matrix per project" a property of the database rather than of
    whoever remembered to check.

    **Reuse is decided by `on_file`, not by the payload.** `payload["reused"]`
    is approver-editable, and trusting it let an approver answer G6 with
    `"reused": true` on a project that had no matrix at all — the gate read as
    settled and no row was ever written, leaving every later stage narrowing
    `CLAIM_SIGN` to a `legal_owner_id` that does not exist.
    """
    if on_file:
        return None

    owners = payload.get("owners")
    if not isinstance(owners, dict):
        raise SignOffError(
            "This decision carries no `owners`, so there is nobody to record as "
            "brand, legal or performance owner."
        )

    resolved: dict[str, uuid.UUID] = {}
    for field in OWNER_FIELDS:
        raw = owners.get(field)
        if raw in (None, ""):
            raise SignOffError(f"This decision names no {field}, and all three are required.")
        try:
            resolved[field] = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
        except (ValueError, AttributeError, TypeError) as exc:
            raise SignOffError(
                f"{field} is {raw!r}, which is not a user id. Owners are recorded by id, "
                "never by name or email address."
            ) from exc

    return SignOffMatrix(
        workspace_id=workspace_id,
        project_id=project_id,
        brand_owner_id=resolved["brand_owner_id"],
        legal_owner_id=resolved["legal_owner_id"],
        performance_owner_id=resolved["performance_owner_id"],
        version=(previous.version + 1) if previous is not None else 1,
        previous_id=previous.id if previous is not None else None,
        set_by=set_by,
    )


async def apply_decision(
    db: AsyncSession, *, approval: Approval, run: Run, decided_by: uuid.UUID
) -> SignOffMatrix | None:
    """Write the matrix an approved G6 confirms.

    Does not commit. The caller writes the audit row in the same transaction and
    commits both together, so a matrix without its audit entry is not a state
    this can reach.

    **This never supersedes an existing matrix.** Changing the legal owner is a
    governance act with its own ceremony — an admin reassigns it, every
    signature the outgoing owner made is voided, and the reason is mandatory and
    audit-logged. A gate decision is none of those things, and because a parked
    run releases the project lock, a second G6 can sit open for days beside a
    matrix that was established after it was proposed. Answering that stale gate
    would quietly install a new sole signer. So a G6 whose premise no longer
    holds is refused here and the existing matrix stands.
    """
    if approval.status is not ApprovalStatus.APPROVED:
        return None

    current = (
        (
            await db.execute(
                sa.select(SignOffMatrix)
                .where(
                    SignOffMatrix.project_id == run.project_id,
                    SignOffMatrix.superseded_at.is_(None),
                )
                .with_for_update()
            )
        )
        .scalars()
        .first()
    )

    payload = approval.edited_proposal or approval.proposal or {}
    row = matrix_from_proposal(
        payload,
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        set_by=decided_by,
        previous=current,
        on_file=current is not None,
    )
    if row is None:
        return None

    roster = await _roster(db, workspace_id=run.workspace_id)
    assert_eligible(
        {
            "brand_owner_id": row.brand_owner_id,
            "legal_owner_id": row.legal_owner_id,
            "performance_owner_id": row.performance_owner_id,
        },
        roster=roster,
    )

    db.add(row)
    await db.flush()
    return row


async def _roster(db: AsyncSession, *, workspace_id: uuid.UUID) -> list[tuple[User, Membership]]:
    """Active accounts with an active membership of this workspace.

    Both statuses: `membership.status` is an unaccepted invitation to *this*
    workspace and `user.status` is the account itself, so a disabled account
    with a live membership would otherwise be recordable as the one identity
    permitted to sign.
    """
    result = await db.execute(
        sa.select(User, Membership)
        .join(Membership, Membership.user_id == User.id)
        .where(
            Membership.workspace_id == workspace_id,
            Membership.status == UserStatus.ACTIVE,
            User.status == UserStatus.ACTIVE,
        )
        .order_by(User.id)
    )
    return [(row[0], row[1]) for row in result.all()]
