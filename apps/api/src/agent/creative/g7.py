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

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.creative import brief as briefs
from agent.creative import lint_adapter
from agent.db.models import Approval, Run
from agent.db.models import CreativeBrief as CreativeBriefRow
from agent.schemas.creative_brief import CreativeBrief
from agent.schemas.creative_input import CreativeInput

#: `Approval.gate_key` of node 4.1.1.
G7 = "G7"

#: PRD §11: 4.2.3 writes RSA A for every Search ad group and 4.2.4 writes
#: variant B, "a second RSA per ad group from a different brief angle".
RSAS_PER_AD_GROUP = 2

#: The plan's `PlannedCampaign.type` for a campaign that carries RSAs.
#: Performance Max carries asset-group text instead, and an untyped campaign
#: is not counted — an authorisation must not claim work nobody planned.
SEARCH = "search"


@dataclass(frozen=True, slots=True)
class Authorisation:
    """What approving the brief lets the run do, in numbers (PRD §15.4 D,
    §15.2 rule 8): "Authorises 20 RSAs, 36 images, 4 videos and up to $38.40
    of media spend".

    Media figures are the brief's own `media_plan`, which is `calc/`'s
    estimate copied — the numbers the approver signs are the numbers the
    hash covers, never a second computation.
    """

    rsas: int
    images: int
    videos: int
    media_usd: Decimal


def authorises(brief: CreativeBrief, inp: CreativeInput) -> Authorisation:
    search = {
        campaign.campaign_ref or campaign.name
        for campaign in inp.account_structure.campaigns
        if campaign.type == SEARCH
    }
    groups = sum(1 for group in brief.ad_groups if group.campaign_ref in search)
    jobs = brief.media_plan.jobs
    return Authorisation(
        rsas=RSAS_PER_AD_GROUP * groups,
        images=int(jobs.get("image", 0)),
        videos=int(jobs.get("video", 0)),
        media_usd=Decimal(brief.media_plan.media_usd),
    )


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
