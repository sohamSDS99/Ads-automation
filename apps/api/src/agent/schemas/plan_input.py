"""`PlanInput` — the one object that crosses from Stage 01 to Stage 02.

Stage 02 PRD §4.3. Assembled once, at plan-run start, from an accepted
research report; hashed into `Run.input_hash`; passed to every plan node
read-only. No node re-reads the `Report` row, and no plan node re-runs a
research node or re-queries a research connector for a fact research already
established (Stage 02 law 13).

Three properties of the shape are load-bearing:

* **It is frozen.** "Passed read-only" is a structural fact here rather than a
  convention a node could forget. The nested Stage 01 sections are not frozen
  — they are `ResearchReport`'s own models and they belong to it — but nothing
  can swap out a whole section behind the run's back.
* **It forbids extras.** Everything in here is put there by
  `orchestrator.plan_input`, so an unexpected key is a builder bug, not a
  richer record. That is the opposite of `ResearchReport`, which allows extras
  precisely because a *model* fills it in.
* **It carries its own hash.** One definition of "the same input", used by
  `Run.input_hash` at launch and by anything later that needs to know whether
  two runs were planned from the same research.

The Stage 01 sections are imported rather than re-declared. A second spelling
of `DemandMap` would be the most expensive kind of duplication in the product:
two shapes for the same evidence, diverging quietly.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

# `Market` is the shape of `Project.markets`, and this is where it is declared.
# Importing it upwards from `api.schemas_projects` is deliberate: a fourth
# definition of {country, language, currency} would be the actual mistake.
from agent.api.schemas_projects import Market
from agent.export.contract import (
    AccountLearnings,
    BusinessContext,
    Claim,
    CompetitiveLandscape,
    DemandMap,
    LaunchReadiness,
    PricedKeyword,
    Readiness,
)

#: This contract's own version, independent of the research report's. Bumped
#: when a plan node could read a `PlanInput` it does not understand.
PLAN_INPUT_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"


class PlanInput(BaseModel):
    """Everything Stage 02 is allowed to know about Stage 01."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = PLAN_INPUT_SCHEMA_VERSION

    # -- provenance ---------------------------------------------------------
    project_id: uuid.UUID
    research_run_id: uuid.UUID
    research_report_id: uuid.UUID
    #: Validated against `settings.plan_supported_research_schemas` *before*
    #: this object is built, so reaching here means it was supported at launch.
    research_schema_version: str
    accepted_by: uuid.UUID
    accepted_at: datetime
    acceptance_note: str | None = None
    #: Set only when E3 was overridden — an admin accepted a `no_go` report.
    #: Printed on the plan cover page, so it travels with the input rather than
    #: being looked up again later.
    override_reason: str | None = None

    # -- the research, verbatim --------------------------------------------
    launch_readiness: LaunchReadiness
    launch_blockers: list[Claim] = Field(default_factory=list)
    business_context: BusinessContext
    account_learnings: AccountLearnings
    competitive_landscape: CompetitiveLandscape
    demand_map: DemandMap
    readiness: Readiness
    priced_keyword_list: list[PricedKeyword] = Field(default_factory=list)
    #: Carried through verbatim and displayed on the budget gate: a forecast
    #: built on degraded demand data must not look as confident as one that is
    #: not (§4.3 rule 4).
    degraded_sources: list[str] = Field(default_factory=list)

    # -- the project --------------------------------------------------------
    markets: list[Market] = Field(default_factory=list)
    product_context: dict[str, Any] = Field(default_factory=dict)

    def content_hash(self) -> str:
        """sha256 over the canonical JSON. What `Run.input_hash` stores.

        Field order is the declaration order above and pydantic's JSON encoding
        is stable, so the same research accepted the same way hashes the same
        in any process — which is the only reason the value is worth storing.
        """
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()

    @property
    def is_launchable(self) -> bool:
        """Mirrors `ResearchReport.is_launchable`. A `no_go` needs an override."""
        return self.launch_readiness != "no_go"
