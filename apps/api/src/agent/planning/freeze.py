"""Sealing a campaign plan (Stage 02 PRD §12.2, §16, law 17).

Freezing is one transaction and it is the only place a plan becomes
authoritative. §12.2 lists what it does; this module is that list, in order,
with the ordering made load-bearing:

1. assert all four `Approval` rows are `approved`;
2. assert the critique returned no `blocking` issue;
3. mint `version = max(version) + 1` for the project;
4. copy the four approval ids into `frozen_approval_ids`;
5. set `frozen_at` and `frozen_by`;
6. mark any prior frozen plan `superseded`;
7. write an `AuditLog` row.

**The payload is rewritten before the status changes, not after.** The database
trigger seals `payload`, `markdown` and `version` the moment `status =
'frozen'`, and it fires on `OLD.status` — so the single `UPDATE` that carries a
row from `ready_to_freeze` to `frozen` may also carry the new version and the
re-rendered markdown, and the *next* one may not. Splitting them into two
statements would make the second one the thing the trigger rejects. This is
not a workaround: it is why the trigger checks `OLD.status` rather than
`NEW.status`, and a test asserts both halves.

**Idempotent on `confirm_version` (§16 rule 2).** Freezing an already-frozen
plan at the same version returns the existing row, because a dialog that
double-submits or a client that retries a lost 200 must not see an error for
something that has already succeeded. A *different* version is a 409: the
caller is looking at a stale screen, and the fix is to reload it, not to
guess.

**A version race is a 409, not a 500.** Two plans frozen in the same project at
the same instant both compute `N`; the partial unique index refuses the
second. `IntegrityError` reaching a client as a 500 tells them nothing;
`FreezeConflict` tells them to reopen the dialog.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.db.models import (
    Approval,
    ApprovalStatus,
    CampaignPlan,
    CampaignPlanStatus,
    Project,
    User,
)
from agent.db.repos import CampaignPlanRepo
from agent.export.plan_contract import CampaignPlan as PlanContract
from agent.export.plan_markdown import render_plan_markdown
from agent.planning import staleness

log = structlog.get_logger(__name__)

#: §11 and §16: four gates, no more. A plan cannot be frozen until every one of
#: them carries an `approved` row.
REQUIRED_GATES: tuple[str, ...] = ("G1", "G2", "G3", "G4")

#: What each gate is called, for a blocker message a person can act on.
GATE_LABELS = {
    "G1": "campaign targets",
    "G2": "lead definition",
    "G3": "budget allocation",
    "G4": "channel slate",
}


@dataclass(frozen=True, slots=True)
class Blocker:
    """One reason a plan may not be frozen, in the shape §16 rule 1 gives it."""

    code: str
    detail: str
    fix_url: str


class FreezeRefused(RuntimeError):
    """The plan is not in a state that may be sealed. Carries the blockers."""

    def __init__(self, blockers: list[Blocker]) -> None:
        super().__init__("; ".join(item.detail for item in blockers))
        self.blockers = blockers


class FreezeConflict(RuntimeError):
    """The caller's `confirm_version` does not match what would be minted."""

    def __init__(self, detail: str, *, expected: int, submitted: int) -> None:
        super().__init__(detail)
        self.expected = expected
        self.submitted = submitted


@dataclass(slots=True)
class FreezeResult:
    """What the freeze did. `already_frozen` is the idempotent path."""

    plan: CampaignPlan
    version: int
    already_frozen: bool = False
    superseded: list[uuid.UUID] = field(default_factory=list)
    approval_ids: list[uuid.UUID] = field(default_factory=list)


async def freeze_plan(
    session: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    plan_run_id: uuid.UUID,
    confirm_version: int,
    actor_id: uuid.UUID,
    ip: str | None = None,
) -> FreezeResult:
    """Seal one plan. Raises `FreezeRefused` or `FreezeConflict`, or commits."""
    repo = CampaignPlanRepo(session, workspace_id)
    plan = await repo.for_run(plan_run_id)
    if plan is None:
        raise FreezeRefused(
            [
                Blocker(
                    code="plan_not_found",
                    detail=f"No campaign plan for run {plan_run_id}.",
                    fix_url="/plan",
                )
            ]
        )

    if plan.status is CampaignPlanStatus.FROZEN:
        return _already_frozen(plan, confirm_version)

    # Recompute the staleness flag before reading it. `staleness.refresh_for_
    # project` runs on acceptance transitions and can only touch rows that
    # exist at that moment — and a plan row is written at the *end* of its run,
    # so a run that was in flight when newer research was accepted lands with
    # the flag at its default. Deciding the freeze on a stored value nobody has
    # recomputed is deciding it on a guess.
    await staleness.refresh_for_plan(
        session, workspace_id=workspace_id, plan=plan, actor_id=actor_id, ip=ip
    )

    approvals = await _approvals(session, plan_run_id)
    blockers = _blockers(plan, approvals)
    if blockers:
        # Nothing has been sealed, but the refresh above may have corrected a
        # row — and that correction is why this refusal happened. Commit it, or
        # the next caller recomputes the same thing and the audit trail never
        # says when it was noticed.
        await session.commit()
        raise FreezeRefused(blockers)

    version = await repo.next_version(plan.project_id)
    if confirm_version != version:
        raise FreezeConflict(
            f"This plan will be frozen as v{version}, and the request confirmed "
            f"v{confirm_version}. Reload the plan and try again — another version was "
            "probably frozen in this project while the dialog was open.",
            expected=version,
            submitted=confirm_version,
        )

    superseded = await _supersede(session, repo, plan, actor_id=actor_id, ip=ip)
    approval_ids = [approvals[key].id for key in REQUIRED_GATES]
    frozen_at = datetime.now(UTC)
    actor = await session.get(User, actor_id)

    # Order matters, and the module docstring says why: this one statement
    # carries the row from `ready_to_freeze` to `frozen` *and* writes the
    # version, payload and markdown. A second statement could not.
    plan.version = version
    plan.frozen_at = frozen_at
    plan.frozen_by = actor_id
    plan.frozen_approval_ids = approval_ids
    plan.payload, plan.markdown = _sealed_payload(
        plan,
        version=version,
        frozen_at=frozen_at,
        frozen_by_name=actor.name if actor else "",
        project=await session.get(Project, plan.project_id),
    )
    plan.status = CampaignPlanStatus.FROZEN

    write_audit(
        session,
        workspace_id=workspace_id,
        actor_id=actor_id,
        action=AuditAction.PLAN_FROZEN,
        target_type=AuditTarget.CAMPAIGN_PLAN,
        target_id=plan.id,
        meta={
            "plan_run_id": str(plan_run_id),
            "project_id": str(plan.project_id),
            "version": version,
            "approval_ids": [str(value) for value in approval_ids],
            "superseded": [str(value) for value in superseded],
        },
        ip=ip,
    )

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        raise FreezeConflict(
            f"Version {version} was taken by another freeze a moment ago. Reload the "
            "plan; the next version will be one higher.",
            expected=version,
            submitted=confirm_version,
        ) from exc

    log.info(
        "plan.frozen",
        plan_id=str(plan.id),
        plan_run_id=str(plan_run_id),
        version=version,
        superseded=len(superseded),
    )
    return FreezeResult(
        plan=plan, version=version, superseded=superseded, approval_ids=approval_ids
    )


# ---------------------------------------------------------------------------
# the checks
# ---------------------------------------------------------------------------


def _blockers(plan: CampaignPlan, approvals: dict[str, Approval]) -> list[Blocker]:
    """Everything standing between this plan and a signature."""
    found: list[Blocker] = []
    project_url = f"/projects/{plan.project_id}/plan"

    for key in REQUIRED_GATES:
        approval = approvals.get(key)
        if approval is None:
            found.append(
                Blocker(
                    code="gate_not_opened",
                    detail=(
                        f"Gate {key} ({GATE_LABELS[key]}) never opened, so nobody has "
                        "approved it. The plan run did not reach that node."
                    ),
                    fix_url=project_url,
                )
            )
        elif approval.status is not ApprovalStatus.APPROVED:
            found.append(
                Blocker(
                    code="gate_not_approved",
                    detail=(
                        f"Gate {key} ({GATE_LABELS[key]}) is {approval.status.value}. "
                        "All four gates must be approved before a plan can be frozen."
                    ),
                    fix_url="/approvals",
                )
            )

    blocking = _blocking_issues(plan)
    if blocking:
        found.append(
            Blocker(
                code="blocking_critique",
                detail=(
                    f"The plan critique found {len(blocking)} blocking issue"
                    f"{'' if len(blocking) == 1 else 's'}: "
                    + "; ".join(str(issue.get("finding", "")) for issue in blocking[:3])
                ),
                fix_url=f"/plans/{plan.plan_run_id}",
            )
        )

    if plan.status is CampaignPlanStatus.BLOCKED and not blocking:
        # `blocked` with no blocking issue in the payload means a gate was
        # rejected. The gate loop above has already said which, so this only
        # fires when the row and the payload disagree — which is worth
        # refusing rather than resolving in favour of either.
        found.append(
            Blocker(
                code="plan_blocked",
                detail=(
                    "This plan is marked blocked. Re-run it; a blocked plan is not a "
                    "draft that happens to be unfinished."
                ),
                fix_url=project_url,
            )
        )

    if plan.source_superseded:
        found.append(
            Blocker(
                code="source_superseded",
                detail=(
                    "The research this plan was built from has been superseded by a newer "
                    "acceptance. Re-run the plan against the current research before "
                    "freezing it (§4.4)."
                ),
                fix_url=project_url,
            )
        )
    return found


def _blocking_issues(plan: CampaignPlan) -> list[dict[str, Any]]:
    """The critique's blocking findings, read off the stored payload.

    From the payload rather than from the `node_run` output, because the
    payload is what the exported PDF carries: if the two ever disagree, the
    document a person signed is the one that matters.
    """
    payload = plan.payload if isinstance(plan.payload, dict) else {}
    issues = payload.get("critique_issues")
    if not isinstance(issues, list):
        return []
    return [
        dict(issue)
        for issue in issues
        if isinstance(issue, dict) and issue.get("severity") == "blocking"
    ]


async def _approvals(session: AsyncSession, plan_run_id: uuid.UUID) -> dict[str, Approval]:
    """The gate rows for this run, keyed by gate. Latest wins on a duplicate."""
    rows = (
        await session.execute(
            sa.select(Approval)
            .where(Approval.run_id == plan_run_id)
            .order_by(Approval.created_at.asc())
        )
    ).scalars()
    found: dict[str, Approval] = {}
    for approval in rows:
        key = (approval.gate_key or "").strip().upper()
        if key in REQUIRED_GATES:
            found[key] = approval
    return found


# ---------------------------------------------------------------------------
# the writes
# ---------------------------------------------------------------------------


def _already_frozen(plan: CampaignPlan, confirm_version: int) -> FreezeResult:
    """§16 rule 2. Same version is a 200; a different one is a 409."""
    if confirm_version == plan.version:
        return FreezeResult(
            plan=plan,
            version=plan.version,
            already_frozen=True,
            approval_ids=list(plan.frozen_approval_ids or []),
        )
    raise FreezeConflict(
        f"This plan is already frozen as v{plan.version}; the request confirmed "
        f"v{confirm_version}. Reload it.",
        expected=plan.version,
        submitted=confirm_version,
    )


async def _supersede(
    session: AsyncSession,
    repo: CampaignPlanRepo,
    plan: CampaignPlan,
    *,
    actor_id: uuid.UUID,
    ip: str | None,
) -> list[uuid.UUID]:
    """Mark every currently-frozen plan in this project superseded.

    Their `payload`, `markdown` and `version` are untouched — the trigger would
    refuse them anyway, and a superseded plan has to keep saying exactly what
    it said when it was signed. Only `status` moves.
    """
    superseded: list[uuid.UUID] = []
    for previous in await repo.frozen_for_project(plan.project_id):
        if previous.id == plan.id:
            continue
        previous.status = CampaignPlanStatus.SUPERSEDED
        superseded.append(previous.id)
        write_audit(
            session,
            workspace_id=plan.workspace_id,
            actor_id=actor_id,
            action=AuditAction.PLAN_SUPERSEDED,
            target_type=AuditTarget.CAMPAIGN_PLAN,
            target_id=previous.id,
            meta={"superseded_by_plan_run": str(plan.plan_run_id), "version": previous.version},
            ip=ip,
        )
    return superseded


def _sealed_payload(
    plan: CampaignPlan,
    *,
    version: int,
    frozen_at: datetime,
    frozen_by_name: str,
    project: Project | None,
) -> tuple[dict[str, Any], str]:
    """The payload and markdown a frozen plan carries forever.

    Three fields change and nothing else: `version`, `plan_status` and
    `generated_at` — the last because §14's frozen exports are stamped with
    when the plan was sealed, not when the draft was assembled, and a reader
    comparing a PDF's date against an audit row should find them equal.

    A payload that cannot be parsed is written back with the three fields
    patched in place rather than rebuilt. Refusing to freeze because a draft
    from an older contract version cannot round-trip would strand a plan four
    people have already approved.
    """
    payload = dict(plan.payload) if isinstance(plan.payload, dict) else {}
    payload["version"] = version
    payload["plan_status"] = "frozen"
    payload["generated_at"] = frozen_at.isoformat()

    try:
        parsed = PlanContract.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning(
            "plan.freeze_payload_unparseable",
            plan_id=str(plan.id),
            error=f"{type(exc).__name__}: {exc}",
        )
        return payload, plan.markdown

    markdown = render_plan_markdown(
        parsed,
        project_name=project.name if project else None,
        frozen_by_name=frozen_by_name,
    )
    return parsed.model_dump(mode="json"), markdown
