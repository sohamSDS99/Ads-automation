"""The freeze, in the parts that need no database (Stage 02 §12.2, §16).

`planning/freeze.py` splits into pure decisions and one transaction. The
decisions are here: which blockers a plan carries, what `confirm_version` does
on an already-frozen plan, and what the sealed payload says. The transaction —
the version mint, the supersede, the audit row and the database trigger that
makes law 17 real — is `tests/integration/test_plan_freeze.py`, because a
trigger cannot be asserted against a stub.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest

from agent.db.models import (
    Approval,
    ApprovalRequiredRole,
    ApprovalStatus,
    CampaignPlanStatus,
)
from agent.db.models import (
    CampaignPlan as CampaignPlanRow,
)
from agent.planning import freeze as freezing
from tests import plan_fixture as fixture

FROZEN_AT = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


def row(
    *,
    status: CampaignPlanStatus = CampaignPlanStatus.READY_TO_FREEZE,
    version: int = 0,
    critique_issues: list[dict] | None = None,
    source_superseded: bool = False,
) -> CampaignPlanRow:
    """A `campaign_plan` row carrying the shared fixture's payload."""
    plan = fixture.plan(
        plan_status="frozen" if status is CampaignPlanStatus.FROZEN else "ready_to_freeze",
        critique_issues=critique_issues or [],
    )
    return CampaignPlanRow(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        project_id=fixture.PROJECT_ID,
        plan_run_id=fixture.PLAN_RUN_ID,
        acceptance_id=uuid.uuid4(),
        schema_version="1.0",
        version=version,
        status=status,
        payload=plan.model_dump(mode="json"),
        markdown="# stored markdown\n",
        source_superseded=source_superseded,
    )


def approvals(**overrides: ApprovalStatus) -> dict[str, Approval]:
    """All four gates approved, unless a test says otherwise."""
    found: dict[str, Approval] = {}
    for key, node_id in (
        ("G1", "2.1.3"),
        ("G2", "2.1.4"),
        ("G3", "2.2.4"),
        ("G4", "2.3.1"),
    ):
        found[key] = Approval(
            id=uuid.uuid4(),
            run_id=fixture.PLAN_RUN_ID,
            node_id=node_id,
            gate_key=key,
            status=overrides.get(key, ApprovalStatus.APPROVED),
            required_role=ApprovalRequiredRole.APPROVER,
            proposal={},
        )
    return found


def codes(blockers: list[freezing.Blocker]) -> set[str]:
    return {item.code for item in blockers}


# ---------------------------------------------------------------------------
# what stops a freeze
# ---------------------------------------------------------------------------


def test_four_approved_gates_and_a_clean_critique_is_freezable() -> None:
    assert freezing._blockers(row(), approvals()) == []


def test_a_pending_gate_blocks_and_names_which() -> None:
    blockers = freezing._blockers(row(), approvals(G3=ApprovalStatus.PENDING))
    assert codes(blockers) == {"gate_not_approved"}
    assert "G3 (budget allocation)" in blockers[0].detail


def test_a_rejected_gate_blocks() -> None:
    blockers = freezing._blockers(row(), approvals(G1=ApprovalStatus.REJECTED))
    assert codes(blockers) == {"gate_not_approved"}
    assert "rejected" in blockers[0].detail


def test_a_gate_that_never_opened_blocks_differently_from_one_that_is_pending() -> None:
    """A run that never reached 2.2.4 is a different problem from an approver
    who has not looked yet, and the fix_url is different too."""
    missing = approvals()
    del missing["G4"]
    blockers = freezing._blockers(row(), missing)
    assert codes(blockers) == {"gate_not_opened"}
    assert blockers[0].fix_url.endswith("/plan")


def test_every_undecided_gate_is_reported_not_just_the_first() -> None:
    blockers = freezing._blockers(
        row(), approvals(G2=ApprovalStatus.PENDING, G3=ApprovalStatus.PENDING)
    )
    assert len(blockers) == 2


def test_a_blocking_critique_issue_stops_the_freeze() -> None:
    plan_row = row(
        critique_issues=[
            {
                "severity": "blocking",
                "section": "media_plan.allocation",
                "finding": "The allocation totals 45,000 against an envelope of 40,000.",
                "fix": "Re-run 2.2.4.",
                "check": "1_allocation_sums",
            }
        ]
    )
    blockers = freezing._blockers(plan_row, approvals())
    assert codes(blockers) == {"blocking_critique"}
    assert "45,000" in blockers[0].detail


def test_a_warning_critique_issue_does_not() -> None:
    plan_row = row(
        critique_issues=[
            {"severity": "warning", "section": "x", "finding": "y", "fix": "z", "check": "5_brand"}
        ]
    )
    assert freezing._blockers(plan_row, approvals()) == []


def test_superseded_research_stops_the_freeze() -> None:
    """§4.4: a draft cannot be frozen against research that has been re-accepted."""
    blockers = freezing._blockers(row(source_superseded=True), approvals())
    assert codes(blockers) == {"source_superseded"}


def test_a_blocked_plan_with_no_blocking_issue_is_refused_rather_than_resolved() -> None:
    """The row and the payload disagreeing is worth stopping on."""
    blockers = freezing._blockers(row(status=CampaignPlanStatus.BLOCKED), approvals())
    assert codes(blockers) == {"plan_blocked"}


# ---------------------------------------------------------------------------
# §16 rule 2 — idempotence on confirm_version
# ---------------------------------------------------------------------------


def test_freezing_an_already_frozen_plan_at_the_same_version_is_not_an_error() -> None:
    frozen = row(status=CampaignPlanStatus.FROZEN, version=3)
    frozen.frozen_approval_ids = [uuid.uuid4()]
    result = freezing._already_frozen(frozen, 3)
    assert result.already_frozen is True
    assert result.version == 3
    assert result.approval_ids == frozen.frozen_approval_ids


def test_freezing_an_already_frozen_plan_at_a_different_version_is_a_conflict() -> None:
    frozen = row(status=CampaignPlanStatus.FROZEN, version=3)
    with pytest.raises(freezing.FreezeConflict) as caught:
        freezing._already_frozen(frozen, 4)
    assert caught.value.expected == 3
    assert caught.value.submitted == 4


# ---------------------------------------------------------------------------
# what the seal writes
# ---------------------------------------------------------------------------


def test_the_sealed_payload_carries_the_version_the_status_and_the_freeze_time() -> None:
    payload, markdown = freezing._sealed_payload(
        row(), version=3, frozen_at=FROZEN_AT, frozen_by_name="Soham Sarker", project=None
    )
    assert payload["version"] == 3
    assert payload["plan_status"] == "frozen"
    assert payload["generated_at"].startswith("2026-09-22T12:00")
    assert "FROZEN" not in markdown or "DRAFT" not in markdown


def test_the_sealed_markdown_loses_the_draft_watermark_and_names_the_signer() -> None:
    _, markdown = freezing._sealed_payload(
        row(), version=3, frozen_at=FROZEN_AT, frozen_by_name="Soham Sarker", project=None
    )
    assert "DRAFT — NOT APPROVED" not in markdown
    assert "Soham Sarker" in markdown
    assert "**Status:** Frozen (v3)" in markdown


def test_an_unparseable_payload_is_patched_rather_than_refused() -> None:
    """Four people already approved this plan; a contract drift must not strand it."""
    broken = row()
    broken.payload = {"schema_version": "0.9", "nonsense": True}
    payload, markdown = freezing._sealed_payload(
        broken, version=2, frozen_at=FROZEN_AT, frozen_by_name="", project=None
    )
    assert payload["version"] == 2
    assert payload["plan_status"] == "frozen"
    assert payload["nonsense"] is True
    # The stored markdown is kept rather than re-rendered from a payload that
    # does not parse — a blank document would be worse than a stale one.
    assert markdown == broken.markdown


def test_the_four_gate_keys_are_the_four_the_prd_names() -> None:
    assert freezing.REQUIRED_GATES == ("G1", "G2", "G3", "G4")
    assert set(freezing.GATE_LABELS) == set(freezing.REQUIRED_GATES)


def test_a_missing_plan_raises_refused_rather_than_returning_none() -> None:
    blockers = [
        freezing.Blocker(code="plan_not_found", detail="No campaign plan.", fix_url="/plan")
    ]
    with pytest.raises(freezing.FreezeRefused) as caught:
        raise freezing.FreezeRefused(blockers)
    assert caught.value.blockers == blockers
