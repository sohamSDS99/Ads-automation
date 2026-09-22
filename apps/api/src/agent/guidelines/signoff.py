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
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Approval, ApprovalStatus, Run, SignOffMatrix

__all__ = ["SignOffError", "apply_decision", "matrix_from_proposal"]

#: The three owners §11 requires, in the order a card renders them.
OWNER_FIELDS = ("brand_owner_id", "legal_owner_id", "performance_owner_id")


class SignOffError(ValueError):
    """A decided G6 that cannot become a row. The message is shown verbatim."""


def matrix_from_proposal(
    payload: dict[str, Any],
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    set_by: uuid.UUID,
    previous: SignOffMatrix | None,
) -> SignOffMatrix | None:
    """The row an approved proposal becomes, or `None` when there is nothing to write.

    `None` is the reuse path and not a failure: 3.5.1 emits `reused` when a
    current matrix already names the owners, and a second row would collide with
    `uq_signoff_matrix_current` — the partial unique index that makes "exactly
    one current matrix per project" a property of the database rather than of
    whoever remembered to check.
    """
    if payload.get("reused"):
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
    """Write the matrix an approved G6 confirms, superseding whatever it replaces.

    Does not commit. The caller writes the audit row in the same transaction and
    commits both together, so a matrix without its audit entry is not a state
    this can reach.
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

    row = matrix_from_proposal(
        approval.edited_proposal or approval.proposal or {},
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        set_by=decided_by,
        previous=current,
    )
    if row is None:
        return None

    if current is not None:
        # Stamped before the insert, and inside the same transaction: the
        # partial unique index permits exactly one row with a null
        # `superseded_at`, so the order here is what the database enforces
        # rather than a convention.
        current.superseded_at = sa.func.now()
        await db.flush()

    db.add(row)
    await db.flush()
    return row
