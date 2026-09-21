"""Stage 2.1 — set the goal and the numbers (Stage 02 PRD §11).

Four nodes. Two run unattended; `2.1.3 campaign_targets` carries gate **G1**
and `2.1.4 lead_definition` carries gate **G2**, and per §5.3 they run in
parallel and do not block each other — a slow sales approver must not hold up
the budget branch that G1 feeds.

**The shape every node in here follows, and why.** Global law 14 says the LLM
never does arithmetic: every figure comes from a registered `@formula`, is
persisted as a `PlanCalc` row plus a `derived` Evidence row, and is cited by
`calc_evidence_ids`. So each node splits in two, exactly as Stage 1.1 already
does for LTV and payback:

1. `gather()` reads the CRM and the account, calls `ctx.plan.calc.run(...)`,
   and returns the Stage 01 evidence **plus** the `derived` rows the
   calculation produced. That second half is what makes the citation checkable
   — the executor tests `calc_evidence_ids` against exactly this set.
2. `reason()` asks the model for labels, rationale and *selections* — which
   computed figure applies to which campaign — and merges the numbers in
   afterwards. The model never sees a figure it is allowed to restate, and
   `scripts/check_calc_isolation.py` fails the build if anything in this
   package so much as multiplies two fields.

**Selection, not calculation.** A target is not invented and it is not
averaged: it is one of the figures `economics.max_cpa_v1` produced, chosen by
the model for a named reason. `target_cpl_usd` is the ceiling after the safety
margin; `max_cpl_usd` is the ceiling itself, which is what a campaign is
allowed to pay while it is still learning. Picking between them is judgement,
which is the model's job; producing them is arithmetic, which is not.

**When the CRM cannot support a ceiling** — no margin stated, no closed-lost
rows, no deal values — nothing is guessed. `planning/crm.py` names the missing
field and the node stops with that name in its error, before a token is spent.
That is a deliberate deviation from PRD §18, which asks for an
`insufficient_input` status and a gate card the approver fills in by hand;
§9.1's `calc_evidence_ids: min_length=1` makes such an output unconstructable,
and `_insufficient` below argues the trade in full.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn

import structlog
from pydantic import BaseModel, Field

from agent.calc.registry import CalcError
from agent.db.models import ApprovalRequiredRole, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes import gather, prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.orchestrator.plan_calc import Calculation
from agent.planning import crm

log = structlog.get_logger(__name__)

CONVERSION_ACTION = "conversion_action"


class InsufficientPlanInput(CalcError):
    """The research and the CRM cannot supply a figure this node must cite.

    A `CalcError` subclass so it reads as "the arithmetic could not be done"
    rather than "the node is broken", and so the executor's retry ladder does
    what it should with it: three attempts against the same missing column
    produce the same message three times, which is cheap, and the last one is
    what a person reads.
    """


MAX_CPA = "economics.max_cpa_v1"
PAYBACK = "economics.payback_v1"

#: Which figure out of `economics.max_cpa_v1` each selector resolves to. The
#: model picks a key; the node reads the value. A mapping rather than a branch
#: so that adding a basis is a line here and not a new arithmetic path.
VALUE_BASIS: dict[str, str] = {
    "max_cpl": "max_cpl_usd",
    "target_cpl": "target_cpl_usd",
    "max_cpa_won": "max_cpa_won_usd",
    "acv": "acv_usd",
}

#: Which computed field is the *target* and which is the *ceiling* for each
#: KPI. `conv_volume` is deliberately absent: a volume target needs the demand
#: forecast, which is node 2.2.1 in S2-P3, and a volume target with no forecast
#: behind it would be the first number in the plan with nothing under it.
KPI_TARGET: dict[str, str] = {
    "cpl": "target_cpl_usd",
    "cpa": "target_cpa_won_usd",
    "roas": "target_roas",
}
KPI_CEILING: dict[str, str] = {
    "cpl": "max_cpl_usd",
    "cpa": "max_cpa_won_usd",
    "roas": "target_roas",
}

#: What a ramp month is allowed to aim at. `learning` is the raw ceiling — a
#: campaign in its learning period is permitted to pay the most the economics
#: allow; `steady` is the ceiling after the safety margin. Two computed values,
#: no interpolation, because an interpolated month would be arithmetic.
RAMP_PHASES = ("learning", "steady")

ValueBasis = Literal["max_cpl", "target_cpl", "max_cpa_won", "acv", "none"]
PrimaryKpi = Literal["cpl", "cpa", "roas"]
Objective = Literal["lead_gen", "demo", "trial", "brand_defense", "remarketing"]
Confidence = Literal["high", "medium", "low"]
Status = Literal["ok", "insufficient_input"]


#: Rows of the research keyword list one prompt may read. The full list is
#: thousands of rows and belongs to node 2.4.2, which groups it; Stage 2.1
#: only needs enough of it to tell one campaign's demand from another's.
DEMAND_ROWS = 40


def _plan_block(ctx: RunContext) -> str:
    """What this plan is being built from. Configuration, not evidence.

    Never cited — the ids in here belong to the research run, and while §4.3
    rule 2 does allow a plan `Claim` to cite a research `evidence_id`, the
    executor checks citations against what *this* node gathered. So the
    provenance is shown for the model's judgement and the citations come from
    the evidence blocks.
    """
    plan = ctx.require_plan()
    lines = [
        "PLAN SOURCE",
        f"  research run: {plan.input.research_run_id}",
        f"  accepted at: {plan.input.accepted_at.isoformat()}",
        f"  launch readiness: {plan.input.launch_readiness}",
        f"  constants version: {plan.constants.version}",
    ]
    if plan.input.degraded_sources:
        lines.append(f"  degraded sources: {', '.join(sorted(plan.input.degraded_sources))}")
    if plan.input.override_reason:
        lines.append(f"  started over a no-go verdict because: {plan.input.override_reason}")
    if plan.input.launch_blockers:
        lines.append("  unresolved launch blockers from research:")
        lines.extend(f"    - {claim.statement}" for claim in plan.input.launch_blockers)
    compliance = plan.input.business_context.compliance
    if compliance is not None and compliance.prohibited_claims:
        lines.append("  claims legal has prohibited: " + "; ".join(compliance.prohibited_claims))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# shared basis
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Economics:
    """The CRM ceiling, computed once per node and remembered for `reason()`.

    Held on `ctx.scratch` rather than recomputed, because `gather()` and
    `reason()` are two calls and the frame assembly is not free. The
    `Calculation` it carries is the same `PlanCalc` row for every node in this
    stage: `DerivedWriter` dedupes on `(plan_run_id, formula_id, inputs_hash)`,
    so 2.1.1, 2.1.2, 2.1.3 and 2.1.4 all cite one calculation rather than four
    copies of it.
    """

    found: gather.Gathered
    basis: crm.SegmentBasis
    ceiling: Calculation | None = None
    payback: Calculation | None = None
    gaps: list[str] = field(default_factory=list)

    @property
    def blended(self) -> dict[str, Any]:
        """The account-wide ceiling, or an empty mapping when there is none."""
        if self.ceiling is None:
            return {}
        blended = self.ceiling.value.get("blended")
        return blended if isinstance(blended, dict) else {}

    @property
    def by_segment(self) -> dict[str, dict[str, Any]]:
        """Per-segment ceilings, keyed by segment name."""
        if self.ceiling is None:
            return {}
        return {str(row["segment"]): row for row in self.ceiling.value.get("by_segment", [])}

    def figures(self, segment: str | None) -> dict[str, Any]:
        """One segment's computed ceiling, falling back to the blended one."""
        if segment:
            found = self.by_segment.get(segment)
            if found is not None:
                return found
        return self.blended

    @property
    def close_rate_basis(self) -> dict[str, str]:
        """Whether each segment's close rate was its own or the account's.

        Read back off the assembled frame, not off the calculation: it is an
        input, and `economics.max_cpa_v1` echoes only the columns its
        arithmetic uses. Losing it would make every segment read as if its own
        losses had been observed, which is precisely the thing
        `planning/crm.py` refuses to pretend.
        """
        if self.basis.frame.empty:
            return {}
        return {
            str(row["segment"]): str(row["close_rate_basis"])
            for row in self.basis.frame.to_dict(orient="records")
        }

    @property
    def calc_ids(self) -> list[uuid.UUID]:
        """What the node's `calc_evidence_ids` must contain."""
        return [item.id for item in (self.ceiling, self.payback) if item is not None]

    @property
    def evidence(self) -> list[Evidence]:
        """Stage 01 evidence plus the `derived` rows the calculation produced."""
        rows = list(self.found.evidence)
        rows.extend(item.evidence for item in (self.ceiling, self.payback) if item is not None)
        return rows

    @property
    def status(self) -> Status:
        return "ok" if self.ceiling is not None else "insufficient_input"


async def _economics(ctx: RunContext, *, needs: Sequence[gather.Need], payback: bool) -> Economics:
    """Read the CRM, build the ceiling, and record what could not be built.

    The single place any Stage 2.1 node reaches `calc/`. A `CalcError` is
    caught rather than raised: PRD §18 says thin CRM data makes 2.1.2 emit
    `insufficient_input` naming the missing field, not that it takes the run
    down — the plan carries the gap into `open_dependencies` and the gate asks
    a human for the number.
    """
    plan = ctx.require_plan()
    found = await gather.collect(ctx, *needs)
    basis = crm.segments_frame(
        won=found.payloads(crm.CRM_WON),
        lost=found.payloads(crm.CRM_LOST),
        business_context=plan.input.business_context,
        product_context=plan.input.product_context,
    )
    economics = Economics(found=found, basis=basis, gaps=list(basis.gaps))
    if not basis.usable:
        log.info("plan.economics_unavailable", node_id=ctx.node_id, gaps=economics.gaps)
        return economics

    try:
        economics.ceiling = await plan.calc.run(MAX_CPA, basis.frame)
    except CalcError as exc:
        economics.gaps.append(f"economics.max_cpa_v1 — {exc}")
        log.warning("plan.ceiling_failed", node_id=ctx.node_id, error=str(exc))
        return economics

    if payback and basis.has_contract_term:
        frame = crm.payback_frame(basis, economics.ceiling.value)
        if not frame.empty:
            try:
                economics.payback = await plan.calc.run(PAYBACK, frame)
            except CalcError as exc:
                # A missing payback is a missing column on the plan, not a dead
                # run: the ceiling it would have annotated is already computed.
                economics.gaps.append(f"economics.payback_v1 — {exc}")
                log.warning("plan.payback_failed", node_id=ctx.node_id, error=str(exc))
    return economics


def _crm_needs(*, conversion_actions: bool) -> list[gather.Need]:
    """The evidence Stage 2.1 reads. Every source here is read-only."""
    needs = [
        gather.Need(crm.CRM_WON, limit=2_000),
        gather.Need(crm.CRM_LOST, limit=2_000),
    ]
    if conversion_actions:
        needs.append(
            gather.Need(
                CONVERSION_ACTION,
                connector="google_ads",
                params={"kinds": [CONVERSION_ACTION]},
                limit=5_000,
                # A project with no Google Ads account still gets a taxonomy —
                # it is then a proposal for actions to create rather than a
                # reading of actions that exist, and the coverage note says so
                # (PRD §18, "Google Ads account not connected at all").
                optional=True,
            )
        )
    return needs


def _computed_for_prompt(economics: Economics) -> dict[str, Any]:
    """The computed table a Stage 2.1 prompt may reference but never restate."""
    return {
        "blended": economics.blended,
        "by_segment": list(economics.by_segment.values()),
        "payback": (economics.payback.value if economics.payback is not None else None),
        "close_rate_basis": (
            "per segment where losses were recorded, account-wide otherwise"
            if economics.ceiling is not None
            else None
        ),
        "unavailable": economics.gaps,
    }


# ---------------------------------------------------------------------------
# 2.1.1 — conversion_taxonomy
# ---------------------------------------------------------------------------


class ActionDraft(BaseModel):
    """One conversion action, as the model is asked to describe it.

    Carries a `value_basis` **selector** rather than a value. The node reads
    the figure that selector names out of `economics.max_cpa_v1`'s result, so
    the model decides *which* computed figure is the right value for this
    action and never decides what the figure is.
    """

    name: str = Field(description="The conversion action's name in Google Ads, or a proposed one.")
    ads_action_id: str | None = Field(
        default=None, description="Its id in the account. Null for an action that does not exist."
    )
    category: str = Field(description="Google's category, e.g. submit_lead_form, qualified_lead.")
    counting: Literal["one_per_click", "every"]
    value_model: Literal["fixed", "dynamic", "none"]
    value_basis: ValueBasis = Field(
        description=(
            "Which computed figure is this action worth? "
            "'max_cpl' the ceiling per lead, 'target_cpl' the same after the safety margin, "
            "'max_cpa_won' the ceiling per closed-won customer, 'acv' the measured contract "
            "value, 'none' for an action carrying no value."
        )
    )
    primary: bool = Field(description="Is this the action campaigns optimise toward?")
    include_in_conversions: bool
    rationale: str
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class DeprecateDraft(BaseModel):
    """An action that should stop counting, and why."""

    action: str
    reason: str
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class TaxonomyDraft(BaseModel):
    """What the model returns for 2.1.1. No figure appears in it."""

    actions: list[ActionDraft] = Field(default_factory=list)
    deprecate: list[DeprecateDraft] = Field(default_factory=list)
    #: Best first. An ordinal the model assigns, not a computed score — the
    #: same shape Stage 01 already uses for `impact_1_5`.
    ranking: list[str] = Field(
        default_factory=list, description="Action names, most important first."
    )


class ConversionAction(BaseModel):
    """One action, with its value merged in from the calculation."""

    name: str
    ads_action_id: str | None = None
    category: str
    counting: Literal["one_per_click", "every"]
    value_model: Literal["fixed", "dynamic", "none"]
    value_basis: ValueBasis
    assigned_value_usd: float | None = None
    lead_to_won_rate_pct: float | None = None
    rank: int
    primary: bool
    include_in_conversions: bool
    rationale: str
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class DeprecatedAction(BaseModel):
    """2.1.1's `deprecate[]`."""

    action: str
    reason: str
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class ConversionTaxonomyOutput(BaseModel):
    """2.1.1 output — which conversions count, in which order, at what value."""

    actions: list[ConversionAction] = Field(default_factory=list)
    deprecate: list[DeprecatedAction] = Field(default_factory=list)
    status: Status = "ok"
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(
        min_length=1,
        description="The `derived` rows every figure above resolves to (law 14).",
    )


class ConversionTaxonomyNode(LLMNode):
    """2.1.1 — what counts as a conversion, and what each one is worth."""

    spec = NodeSpec(
        id="2.1.1",
        name="conversion_taxonomy",
        stage="2.1",
        run_stage=RunStage.PLAN,
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=ConversionTaxonomyOutput,
        connectors=("google_ads",),
        calc=(MAX_CPA,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        economics = await _economics(ctx, needs=_crm_needs(conversion_actions=True), payback=False)
        ctx.scratch[self.spec.id] = economics
        return economics.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        economics: Economics = ctx.scratch[self.spec.id]
        if economics.ceiling is None:
            _insufficient(self.spec.id, economics)

        draft = await ctx.complete(
            TaxonomyDraft,
            system=prompts.system_prompt(
                "You decide which conversion actions a paid-search account should count, "
                "in what order, and which computed figure each one is worth. You never state "
                "a value yourself: you choose the basis and the value is filled in for you."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.evidence_block(
                    economics.found, CONVERSION_ACTION, title="conversion actions in the account"
                ),
                prompts.evidence_block(economics.found, crm.CRM_WON, title="closed-won deals"),
                prompts.computed_block(
                    "unit economics (yours to select from, never to restate)",
                    _computed_for_prompt(economics),
                ),
                prompts.coverage_block(economics.found),
                "TASK\n"
                "  List every conversion action this account should count, including any that "
                "do not exist yet and should be created. For each, choose `value_basis` from "
                "the computed figures above. Mark exactly one action `primary`. Then order "
                "every action name in `ranking`, most important first. List in `deprecate` any "
                "existing action that should stop counting toward conversions.",
                prompts.cite_from("EVIDENCE — conversion actions in the account"),
            ),
            task_class=self.spec.task_class,
        )

        blended = economics.blended
        rank_of = {name: index for index, name in enumerate(draft.ranking, start=1)}
        fallback = len(draft.ranking)
        actions = [
            ConversionAction(
                name=item.name,
                ads_action_id=item.ads_action_id,
                category=item.category,
                counting=item.counting,
                value_model=item.value_model,
                value_basis=item.value_basis,
                assigned_value_usd=_figure(blended, VALUE_BASIS.get(item.value_basis)),
                lead_to_won_rate_pct=_figure(blended, "lead_to_won_pct"),
                rank=rank_of.get(item.name, fallback),
                primary=item.primary,
                include_in_conversions=item.include_in_conversions,
                rationale=item.rationale,
                evidence_ids=item.evidence_ids,
            )
            for item in draft.actions
        ]
        return ConversionTaxonomyOutput(
            actions=sorted(actions, key=lambda action: action.rank),
            deprecate=[
                DeprecatedAction(
                    action=item.action, reason=item.reason, evidence_ids=item.evidence_ids
                )
                for item in draft.deprecate
            ],
            status=economics.status,
            open_gaps=economics.gaps,
            coverage=gather.coverage_notes(economics.found),
            calc_evidence_ids=economics.calc_ids,
        )


# ---------------------------------------------------------------------------
# 2.1.2 — unit_economics_ceiling
# ---------------------------------------------------------------------------


class MethodNotes(BaseModel):
    """The only thing the model contributes to 2.1.2 (PRD §11: pandas only)."""

    method_notes: str = Field(
        description=(
            "Two or three sentences on how these ceilings were arrived at and what "
            "would move them. Reference the figures; never restate or adjust one."
        )
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="What a reader should not conclude from these numbers.",
    )


class SegmentCeiling(BaseModel):
    """One segment's ceiling. Every field is computed; none is written by a model."""

    segment: str
    #: None on the blended row. The account-wide ceiling is derived from
    #: blended *inputs* rather than from a deal count, and adding the
    #: per-segment counts up here would be arithmetic in a plan node.
    deals: int | None = None
    acv_usd: float
    gross_margin_pct: float
    lead_to_won_pct: float
    close_rate_basis: str = Field(
        default="account",
        description="`segment` when this segment's own losses were recorded, else `account`.",
    )
    max_cpa_won_usd: float
    max_cpl_usd: float
    target_cpl_usd: float
    target_cpa_won_usd: float
    target_roas: float
    payback_months: float | None = None
    ltv_usd: float | None = None
    ltv_to_cac: float | None = None


class UnitEconomicsOutput(BaseModel):
    """2.1.2 output — the ceiling nothing downstream may exceed."""

    by_segment: list[SegmentCeiling] = Field(default_factory=list)
    blended: SegmentCeiling | None = None
    method_notes: str = ""
    caveats: list[str] = Field(default_factory=list)
    excluded: list[dict[str, Any]] = Field(
        default_factory=list, description="Segments the formula refused, each with its reason."
    )
    status: Status = "ok"
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class UnitEconomicsNode(LLMNode):
    """2.1.2 — the most this business can pay for a lead and for a customer."""

    spec = NodeSpec(
        id="2.1.2",
        name="unit_economics_ceiling",
        stage="2.1",
        run_stage=RunStage.PLAN,
        depends_on=("2.1.1",),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=UnitEconomicsOutput,
        calc=(MAX_CPA, PAYBACK),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        economics = await _economics(ctx, needs=_crm_needs(conversion_actions=False), payback=True)
        ctx.scratch[self.spec.id] = economics
        return economics.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        economics: Economics = ctx.scratch[self.spec.id]
        if economics.ceiling is None:
            _insufficient(self.spec.id, economics)

        payback_rows = _payback_rows(economics)
        basis_of = economics.close_rate_basis
        segments = [
            _ceiling_row(
                row,
                payback_rows.get(str(row.get("segment", ""))),
                basis=basis_of.get(str(row.get("segment", ""))),
            )
            for row in economics.ceiling.value.get("by_segment", [])
        ]
        blended_payback = (
            economics.payback.value.get("blended") if economics.payback is not None else None
        )
        blended = _ceiling_row({"segment": "blended", **economics.blended}, blended_payback)

        notes = await ctx.complete(
            MethodNotes,
            system=prompts.system_prompt(
                "You explain how a set of unit-economics ceilings was computed and what a "
                "reader should be careful about. You write prose only — every figure here "
                "was computed in pandas and is final."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "the ceilings (final — do not restate or adjust)",
                    {
                        "formula": "maxCPL = (ACV x grossMargin / targetCacRatio) x closeRate",
                        "by_segment": economics.ceiling.value.get("by_segment", []),
                        "blended": economics.blended,
                        "payback": (
                            economics.payback.value if economics.payback is not None else None
                        ),
                        "excluded": list(economics.ceiling.result.excluded),
                        "assembly_notes": economics.basis.notes,
                        "unavailable": economics.gaps,
                    },
                ),
                "TASK\n"
                "  Write `method_notes`: how these ceilings were arrived at, and what would "
                "move them most. Then list `caveats` — what a reader must not conclude from "
                "them. Name the gaps above if there are any.",
            ),
            task_class=self.spec.task_class,
        )
        return UnitEconomicsOutput(
            by_segment=segments,
            blended=blended,
            method_notes=notes.method_notes,
            caveats=notes.caveats,
            excluded=[dict(row) for row in economics.ceiling.result.excluded],
            status=economics.status,
            open_gaps=economics.gaps,
            coverage=gather.coverage_notes(economics.found),
            calc_evidence_ids=economics.calc_ids,
        )


# ---------------------------------------------------------------------------
# 2.1.3 ⛳ G1 — campaign_targets
# ---------------------------------------------------------------------------


class RampStageDraft(BaseModel):
    """One month of a campaign's ramp, as a phase rather than as a figure."""

    month: int = Field(ge=1, le=12)
    phase: Literal["learning", "steady"] = Field(
        description="`learning` aims at the raw ceiling; `steady` at the ceiling after the "
        "safety margin. There is no third option — an interpolated target would be a "
        "number with no calculation behind it."
    )


class ObjectiveDraft(BaseModel):
    """One campaign's objective, as the model is asked for it. No figures."""

    campaign_ref: str = Field(description="A stable reference, e.g. `nonbrand-us-lead-gen`.")
    objective: Objective
    primary_kpi: PrimaryKpi
    segment_ref: str | None = Field(
        default=None,
        description=(
            "Which computed segment's ceiling bounds this campaign, by exact segment name. "
            "Null means the blended account ceiling."
        ),
    )
    basis: str = Field(description="Why this objective and this KPI, from the evidence.")
    ramp: list[RampStageDraft] = Field(default_factory=list)
    confidence: Confidence
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class NorthStarDraft(BaseModel):
    """The one number the account is judged on — again as a selection."""

    metric: PrimaryKpi
    period: Literal["monthly", "quarterly"]
    segment_ref: str | None = None
    rationale: str = ""


class CampaignTargetsDraft(BaseModel):
    """What the model returns for 2.1.3."""

    objectives: list[ObjectiveDraft] = Field(default_factory=list)
    north_star: NorthStarDraft


class RampStage(BaseModel):
    """One ramp month with its computed target."""

    month: int
    phase: Literal["learning", "steady"]
    target: float | None = None


class CampaignObjective(BaseModel):
    """One campaign's target and the ceiling it may not cross."""

    campaign_ref: str
    objective: Objective
    primary_kpi: PrimaryKpi
    segment_ref: str | None = None
    target_value: float | None = None
    ceiling_value: float | None = None
    basis: str
    #: `computed` when the ceiling supplied the figure; `human_supplied` when
    #: an approver typed it on the G1 card (PRD §18, thin CRM data).
    target_source: Literal["computed", "human_supplied"] = "computed"
    ramp: list[RampStage] = Field(default_factory=list)
    confidence: Confidence
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class NorthStar(BaseModel):
    """The account-level target."""

    metric: PrimaryKpi
    target: float | None = None
    period: Literal["monthly", "quarterly"]
    segment_ref: str | None = None
    rationale: str = ""


class CampaignTargetsOutput(BaseModel):
    """2.1.3 output — ⛳ G1. One measurable target per campaign."""

    objectives: list[CampaignObjective] = Field(default_factory=list)
    north_star: NorthStar | None = None
    status: Status = "ok"
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class CampaignTargetsNode(LLMNode):
    """2.1.3 ⛳ G1 — one measurable target per campaign, bounded by the ceiling."""

    spec = NodeSpec(
        id="2.1.3",
        name="campaign_targets",
        stage="2.1",
        run_stage=RunStage.PLAN,
        depends_on=("2.1.1", "2.1.2"),
        gate=True,
        gate_key="G1",
        # §5.3 routes G1 to the marketing lead. `approver` is the role; which
        # person is `Project.settings.plan_approvers["G1"]`, and unset means
        # any approver may claim it.
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=CampaignTargetsOutput,
        # Re-run rather than re-read: `DerivedWriter` dedupes on
        # (plan_run_id, formula_id, inputs_hash), so this resolves to the row
        # 2.1.1 already wrote and this node can legitimately cite it as a
        # `derived` row it produced.
        calc=(MAX_CPA,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        economics = await _economics(ctx, needs=_crm_needs(conversion_actions=False), payback=False)
        ctx.scratch[self.spec.id] = economics
        return economics.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        economics: Economics = ctx.scratch[self.spec.id]
        if economics.ceiling is None:
            _insufficient(self.spec.id, economics)

        plan = ctx.require_plan()
        draft = await ctx.complete(
            CampaignTargetsDraft,
            system=prompts.system_prompt(
                "You set the target every paid-search campaign will be judged on. You choose "
                "the objective, the KPI and which computed ceiling bounds it. You never write "
                "a target value — you choose a segment and a KPI, and the figure is filled in."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "ceilings by segment (select from these; never restate one)",
                    _computed_for_prompt(economics),
                ),
                prompts.computed_block(
                    "the conversion taxonomy agreed in 2.1.1", ctx.output_of("2.1.1")
                ),
                prompts.computed_block(
                    "demand and account history from research",
                    {
                        "total_keywords": plan.input.demand_map.total_keywords,
                        "keyword_to_page_map": [
                            item.model_dump(mode="json")
                            for item in plan.input.demand_map.mapping[:DEMAND_ROWS]
                        ],
                        "top_keywords": [
                            item.model_dump(mode="json")
                            for item in plan.input.priced_keyword_list[:DEMAND_ROWS]
                        ],
                        "winners": [
                            item.model_dump(mode="json")
                            for item in plan.input.account_learnings.winners
                        ],
                        "losers": [
                            item.model_dump(mode="json")
                            for item in plan.input.account_learnings.losers
                        ],
                        "markets": [
                            market.model_dump(mode="json") for market in plan.input.markets
                        ],
                    },
                ),
                prompts.coverage_block(economics.found),
                "TASK\n"
                "  Propose one campaign per distinct objective and market that the research "
                "supports — no more. For each, set `primary_kpi` and name the `segment_ref` "
                "whose ceiling bounds it, or leave it null for the blended ceiling. Give a "
                "`ramp` of up to six months: `learning` while the campaign is still gathering "
                "conversions, `steady` once it is. Then choose the `north_star`.",
                prompts.cite_from("EVIDENCE — closed-won deals"),
            ),
            task_class=self.spec.task_class,
        )

        objectives = [_objective(item, economics) for item in draft.objectives]
        north_star = NorthStar(
            metric=draft.north_star.metric,
            target=_figure(
                economics.figures(draft.north_star.segment_ref),
                KPI_TARGET.get(draft.north_star.metric),
            ),
            period=draft.north_star.period,
            segment_ref=draft.north_star.segment_ref,
            rationale=draft.north_star.rationale,
        )
        return CampaignTargetsOutput(
            objectives=objectives,
            north_star=north_star,
            status=economics.status,
            open_gaps=economics.gaps,
            coverage=gather.coverage_notes(economics.found),
            calc_evidence_ids=economics.calc_ids,
        )


# ---------------------------------------------------------------------------
# 2.1.4 ⛳ G2 — lead_definition
# ---------------------------------------------------------------------------


class ScoringSignal(BaseModel):
    """One signal in the lead score, weighted on a 1-5 rank.

    A rank, not a measurement: PRD §11's own 2.5.3 asks the model for
    `impact_1_5`, `confidence_1_5` and `effort_1_5` in the same way. What law
    14 forbids is a *computed* number arriving without a calculation, and a
    weight is a judgement the G2 approver is there to ratify.
    """

    signal: str
    weight: int = Field(ge=1, le=5)
    source_field: str = Field(description="Where this signal is read from, e.g. a CRM field.")


class QualifiedLead(BaseModel):
    """What counts as a qualified lead."""

    required_signals: list[str] = Field(default_factory=list)
    disqualifiers: list[str] = Field(default_factory=list)
    scoring: list[ScoringSignal] = Field(default_factory=list)
    threshold: int = Field(ge=1, description="Total score at or above which a lead is qualified.")


class RoutingRule(BaseModel):
    """Who picks the lead up."""

    segment: str
    owner: str


class LeadDefinitionDraft(BaseModel):
    """What the model returns for 2.1.4. Ranks and policy only — no measurement."""

    qualified_lead: QualifiedLead
    sla_response_hours: int = Field(
        ge=1,
        le=168,
        description=(
            "A policy proposal, not a measurement. The G2 approver ratifies or changes it."
        ),
    )
    routing: list[RoutingRule] = Field(default_factory=list)
    observed_rejection_reasons: list[str] = Field(
        default_factory=list,
        description="Recurring close reasons from the closed-lost rows, as text. No counts.",
    )
    notes: str = ""


class LeadDefinitionOutput(BaseModel):
    """2.1.4 output — ⛳ G2. What sales will accept, and what it rejects."""

    qualified_lead: QualifiedLead | None = None
    #: Measured lead→won from the CRM. Named for what it is: `crm_won` and
    #: `crm_lost` are opportunities that reached a close, so this is the rate
    #: at which a worked opportunity is won.
    observed_sql_to_won_pct: float | None = None
    #: Deliberately not populated. See `MQL_GAP`.
    expected_mql_to_sql_pct: float | None = None
    sla_response_hours: int | None = None
    routing: list[RoutingRule] = Field(default_factory=list)
    observed_rejection_reasons: list[str] = Field(default_factory=list)
    notes: str = ""
    status: Status = "ok"
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


#: Why `expected_mql_to_sql_pct` comes back null. The canonical CRM export
#: (PRD §9.5) has eight columns and none of them is a lifecycle stage:
#: `crm_won` and `crm_lost` are both *opportunities*, so the only rate the data
#: supports is opportunity→won. Inventing an MQL→SQL rate from it would be the
#: kind of plausible number law 14 exists to keep out of a plan — so the field
#: stays null, the gap is named, and the G2 approver supplies it if they know
#: it (their edit is re-validated against this model before the branch resumes).
MQL_GAP = (
    "expected_mql_to_sql_pct — the CRM export carries no lifecycle stage, so MQL-to-SQL "
    "cannot be measured. `observed_sql_to_won_pct` is what the data does support. Enter the "
    "MQL-to-SQL rate on this gate if sales tracks it."
)


class LeadDefinitionNode(LLMNode):
    """2.1.4 ⛳ G2 — what counts as a qualified lead, and what disqualifies one."""

    spec = NodeSpec(
        id="2.1.4",
        name="lead_definition",
        stage="2.1",
        run_stage=RunStage.PLAN,
        # 2.1.1 only. §5.3: G1 and G2 run in parallel and do not block each
        # other, so this must not depend on 2.1.2 or 2.1.3.
        depends_on=("2.1.1",),
        gate=True,
        gate_key="G2",
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=LeadDefinitionOutput,
        calc=(MAX_CPA,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        economics = await _economics(ctx, needs=_crm_needs(conversion_actions=False), payback=False)
        ctx.scratch[self.spec.id] = economics
        return economics.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        economics: Economics = ctx.scratch[self.spec.id]
        if economics.ceiling is None:
            _insufficient(self.spec.id, economics)

        plan = ctx.require_plan()
        draft = await ctx.complete(
            LeadDefinitionDraft,
            system=prompts.system_prompt(
                "You write the definition of a qualified lead that sales and marketing will "
                "both sign. You work from who actually bought, who did not, and why. Weights "
                "and thresholds are ranks you judge; rates are measured for you."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "who bought, and the measured rate (final)",
                    {
                        "by_segment": list(economics.by_segment.values()),
                        "blended": economics.blended,
                        "won_deals": economics.basis.won_deals,
                        "lost_deals": economics.basis.lost_deals,
                    },
                ),
                prompts.computed_block(
                    "recurring close reasons on lost deals",
                    crm.rejection_reasons(economics.found.payloads(crm.CRM_LOST)),
                ),
                prompts.computed_block(
                    "the ideal customer profile and its exclusions, from research",
                    {
                        "segments": [
                            item.model_dump(mode="json")
                            for item in plan.input.business_context.segments
                        ],
                        "exclusions": [
                            item.model_dump(mode="json")
                            for item in plan.input.business_context.exclusions
                        ],
                    },
                ),
                prompts.computed_block(
                    "the conversion taxonomy agreed in 2.1.1", ctx.output_of("2.1.1")
                ),
                prompts.coverage_block(economics.found),
                "TASK\n"
                "  Define the qualified lead: the signals it must carry, what disqualifies "
                "it, a 1-5 weight per signal and the total score at which it qualifies. "
                "Propose an SLA in hours, route each segment to an owner, and list the close "
                "reasons that recur — as text, with no counts.",
                prompts.cite_from("EVIDENCE — closed-won deals"),
            ),
            task_class=self.spec.task_class,
        )

        gaps = list(economics.gaps)
        gaps.append(MQL_GAP)
        return LeadDefinitionOutput(
            qualified_lead=draft.qualified_lead,
            observed_sql_to_won_pct=_figure(economics.blended, "lead_to_won_pct"),
            expected_mql_to_sql_pct=None,
            sla_response_hours=draft.sla_response_hours,
            routing=draft.routing,
            observed_rejection_reasons=draft.observed_rejection_reasons,
            notes=draft.notes,
            status=economics.status,
            open_gaps=gaps,
            coverage=gather.coverage_notes(economics.found),
            calc_evidence_ids=economics.calc_ids,
        )


# ---------------------------------------------------------------------------
# merge helpers — lookups only, never arithmetic
# ---------------------------------------------------------------------------


def _figure(computed: dict[str, Any], key: str | None) -> float | None:
    """One computed figure by name. None when it was not computed.

    Every number in this module's output goes through here. It is a dictionary
    read and a float cast — no operator touches a value — which is what makes
    `check_calc_isolation.py` able to prove the package does no arithmetic.
    """
    if not key:
        return None
    value = computed.get(key)
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _payback_rows(economics: Economics) -> dict[str, dict[str, Any]]:
    """`economics.payback_v1`'s per-segment result, keyed by segment."""
    if economics.payback is None:
        return {}
    return {str(row["segment"]): row for row in economics.payback.value.get("by_segment", [])}


def _ceiling_row(
    computed: dict[str, Any], payback: dict[str, Any] | None, *, basis: str | None = None
) -> SegmentCeiling:
    """One `SegmentCeiling` assembled from the two calculations.

    `basis` comes from the assembled frame rather than from `computed`, for
    the reason `Economics.close_rate_basis` gives.
    """
    paid = payback or {}
    counted = _figure(computed, "deals")
    return SegmentCeiling(
        segment=str(computed.get("segment", "blended")),
        deals=None if counted is None else int(counted),
        acv_usd=_figure(computed, "acv_usd") or 0.0,
        gross_margin_pct=_figure(computed, "gross_margin_pct") or 0.0,
        lead_to_won_pct=_figure(computed, "lead_to_won_pct") or 0.0,
        close_rate_basis=basis or "account",
        max_cpa_won_usd=_figure(computed, "max_cpa_won_usd") or 0.0,
        max_cpl_usd=_figure(computed, "max_cpl_usd") or 0.0,
        target_cpl_usd=_figure(computed, "target_cpl_usd") or 0.0,
        target_cpa_won_usd=_figure(computed, "target_cpa_won_usd") or 0.0,
        target_roas=_figure(computed, "target_roas") or 0.0,
        payback_months=_figure(paid, "payback_months"),
        ltv_usd=_figure(paid, "ltv_usd"),
        ltv_to_cac=_figure(paid, "ltv_to_cac"),
    )


def _objective(draft: ObjectiveDraft, economics: Economics) -> CampaignObjective:
    """One campaign objective, with its target and ceiling selected, not computed."""
    figures = economics.figures(draft.segment_ref)
    target = _figure(figures, KPI_TARGET.get(draft.primary_kpi))
    ceiling = _figure(figures, KPI_CEILING.get(draft.primary_kpi))
    by_phase = {"learning": ceiling, "steady": target}
    return CampaignObjective(
        campaign_ref=draft.campaign_ref,
        objective=draft.objective,
        primary_kpi=draft.primary_kpi,
        segment_ref=draft.segment_ref,
        target_value=target,
        ceiling_value=ceiling,
        basis=draft.basis,
        target_source="computed" if target is not None else "human_supplied",
        ramp=[
            RampStage(month=stage.month, phase=stage.phase, target=by_phase.get(stage.phase))
            for stage in sorted(draft.ramp, key=lambda stage: stage.month)
        ],
        confidence=draft.confidence,
        evidence_ids=draft.evidence_ids,
    )


def _insufficient(node_id: str, economics: Economics) -> NoReturn:
    """Stop this node, naming the field that was missing.

    **A deviation from PRD §18, argued rather than overlooked.** §18 says thin
    CRM data makes 2.1.2 emit `status='insufficient_input'` and 2.1.3's gate
    card show the ceiling as unknown, so an approver can type the target in as
    `basis: human_supplied`. That behaviour and §9.1 item 4 cannot both hold
    as written: every output model in this stage declares
    `calc_evidence_ids: Field(min_length=1)` — the invariant that makes "a
    number with no calculation behind it fails schema validation" true — and
    an output with no calculation behind *any* of its numbers cannot satisfy
    it. There is nothing honest to cite.

    So the node fails, and the failure names the field: "no product in the
    research report states a gross margin" is a sentence somebody can act on,
    which an empty gate card is not. No tokens are spent — the model is never
    called — and the branch below simply does not run, so nobody is asked a
    question that has no answerable shape.

    Resolving it properly needs one of two decisions this phase should not
    take on its own, and both are recorded as open questions: either the
    output contract grows an explicit "no calculation, human to supply"
    state, or `calc_evidence_ids` becomes conditional on the figures actually
    being present.
    """
    detail = "; ".join(economics.gaps) or "no reason was recorded"
    log.warning("plan.insufficient_input", node_id=node_id, gaps=economics.gaps)
    raise InsufficientPlanInput(
        f"node {node_id} cannot produce a figure it is required to cite: {detail}"
    )


conversion_taxonomy = ConversionTaxonomyNode()
unit_economics_ceiling = UnitEconomicsNode()
campaign_targets = CampaignTargetsNode()
lead_definition = LeadDefinitionNode()
