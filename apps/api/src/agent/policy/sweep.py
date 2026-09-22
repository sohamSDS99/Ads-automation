"""The `policy_watch` sweep (PRD §8.5). The thing the cron job calls.

Lives here rather than in `scheduling/` so it can be called directly from a
test without an arq context — the same reason `poller.py` and `reaper.py` sit
apart from `scheduling/jobs.py`.

The sweep is a pipeline of the three modules, in the only order that is safe:

    watcher.watch_workspace   → did anything change?     (no model, no writes
                                                          beyond the amendment)
    classifier.classify       → what kind of change?     (one model call)
    lifecycle.apply           → what happens now?        (no model)

**Classification failure is not sweep failure.** An amendment that could not be
classified stays `open`/`unclassified`, and §8.6 resolves that to `substantive`
— a human. The failure mode of the alternative is the one that matters: if a
classifier outage rolled back the amendment, a policy change would be detected,
discarded, and never seen again, because tomorrow's fetch would find the new
hash already stored and report "unchanged".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import AmendmentStatus, PolicyAmendment, Workspace
from agent.guidelines.constants import get_content_constants
from agent.llm.gateway import LLMGateway
from agent.llm.router import ModelRouter
from agent.policy import classifier, lifecycle, watcher

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class SweepOutcome:
    checked: int = 0
    changed: int = 0
    unchanged: int = 0
    first_seen: int = 0
    stale: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    amendments: int = 0
    auto_applied: int = 0
    needs_review: int = 0
    unclassifiable: list[str] = field(default_factory=list)


async def sweep(
    db: AsyncSession,
    *,
    llm: LLMGateway,
    router: ModelRouter,
    workspace_id: uuid.UUID | None = None,
) -> SweepOutcome:
    """Check every watched source, then classify and apply what changed."""
    outcome = SweepOutcome()
    constants = get_content_constants()

    workspaces = (
        [workspace_id]
        if workspace_id is not None
        else list((await db.execute(sa.select(Workspace.id))).scalars().all())
    )

    for workspace in workspaces:
        results = await watcher.watch_workspace(db, workspace_id=workspace)
        outcome.checked += len(results)
        for result in results:
            match result.outcome:
                case "changed":
                    outcome.changed += 1
                    outcome.amendments += len(result.amendment_ids)
                case "unchanged":
                    outcome.unchanged += 1
                case "first_seen":
                    outcome.first_seen += 1
                case "stale":
                    # Surfaced by label, not swallowed. A stale selector means
                    # this source has stopped reporting, and a sweep that
                    # returned "all fine" would hide that indefinitely.
                    outcome.stale.append(f"{result.label}: {result.detail}")
                case "failed":
                    outcome.failed.append(f"{result.label}: {result.detail}")

    pending = (
        (
            await db.execute(
                sa.select(PolicyAmendment)
                .where(PolicyAmendment.status == AmendmentStatus.OPEN)
                .order_by(PolicyAmendment.detected_at)
            )
        )
        .scalars()
        .all()
    )
    for amendment in pending:
        try:
            classification = await classifier.classify(
                db, amendment, llm=llm, router=router, constants=constants
            )
        except Exception as exc:  # noqa: BLE001 — see the module docstring
            log.error(
                "policy_sweep.classify_failed", amendment_id=str(amendment.id), error=str(exc)
            )
            outcome.unclassifiable.append(str(amendment.id))
            continue
        applied = await lifecycle.apply(db, amendment, classification, constants=constants)
        if applied.status is AmendmentStatus.AUTO_APPLIED:
            outcome.auto_applied += 1
        elif applied.status is AmendmentStatus.NEEDS_REVIEW:
            outcome.needs_review += 1

    await db.commit()
    log.info(
        "policy_sweep.done",
        checked=outcome.checked,
        changed=outcome.changed,
        auto_applied=outcome.auto_applied,
        needs_review=outcome.needs_review,
        stale=len(outcome.stale),
    )
    return outcome
