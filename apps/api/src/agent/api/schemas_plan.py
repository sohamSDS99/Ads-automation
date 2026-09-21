"""Request and response models for the Stage 01 → Stage 02 handshake.

Stage 02 PRD §16. Three shapes matter here:

* `Blocker` carries `severity`. PRD §4.2 lists E7 (`source_stale`) as a
  *warning* that is still startable, and E1–E6 as things that stop the run.
  A flat `blockers[]` with `eligible: true` alongside it would be a
  contradiction the UI has to guess its way out of, so the severity is on the
  row and `eligible` is derived from it.
* `PlanEligibility.source` is not in §16's one-line signature. The Stage 02
  landing page has to render who accepted the research, when, its verdict and
  its `degraded_sources` (§15.3 A), and the only other way to get those is to
  fetch the whole report payload to read five fields off it.
* Nothing here exposes a report payload, a credential or a raw CRM row (§16
  rule 5).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.db.models import CampaignPlanStatus, RunStatus
from agent.export.contract import LaunchReadiness

#: What a failing eligibility check is called. Stable strings: the frontend
#: switches on them and the tests name them (PRD §4.2 E1–E8).
BlockerCode = Literal[
    "no_accepted_research",
    "research_schema_unsupported",
    "research_says_no_go",
    "plan_in_flight",
    "plan_already_frozen",
    "missing_credential",
    "source_stale",
    "missing_permission",
    # Freeze-time blockers (Stage 02 §12.2, §16). Separate codes rather than a
    # reused "not_eligible": the Plan Viewer's freeze dialog renders the array
    # verbatim and routes each code to its own fix, and "gate G3 is pending"
    # and "the critique found a blocking issue" are different screens.
    "plan_not_found",
    "gate_not_opened",
    "gate_not_approved",
    "blocking_critique",
    "plan_blocked",
    "source_superseded",
    "version_race",
]


class Blocker(BaseModel):
    """One failing precondition, in words the person reading it can act on."""

    code: BlockerCode
    #: A whole sentence naming what is wrong with *this* project — never a
    #: generic "unavailable" (PRD §15.1).
    detail: str
    #: Where to go and fix it. Relative, always.
    fix_url: str
    #: `blocker` stops the run. `warning` is shown and does not.
    severity: Literal["blocker", "warning"] = "blocker"


class AcceptedSource(BaseModel):
    """The accepted research run, as the Stage 02 landing page needs it."""

    acceptance_id: uuid.UUID
    research_run_id: uuid.UUID
    research_report_id: uuid.UUID
    research_schema_version: str
    accepted_by: uuid.UUID
    accepted_by_name: str
    accepted_at: datetime
    note: str | None = None
    override_reason: str | None = None
    launch_readiness: LaunchReadiness
    degraded_sources: list[str] = Field(default_factory=list)
    #: Whole days since acceptance. The age chip turns amber past
    #: `plan_source_max_age_days` and the run is blocked past the hard limit.
    age_days: int


class PlanEligibility(BaseModel):
    """`GET /projects/{id}/plan/eligibility` — PRD §4.2."""

    eligible: bool
    blockers: list[Blocker] = Field(default_factory=list)
    #: Absent until research has been accepted, which is blocker E1.
    source: AcceptedSource | None = None


class AcceptResearchRequest(BaseModel):
    """`POST /runs/{id}/accept` — `{note?, override_reason?}`."""

    note: str | None = Field(default=None, max_length=2000)
    #: Required, and admin-only, when the report says `no_go` (E3). Stored on
    #: the acceptance, printed on the plan cover page, and audit-logged.
    override_reason: str | None = Field(default=None, max_length=2000)


class ResearchAcceptanceResponse(BaseModel):
    """One acceptance. Returned by both accept verbs."""

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID
    report_id: uuid.UUID
    accepted_by: uuid.UUID
    accepted_by_name: str
    accepted_at: datetime
    note: str | None = None
    launch_readiness_at_acceptance: str
    override_reason: str | None = None
    #: False once superseded by a later acceptance, or withdrawn.
    is_current: bool


class PlanRunAccepted(BaseModel):
    """`POST /projects/{id}/plan/runs` — 202."""

    run_id: uuid.UUID
    status: RunStatus
    source_run_id: uuid.UUID
    input_hash: str


class PlanVersion(BaseModel):
    """One row of the plan history table (PRD §15.3 A, *History*)."""

    id: uuid.UUID
    plan_run_id: uuid.UUID
    version: int
    status: CampaignPlanStatus
    schema_version: str
    source_superseded: bool
    frozen_at: datetime | None = None
    frozen_by: uuid.UUID | None = None
    frozen_by_name: str | None = None
    created_at: datetime


class PlanVersionList(BaseModel):
    """`GET /projects/{id}/plans` — newest first."""

    items: list[PlanVersion] = Field(default_factory=list)


class PlanCalcRow(BaseModel):
    """One calculation behind one number in the plan (PRD §15.3 B, *Calc* tab).

    This is the read side of Stage 02 law 14: the model never does arithmetic,
    so every figure a node asserts has a registered `@formula` behind it and
    leaves a `plan_calc` row. The console renders these so an approver can ask
    "where did $47 come from" and get the formula, its inputs and the constants
    version rather than a model's recollection.

    `evidence_id` is nullable by design — evidence can be pruned, and the
    calculation has to outlive it. A row with no evidence still reproduces.
    """

    id: uuid.UUID
    node_id: str
    #: The registry key, e.g. `economics.max_cpa_v1` — not a description.
    formula_id: str
    #: Constants-file version plus code version, so a number can be reproduced
    #: against the thresholds current when it was computed.
    calc_version: str
    inputs: dict[str, Any]
    result: dict[str, Any]
    evidence_id: uuid.UUID | None = None
    created_at: datetime


class PlanCalcList(BaseModel):
    """`GET /plans/{plan_run_id}/calcs` — oldest first, the order they computed in."""

    items: list[PlanCalcRow] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# the plan itself — the read side (PRD §15.3 D, E, F)
# ---------------------------------------------------------------------------


class PlanGateDecision(BaseModel):
    """One of law 16's four gates, as the freeze dialog has to show it.

    §15.3 E requires the dialog to list the four decisions with decider and
    timestamp before anyone types a version, so they travel with the plan
    rather than being re-fetched per gate from the approvals inbox. A gate that
    has not been decided is still a row: "G4 — not yet decided" is the answer
    to why the freeze button is refusing, and an absent row is not.
    """

    gate_key: str
    node_id: str
    status: str
    decided_by: uuid.UUID | None = None
    decided_by_name: str | None = None
    decided_at: datetime | None = None
    note: str | None = None
    #: True when the approver changed the agent's proposal before approving it.
    edited: bool = False


class PlanStructureTotals(BaseModel):
    """What the tree adds up to, for a reader who will never scroll all of it.

    Counted once, on the server, from the same flattener the diff uses. Counting
    in the frontend would mean the freeze dialog's "4,000 keywords" came from
    whichever page of the structure happened to be loaded.
    """

    campaigns: int = 0
    ad_groups: int = 0
    keywords: int = 0


class PlanCritique(BaseModel):
    """Node 2.6.2's verdict, as far as the freeze dialog needs it.

    Read from the 2.6.2 node run rather than from the payload: the payload is
    written by 2.6.1 and the critique runs after it, so the node output is the
    earliest and most authoritative place the verdict exists. `blocking` is what
    §12.2 asserts on, and a plan with no critique recorded yet reports
    `verdict=None` — the freeze route refuses it on the row's status anyway.
    """

    verdict: str | None = None
    blocking: list[str] = Field(default_factory=list)
    advisory: list[str] = Field(default_factory=list)
    checked_at: datetime | None = None


class PlanDetail(BaseModel):
    """`GET /plans/{plan_run_id}` — one plan version, whole (PRD §15.3 D).

    **`payload` is deliberately opaque.** It is the §12 `CampaignPlan` object
    exactly as node 2.6.1 wrote it, passed through without a Pydantic model of
    its own. Declaring the contract twice — once where it is filled and once
    where it is served — is how the two copies drift, and the read side gains
    nothing from validating a payload it renders section by section and must
    degrade on anyway. The fields around it are the ones the *row* owns, which
    the payload either does not carry or carries as a copy that can be stale.

    Where the two disagree, the row wins. `payload.plan_status` is written by
    2.6.2; `status` here is the column the freeze transaction reads and locks
    on, so a plan frozen a second ago reports `frozen` here while its payload
    still says `ready_to_freeze`.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    plan_run_id: uuid.UUID
    #: Minted at freeze (§12.2). A draft has not been given one yet.
    version: int
    #: The version this plan *would* be minted at, which is the number §15.3 E
    #: makes the approver type. Derived from the project's frozen plans, so the
    #: dialog and the freeze transaction agree on it without the dialog doing
    #: arithmetic.
    next_version: int
    status: CampaignPlanStatus
    schema_version: str
    source_superseded: bool
    payload: dict[str, Any] = Field(default_factory=dict)
    markdown: str = ""
    frozen_at: datetime | None = None
    frozen_by: uuid.UUID | None = None
    frozen_by_name: str | None = None
    frozen_approval_ids: list[uuid.UUID] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime
    #: The accepted research this plan was planned from (§12 `source`), read
    #: from the acceptance row so the header renders it even while the payload
    #: is still empty.
    source: AcceptedSource | None = None
    gates: list[PlanGateDecision] = Field(default_factory=list)
    critique: PlanCritique | None = None
    totals: PlanStructureTotals = Field(default_factory=PlanStructureTotals)


class PlanStructureKeyword(BaseModel):
    term: str
    match_type: str | None = None
    forecast_cpc_usd: float | None = None
    search_volume: int | None = None


class PlanStructureAdGroup(BaseModel):
    name: str
    theme: str | None = None
    landing_url: str | None = None
    primary_message: str | None = None
    market: str | None = None
    coherence: float | None = None
    negatives: list[str] = Field(default_factory=list)
    #: §15.3 D asks for a naming-validator tick per node. None means 2.4.1
    #: emitted no `validator_regex`, which is not the same as a name that
    #: failed, and the tree says so rather than showing a green tick it cannot
    #: justify.
    name_valid: bool | None = None
    keywords: list[PlanStructureKeyword] = Field(default_factory=list)
    keyword_count: int = 0


class PlanStructureCampaign(BaseModel):
    campaign_ref: str
    name: str
    type: str | None = None
    market: str | None = None
    language: str | None = None
    monthly_budget_usd: float | None = None
    daily_budget_usd: float | None = None
    bid_strategy: str | None = None
    target: float | None = None
    locations: list[str] = Field(default_factory=list)
    negatives: list[str] = Field(default_factory=list)
    name_valid: bool | None = None
    #: 2.4.3's per-campaign verdict — the learning-threshold badge of §15.3 D.
    verdict: str | None = None
    threshold: float | None = None
    forecast_conv_30d: float | None = None
    action: str | None = None
    remedy: str | None = None
    reason: str | None = None
    ad_groups: list[PlanStructureAdGroup] = Field(default_factory=list)
    ad_group_count: int = 0
    keyword_count: int = 0


class PlanStructurePage(BaseModel):
    """`GET /plans/{plan_run_id}/structure` — one page of campaigns.

    §16 rule 4: cursor-paginated by campaign, because a 4,000-keyword plan is
    never returned in one payload. `totals` counts the whole plan rather than
    the page, so a reader on page one still knows how much tree there is.
    """

    plan_run_id: uuid.UUID
    version: int
    status: CampaignPlanStatus
    totals: PlanStructureTotals = Field(default_factory=PlanStructureTotals)
    campaigns: list[PlanStructureCampaign] = Field(default_factory=list)
    #: Opaque. Pass back as `cursor`; absent means this was the last page.
    next_cursor: str | None = None
    #: 2.4.1's regex, for a reader who wants to know what the tick was checking.
    validator_regex: str | None = None
    #: Whether the live-account collision pass ran: `checked` or `skipped`.
    collision_check: str | None = None
    #: Names that failed the convention's own regex, named rather than counted.
    #:
    #: **`None` is not the same as `[]`.** `None` means node 2.4.2 never
    #: checked; `[]` means it checked and every name passed. Collapsing the two
    #: would report a clean bill of health on a tree nobody validated, which is
    #: the same mistake as a green tick on an unchecked name.
    invalid_names: list[str] | None = None
    #: Keywords appearing in more than one ad group. Three-state, as above.
    duplicate_terms: list[str] | None = None
    account_negatives: list[str] = Field(default_factory=list)
    orphan_terms: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# freezing (Stage 02 §12.2, §16)
# ---------------------------------------------------------------------------


class FreezePlanRequest(BaseModel):
    """`POST /plans/{plan_run_id}/freeze`.

    `confirm_version` is not ceremony. §15.3-E makes the dialog ask the person
    to type the version they are sealing, and the server checks it against the
    number it would actually mint — so a stale screen produces a 409 the user
    can understand rather than a v4 they believed was a v3.
    """

    confirm_version: int = Field(
        ge=1, description="The version this freeze will mint. From `next_version`."
    )


class FrozenPlan(BaseModel):
    """What a successful freeze returns."""

    plan_id: uuid.UUID
    plan_run_id: uuid.UUID
    project_id: uuid.UUID
    version: int
    status: str
    frozen_at: datetime | None = None
    frozen_by: uuid.UUID | None = None
    frozen_approval_ids: list[uuid.UUID] = Field(default_factory=list)
    #: Plans this freeze displaced. Empty for a project's first frozen plan.
    superseded: list[uuid.UUID] = Field(default_factory=list)
    #: True when the plan was already frozen at this version and nothing
    #: changed. §16 rule 2 makes that a 200, not an error.
    already_frozen: bool = False
