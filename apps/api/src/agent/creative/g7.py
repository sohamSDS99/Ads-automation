"""G7 — the brief gate's decision (Stage 04 PRD §5.3, §8.3; law 40).

"G7 is hash-scoped. The approval stores `approved_hash = brief_hash`.
`media/jobs.py` refuses any submit for a run whose G7 is not `approved` with a
matching hash. An approver may edit the brief before approving; the edited
brief is revalidated against `CreativeBrief` and re-hashed."

Called by the approval route inside the decision's own transaction, before
`approvals.decide()` — so a refused edit writes nothing, and a lost race rolls
the brief back with the decision. The `CreativeBrief` row is updated in one
statement: payload, markdown and hash together with `approved_hash`, which is
the only order `creative_brief_approved_freeze` allows (it freezes the first
three the moment `approved_hash` is set).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import brief as briefs
from agent.creative import lint_adapter
from agent.db.models import Approval, Run
from agent.db.models import CreativeBrief as CreativeBriefRow
from agent.schemas.creative_brief import CreativeBrief

#: `Approval.gate_key` of node 4.1.1.
G7 = "G7"


async def approve(
    db: AsyncSession,
    *,
    approval: Approval,
    run: Run,
    edited: dict[str, Any] | None,
    now: datetime | None = None,
) -> CreativeBrief:
    """Approve the run's brief, as proposed or as edited. Raises `BriefError`.

    Returns the brief that was approved — the re-hashed edit, or the proposal —
    which is what the gate's `NodeRun` must now carry.
    """
    row = (
        await db.execute(
            sa.select(CreativeBriefRow)
            .where(CreativeBriefRow.creative_run_id == run.id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if row is None:
        raise briefs.BriefError("brief", f"run {run.id} has no brief row to approve")
    if row.approved_hash is not None:
        raise briefs.BriefError("brief", f"run {run.id}'s brief is already approved")

    if edited is None:
        final = CreativeBrief.model_validate(approval.proposal)
        if final.brief_hash != row.brief_hash or briefs.brief_hash(final) != final.brief_hash:
            raise briefs.BriefError(
                "brief_hash", "the proposal on the card is not the brief on record"
            )
    else:
        linter = await lint_adapter.load(
            db, workspace_id=run.workspace_id, pin=lint_adapter.current_pin(run)
        )
        final = briefs.revalidate_edit(
            approval.proposal,
            edited,
            licensed=briefs.licensed_claims(linter.ruleset, now or datetime.now(UTC)),
        )

    row.schema_version = final.schema_version
    row.payload = final.model_dump(mode="json")
    row.markdown = briefs.render(final)
    row.brief_hash = final.brief_hash
    row.approved_hash = final.brief_hash
    row.approval_id = approval.id
    await db.flush()
    return final
