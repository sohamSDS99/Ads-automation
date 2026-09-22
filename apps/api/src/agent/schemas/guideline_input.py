"""The object that crosses into Stage 03 (PRD §4.5).

Stage 02's `PlanInput` is the counterpart, and the difference between them is
the whole design. `PlanInput` carries one upstream and cannot be built without
it. `GuidelineInput` carries six, every one of them optional, and the absence
of all six is `standalone` — a first-class, tested path and not a degraded
mode (law 21).

Two rules the rest of the stage leans on:

* **`mode` is derived, never asserted.** It is a function of which bindings
  resolved, so it cannot disagree with them. A caller that claims
  `fully_linked` with no plan id is telling the rulebook it was built from
  something it was not.
* **Every optional field is `None`, never an empty collection.** A node has to
  be able to tell "there was no channel slate" from "the slate was empty", and
  a default of `[]` erases that distinction in the one place it matters:
  `scope: unscoped` widens the asset spec sheet to every campaign type, and
  widening is only safe when it is deliberate.
"""

from __future__ import annotations

import hashlib
import uuid
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

# `Market` is the shape of `Project.markets`, declared with the project rather
# than with the report — same import `PlanInput` uses.
from agent.api.schemas_projects import Market
from agent.db.models import GuidelineMode
from agent.export.contract import CompetitorAd, ComplianceGuardrails
from agent.export.plan_contract import AccountStructure, ChannelSlate

GUIDELINE_INPUT_SCHEMA_VERSION = "1.0"

__all__ = [
    "GUIDELINE_INPUT_SCHEMA_VERSION",
    "GuidelineBindings",
    "GuidelineInput",
    "GuidelineMode",
]


class GuidelineBindings(BaseModel):
    """Which Stage 01 and Stage 02 artifacts this run was pointed at.

    Resolved once at run start and hashed into `Run.input_hash`. A binding that
    does not resolve is *dropped* rather than fatal — Stage 03 does not need
    it, and a stage that starts cold must not acquire a hard dependency by way
    of an optional one (§4.5 rule 4).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    research_run_id: uuid.UUID | None = None
    acceptance_id: uuid.UUID | None = None
    research_schema_version: str | None = None
    plan_id: uuid.UUID | None = None
    plan_version: int | None = None
    plan_schema_version: str | None = None

    @property
    def mode(self) -> GuidelineMode:
        """The four modes, as a function of what actually resolved.

        The two bindings are independent: `plan_linked` without
        `research_linked` is legal, because a frozen plan already carries its
        own `source.research_run_id` and Stage 03 may follow that pointer for
        evidence without needing the acceptance itself.
        """
        research = self.research_run_id is not None
        plan = self.plan_id is not None
        if research and plan:
            return GuidelineMode.FULLY_LINKED
        if research:
            return GuidelineMode.RESEARCH_LINKED
        if plan:
            return GuidelineMode.PLAN_LINKED
        return GuidelineMode.STANDALONE


class GuidelineInput(BaseModel):
    """Everything a guideline run is allowed to know, assembled once."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["1.0"] = "1.0"

    # -- provenance ---------------------------------------------------------
    project_id: uuid.UUID
    guideline_run_id: uuid.UUID
    bindings: GuidelineBindings
    #: Derived from `bindings` by the validator below. Present as a field
    #: rather than only as a property because it is serialised into the hash
    #: and read by every node.
    mode: GuidelineMode = GuidelineMode.STANDALONE

    # -- always present -----------------------------------------------------
    product_context: dict[str, Any] = Field(default_factory=dict)
    markets: list[Market] = Field(default_factory=list)
    #: The connector outputs S3-P2 onward fill in. Typed loosely here on
    #: purpose: the shapes belong to the connectors that produce them, and
    #: pinning them now would be this phase guessing at the next one's work.
    brand_assets: list[dict[str, Any]] = Field(default_factory=list)
    site_pages: list[dict[str, Any]] = Field(default_factory=list)
    creative_history: list[dict[str, Any]] = Field(default_factory=list)
    offer_records: list[dict[str, Any]] = Field(default_factory=list)
    disapproval_history: list[dict[str, Any]] = Field(default_factory=list)

    # -- present only when bound. Every one of these has an explicit `None`
    #    branch in its consumer, exercised by the unbound golden fixture.
    signoff_matrix: uuid.UUID | None = None
    prior_guideline_id: uuid.UUID | None = None
    compliance_guardrails: ComplianceGuardrails | None = None  # 1.1.5
    differentiation_claim: dict[str, Any] | None = None  # 1.3.4
    competitor_creative: list[CompetitorAd] | None = None  # 1.3.2
    channel_slate: ChannelSlate | None = None  # 2.3.1
    account_structure: AccountStructure | None = None  # 2.4.2
    #: 2.5.2's consent outcome. A dict rather than a model because Stage 02
    #: publishes the consent fields on `MeasurementPlan` rather than as a
    #: separate object — see Q-carried in the commit body.
    measurement_consent: dict[str, Any] | None = None

    # -- what was missing ---------------------------------------------------
    #: Rendered on the rulebook header and in every export. A rulebook built
    #: without legal guardrails must not look as authoritative as one built
    #: with them (§4.3 rule 2).
    unbound_inputs: list[str] = Field(default_factory=list)
    constants_version: str

    @model_validator(mode="after")
    def _mode_matches_the_bindings(self) -> GuidelineInput:
        derived = self.bindings.mode
        if self.mode != derived:
            raise ValueError(
                f"mode {self.mode.value!r} contradicts the bindings, which resolve to "
                f"{derived.value!r}. `mode` is derived from what actually bound and is "
                "never supplied by the caller — see PRD §4.5 rule 3."
            )
        return self

    @model_validator(mode="before")
    @classmethod
    def _derive_mode(cls, data: Any) -> Any:
        """Fill `mode` in when the caller left it out, which is the normal path.

        A caller that *did* supply one is checked by `_mode_matches_the_bindings`
        rather than silently overwritten: quietly correcting a wrong claim would
        hide the bug that produced it.
        """
        if isinstance(data, dict) and "mode" not in data and "bindings" in data:
            bindings = data["bindings"]
            if isinstance(bindings, GuidelineBindings):
                data = {**data, "mode": bindings.mode}
            elif isinstance(bindings, dict):
                data = {**data, "mode": GuidelineBindings(**bindings).mode}
        return data

    def content_hash(self) -> str:
        """sha256 over the canonical JSON. What `Run.input_hash` stores.

        `constants_version` is part of the object rather than mixed in
        afterwards, so a thresholds bump changes the hash and the deterministic
        nodes re-run instead of reusing an output shaped by the old numbers.
        """
        return hashlib.sha256(self.model_dump_json().encode("utf-8")).hexdigest()
