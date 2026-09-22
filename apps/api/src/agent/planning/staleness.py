"""Whether the research a plan was built from is still the current acceptance.

Stage 02 PRD §4.4 and §18, row "Research re-run and re-accepted after a plan
was frozen": a newer accepted research run does **not** invalidate a frozen
plan. It marks it `source_superseded` and offers a re-plan. A draft whose
source acceptance is superseded cannot be frozen until it is re-run against
the current one — `freeze._blockers` already refuses that, and this module is
what makes the flag it reads ever become true.

**Derived, not latched.** `source_superseded` is exactly
`ResearchAcceptance.superseded_by IS NOT NULL` for the acceptance the plan
names. Written as a latch ("set it when a supersede happens") the column would
be right on the way up and wrong on the way back down: withdrawing a newer
acceptance revives the older one — `_accept` sets `superseded_by = None` on a
row it brings back — and a plan whose source is current again must stop
claiming otherwise. So every transition recomputes, and
`test_staleness.py::test_the_flag_equals_the_derived_value` asserts the stored
column and the derivation agree for every row after each transition. One rule
with a proof beats two rules that agree until they do not.

**It is a column and not a property for one reason: the frozen row.** A frozen
plan is immutable in `payload`, `markdown` and `version`; the guard in
migration 0013 deliberately leaves everything else writable so that "the
research behind this has moved on" can still be recorded on a sealed plan. A
computed property would have done, but the plan list and the Plan Viewer both
read this per row and a join-per-row is what the column exists to avoid.

**Same transaction as the acceptance change** (§17 PS4). Both callers are
mid-request with an open session; neither commits here. An audit row is
written per plan that actually changed — a no-op refresh writes nothing,
because "the flag was already right" is not an event.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.db.models import CampaignPlan, ResearchAcceptance

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class StalenessChange:
    """One plan whose relationship to its source research just changed."""

    plan_id: uuid.UUID
    plan_run_id: uuid.UUID
    version: int
    #: What the flag became. `True` = the source was superseded or withdrawn;
    #: `False` = the acceptance it was built from is current again.
    source_superseded: bool


async def refresh_for_project(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    ip: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> list[StalenessChange]:
    """Recompute `source_superseded` for every plan in one project.

    Returns only the plans that changed, so a caller can say "3 plans were
    marked" without re-querying, and so the audit trail carries one row per
    real transition rather than one per refresh.
    """
    rows = (
        await session.execute(
            sa.select(CampaignPlan, ResearchAcceptance.superseded_by)
            .join(ResearchAcceptance, ResearchAcceptance.id == CampaignPlan.acceptance_id)
            .where(
                CampaignPlan.workspace_id == workspace_id,
                CampaignPlan.project_id == project_id,
            )
            .order_by(CampaignPlan.created_at)
        )
    ).all()

    changed: list[StalenessChange] = []
    for plan, superseded_by in rows:
        stale = superseded_by is not None
        if plan.source_superseded == stale:
            continue
        plan.source_superseded = stale
        # Plain values, read before the flush and carried across it. Reading
        # `plan.version` back after a commit costs a lazy re-load with no
        # greenlet context, and this runs inside request handlers that commit
        # immediately afterwards.
        change = StalenessChange(
            plan_id=plan.id,
            plan_run_id=plan.plan_run_id,
            version=plan.version,
            source_superseded=stale,
        )
        changed.append(change)
        write_audit(
            session,
            workspace_id=workspace_id,
            actor_id=actor_id,
            action=(
                AuditAction.PLAN_SOURCE_SUPERSEDED if stale else AuditAction.PLAN_SOURCE_RESTORED
            ),
            target_type=AuditTarget.CAMPAIGN_PLAN,
            target_id=change.plan_id,
            # `me.audit_meta(...)` from the caller, so a superadmin acting
            # inside another workspace is marked here too. A row that records
            # *what* changed but not *whose* privilege did it is half a trail.
            meta={
                **(meta or {}),
                "project_id": str(project_id),
                "plan_run_id": str(change.plan_run_id),
                "version": change.version,
            },
            ip=ip,
        )

    if changed:
        await session.flush()
        log.info(
            "plan.staleness_refreshed",
            project_id=str(project_id),
            marked=[str(row.plan_id) for row in changed if row.source_superseded],
            cleared=[str(row.plan_id) for row in changed if not row.source_superseded],
        )
    return changed


async def refresh_for_plan(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    plan: CampaignPlan,
    actor_id: uuid.UUID | None,
    ip: str | None = None,
    meta: Mapping[str, Any] | None = None,
) -> bool:
    """Recompute one plan's flag, and return what it is now.

    The project-wide refresh above only ever sees rows that **already exist**,
    and a plan row is written at the *end* of a plan run (2.6.1). So a run that
    was in flight when newer research was accepted inserts its plan afterwards,
    with the flag at its `false` default, and nothing recomputes it until the
    next acceptance transition — which may never come. The freeze reads the
    stored column, so that plan could be sealed against research that had
    already been superseded, which is the one thing §4.4 exists to prevent.

    Hence this: the freeze calls it inside its own transaction, so the gate
    decides on a value it computed rather than on one it hopes is current.
    """
    superseded_by = (
        await session.execute(
            sa.select(ResearchAcceptance.superseded_by).where(
                ResearchAcceptance.id == plan.acceptance_id
            )
        )
    ).scalar_one_or_none()
    stale = superseded_by is not None
    if plan.source_superseded == stale:
        return stale

    plan.source_superseded = stale
    plan_id, plan_run_id, version = plan.id, plan.plan_run_id, plan.version
    write_audit(
        session,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=(AuditAction.PLAN_SOURCE_SUPERSEDED if stale else AuditAction.PLAN_SOURCE_RESTORED),
        target_type=AuditTarget.CAMPAIGN_PLAN,
        target_id=plan_id,
        meta={
            **(meta or {}),
            "project_id": str(plan.project_id),
            "plan_run_id": str(plan_run_id),
            "version": version,
            "found_at": "freeze",
        },
        ip=ip,
    )
    await session.flush()
    log.info(
        "plan.staleness_refreshed",
        project_id=str(plan.project_id),
        plan_id=str(plan_id),
        source_superseded=stale,
        at="freeze",
    )
    return stale


async def derived_flags(
    session: AsyncSession, *, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> dict[uuid.UUID, bool]:
    """What `source_superseded` *should* be, computed from the acceptance chain.

    The invariant test's other half. Nothing in production calls this — if it
    did, the column would be pointless — but a rule with no independent
    derivation to check it against is a rule that drifts.
    """
    rows = (
        await session.execute(
            sa.select(CampaignPlan.id, ResearchAcceptance.superseded_by)
            .join(ResearchAcceptance, ResearchAcceptance.id == CampaignPlan.acceptance_id)
            .where(
                CampaignPlan.workspace_id == workspace_id,
                CampaignPlan.project_id == project_id,
            )
        )
    ).all()
    return {plan_id: superseded_by is not None for plan_id, superseded_by in rows}
