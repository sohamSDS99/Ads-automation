"""Request and response models for the approvals API (PRD §14)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from agent.db.models import ApprovalRequiredRole, ApprovalStatus, RunStatus


class ApprovalItem(BaseModel):
    """One gate, as the inbox and the run console show it."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    run_id: uuid.UUID
    project_id: uuid.UUID
    node_id: str
    node_name: str
    #: Which gate this is — 'G1'..'G4' for a plan gate, 'R0' for a research
    #: one (migration 0013 declined to relabel those). Sent so the inbox can
    #: badge a card by the decision it carries rather than by a node id.
    gate_key: str
    status: ApprovalStatus
    required_role: ApprovalRequiredRole
    assignee_id: uuid.UUID | None
    assignee_email: str | None = None
    proposal: dict[str, Any]
    edited_proposal: dict[str, Any] | None = None
    decision_note: str | None = None
    decided_by: uuid.UUID | None = None
    decided_at: datetime | None = None
    created_at: datetime
    run_status: RunStatus
    project_name: str | None = None
    run_triggered_by_name: str | None = Field(
        default=None,
        description="Who launched the run this gate belongs to; null for a scheduled run.",
    )
    sla_hours: int | None = Field(
        default=None,
        description=(
            "Hours the project allows this gate before it is late, from "
            "`project.settings['gate_sla_hours']`. The inbox counts down against it; "
            "P8's reminder job reads the same key."
        ),
    )
    can_decide: bool = Field(
        description=(
            "Whether the caller may decide this gate. Server-computed and advisory — "
            "the route enforces it independently (PRD §18 law 6)."
        )
    )


class ApprovalListResponse(BaseModel):
    """`GET /approvals` — cursor-paginated inbox feed."""

    items: list[ApprovalItem]
    next_cursor: str | None = None


class ApprovalDecisionRequest(BaseModel):
    """`POST /approvals/{id}` — `{decision, note, edited_proposal?}`."""

    decision: str = Field(description="`approve` or `reject`.")
    note: str | None = Field(
        default=None, max_length=4000, description="Why. Recorded in the audit log."
    )
    edited_proposal: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Approve with changes. When present this replaces the proposal as the node's "
            "output, so everything downstream reads the approved text, not the draft."
        ),
    )

    @property
    def approved(self) -> bool:
        return self.decision.strip().lower() in {"approve", "approved", "accept"}

    @property
    def rejected(self) -> bool:
        return self.decision.strip().lower() in {"reject", "rejected", "decline"}


class ReassignRequest(BaseModel):
    """`PATCH /approvals/{id}/assignee` — null hands it back to the whole role."""

    assignee_id: uuid.UUID | None = None


class RecalcLine(BaseModel):
    """One edited allocation line. Money, and the unit it belongs to.

    Deliberately carries no rate. The forecast CPA, the CPC and the absorption
    cap are read from the stored proposal server-side — a client that could
    supply the CPA could make any budget buy any number of conversions, and
    gate G3 exists to stop a budget being signed on a figure nobody can check.
    """

    campaign_ref: str = Field(min_length=1)
    market: str = Field(min_length=1)
    funnel_stage: str = Field(min_length=1)
    usd: float = Field(ge=0, description="What this line should get. Zero switches it off.")


class RecalcRequest(BaseModel):
    """`POST /approvals/{id}/recalc` — what would this edit actually buy?"""

    allocation: list[RecalcLine] = Field(
        min_length=1,
        description=(
            "Only the lines that changed need be sent; everything else in the proposal is "
            "re-forecast unchanged, which is what makes the envelope check meaningful."
        ),
    )
    envelope_usd: float | None = Field(
        default=None,
        gt=0,
        description=(
            "Raise or lower the envelope as part of the edit. Defaults to the envelope in "
            "the proposal."
        ),
    )


class RecalcResponse(BaseModel):
    """The re-forecast. No state advanced, no model called."""

    approval_id: uuid.UUID
    envelope_usd: float
    requested_usd: float = Field(description="What the edited lines add up to.")
    effective_usd: float = Field(
        description="What can actually be spent, after each line's absorption cap."
    )
    wasted_usd: float = Field(description="Requested minus effective — budget with nowhere to go.")
    delta_usd: float = Field(description="Requested minus the envelope. Signed.")
    delta_pct: float
    envelope_breach: bool = Field(
        description=(
            "True when the edit misses the envelope by more than the tolerance. Advisory "
            "here — `POST /approvals/{id}` refuses the decision independently."
        )
    )
    tolerance_pct: float
    est_conv: float
    est_cpa_usd: float | None = None
    baseline_usd: float
    allocation: list[dict[str, Any]]
    capped: list[dict[str, Any]] = Field(default_factory=list)
    below_floor: list[dict[str, Any]] = Field(default_factory=list)
    unknown_lines: list[str] = Field(
        default_factory=list,
        description="Lines sent that the proposal does not contain. Ignored, and named.",
    )
    switched_off: list[str] = Field(default_factory=list)
    calc_evidence_id: uuid.UUID = Field(
        description=(
            "The `PlanCalc` row this what-if wrote. Put it in the edited proposal's "
            "`calc_evidence_ids` so the approved figures cite the calculation that "
            "produced them rather than the draft split they replaced."
        )
    )
    summary: str


class ApprovalDecisionResponse(BaseModel):
    """What the decider gets back, including whether the run picked up again."""

    approval: ApprovalItem
    run_status: RunStatus
    resumed: bool = Field(description="True when the decision re-queued the run.")
