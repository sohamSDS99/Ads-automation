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
from typing import Literal

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
