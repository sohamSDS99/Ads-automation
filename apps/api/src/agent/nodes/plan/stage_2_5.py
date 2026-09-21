"""Stage 2.5 — decide how we measure, and what we test (Stage 02 PRD §11).

All three nodes. 2.5.1 and 2.5.2 hang off 2.1.1 and 2.1.4 and shipped in
S2-P5a, before the budget and structure branches existed; `2.5.3
experiment_backlog` reads 2.4.2, 2.2.4 and 2.3.1 and waited for S2-P5b. That
split is why the phase was halved — §11's edge list makes the measurement
branch independent of the critical path, and building the independent half
first is what kept a phase shippable.

**2.5.3 is the one node whose honest answer is often "you cannot test that".**
It sizes every candidate against the campaign's own forecast, and at ordinary
B2B volumes a 20% lift on a 4% baseline needs seven months of traffic. The
node ranks those tests anyway and funds none of them, with the horizon named
on each. A backlog that promised a readout in three weeks would be worse than
no backlog, because a quarter would get planned around it.

**The numbers these nodes carry are not in PRD §9.2.** That table stops at nine
formulas, all of them serving the budget and structure branches, yet §11
mandates `reconciliation[].tolerance_pct`, `upload.lag_days` and
`upload.backfill_days` here. `agent/calc/measurement.py` is the resolution and
argues itself; what matters at this layer is that the pattern is unchanged
from Stage 2.1:

1. `gather()` reads the account, folds it in `planning/tracking.py`, calls
   `ctx.plan.calc.run(...)` and returns the Stage 01 evidence **plus** the
   `derived` rows the calculation produced. The executor checks
   `calc_evidence_ids` against exactly that second set.
2. `reason()` asks the model for names, owners, cadences and *selections* —
   which computed option applies — and merges the figures in afterwards.

**§8.4 and §11 disagree about 2.5.2, and §11 wins.** §8.4 says "G2
(`lead_definition`) is off the critical path, so 2.5.1 and 2.5.2 keep
executing while sales deliberates". §11's edge list says
`2.5.2←{2.1.1,2.1.4}`, and a gate halts the branch below it — so 2.5.2
cannot run while G2 is pending. Both sentences cannot be true. The edge is
kept, for a reason beyond §11 being the formal spec: 2.5.2 maps CRM stages
onto conversion actions, and *which stage counts as a qualified lead* is
precisely what G2 decides. Uploading a "qualified lead" conversion before
sales has agreed what one is would be building on sand. §8.4's sentence is
right about 2.5.1 and wrong about 2.5.2, and
`tests/integration/test_plan_stage_2_5.py` pins both halves of that.

**What the model is not allowed to write here.** Two things, and both are
compliance rather than style:

* **The consent scope.** PRD §13 and invariant PC1 make gate 1.5.3
  authoritative over which markets may appear in an offline-conversion plan.
  `tracking.consent_scope` computes `markets_allowed`, `markets_blocked` and
  the lawful basis, and 2.5.2 copies them. A model that could write that field
  is a model that could add a blocked market to it, and the critique catching
  it later is a worse control than the schema never offering it.
* **The metric set it reconciles.** 2.5.1 emits one reconciliation rule per
  computed metric and asks the model only for the cadence and the owner. A
  model free to choose the list can quietly drop the metric with the ugliest
  tolerance.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, NoReturn

import pandas as pd
import structlog
from pydantic import BaseModel, Field

from agent.calc.registry import CalcError
from agent.db.models import Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes import gather, prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.orchestrator.plan_calc import Calculation
from agent.planning import crm, experiments, tracking

if TYPE_CHECKING:  # pragma: no cover — a type name only, and `nodes.base` does the same
    from agent.schemas.plan_input import PlanInput

log = structlog.get_logger(__name__)

RECONCILIATION = "measurement.reconciliation_v1"
UPLOAD_WINDOW = "measurement.upload_window_v1"
SAMPLE_SIZE = "power.sample_size_v1"
ICE_RANK = "experiments.ice_rank_v1"

Status = Literal["ok", "insufficient_input"]
PrimarySource = Literal["google_ads", "ga4", "crm", "warehouse"]
UploadMethod = Literal["ads_api", "sheets_link", "manual_csv"]
Cadence = Literal["daily", "weekly", "monthly"]
Refresh = Literal["realtime", "daily", "weekly", "monthly"]

#: §11's `variable` vocabulary for 2.5.3, and the metric each is read on.
#: Closed here and open in `planning/experiments.py`, which maps the same seven
#: names — a rule that raised an eighth would fail validation here rather than
#: reach the plan as a test nobody can interpret.
Variable = Literal[
    "bid_strategy", "landing_page", "audience", "match_type", "budget", "ad_schedule", "geo"
]
TestMetric = Literal["cpl", "cpa", "roas", "conv_volume", "cvr"]

#: What an unrated candidate is scored at. The midpoint of the 1–5 scale, so it
#: ranks below anything the model was positive about and above anything it
#: dismissed, which is the honest position for "nobody said".
NEUTRAL_RATING = 3

#: PRD §21 Q4's answer, and the fallback when the model selects a path that is
#: not on the computed table: "manual CSV monthly, as the honest floor".
DEFAULT_METHOD: UploadMethod = "manual_csv"
DEFAULT_CADENCE: Cadence = "monthly"


def _plan_block(ctx: RunContext) -> str:
    """What this plan is being built from. Configuration, never cited.

    The same block Stage 2.1 shows, minus the CRM-specific lines it does not
    need, plus the two facts that decide a measurement plan: which markets are
    in scope, and whether any of them is one where consent limits tagging.
    """
    plan = ctx.require_plan()
    markets = [market.country for market in plan.input.markets]
    eu = sorted({code for code in markets if code.upper() in tracking.EEA_MARKETS})
    lines = [
        "PLAN SOURCE",
        f"  research run: {plan.input.research_run_id}",
        f"  accepted at: {plan.input.accepted_at.isoformat()}",
        f"  launch readiness: {plan.input.launch_readiness}",
        f"  constants version: {plan.constants.version}",
        f"  markets: {', '.join(markets) or '(none declared)'}",
    ]
    if eu:
        lines.append(
            "  consent-limited markets in scope: "
            + ", ".join(eu)
            + " — the measurement plan must name the consent-signal mechanism in use"
        )
    if plan.input.degraded_sources:
        lines.append(f"  degraded sources: {', '.join(sorted(plan.input.degraded_sources))}")
    if plan.input.launch_blockers:
        lines.append("  unresolved launch blockers from research:")
        lines.extend(f"    - {claim.statement}" for claim in plan.input.launch_blockers)
    return "\n".join(lines)


def _tracking_probe(ctx: RunContext) -> dict[str, Any]:
    """Stage 01 node 1.5.2's findings, as the model should read them."""
    readiness = ctx.require_plan().input.readiness
    check = readiness.synthetic_check
    return {
        "conversion_actions": [
            {
                "name": action.name,
                "status": action.status,
                "staleness_days": action.staleness_days,
            }
            for action in readiness.conversion_actions
        ],
        "synthetic_check": None if check is None else check.model_dump(mode="json"),
        "alerts": readiness.alerts,
    }


# ---------------------------------------------------------------------------
# shared output shapes
# ---------------------------------------------------------------------------


class Prerequisite(BaseModel):
    """Something that must happen before the plan's measurement works.

    Shared by both nodes. `blocking` is what `CampaignPlan.open_dependencies`
    carries forward and what 2.6.2's critique reads, so a node that raises one
    is a node that can stop a freeze.
    """

    task: str
    owner: str = Field(description="A role or a named person. 'unassigned' if nobody owns it yet.")
    blocking: bool


# ---------------------------------------------------------------------------
# 2.5.1 — measurement_source_of_truth
# ---------------------------------------------------------------------------


class MetricDefinition(BaseModel):
    """One metric, defined so two people compute it the same way."""

    metric: str
    formula: str = Field(description="How it is computed, in words or as an expression.")
    source_field: str = Field(description="The field or report it is read from.")
    owner: str
    refresh: Refresh


class MetricOwner(BaseModel):
    """The model's annotation of one computed reconciliation row."""

    metric: str = Field(description="Exactly as it appears in the computed table.")
    cadence: Cadence
    owner: str


class DiscrepancyDraft(BaseModel):
    """A divergence a person knows about that no probe can see."""

    metric: str
    systems: list[str] = Field(default_factory=list)
    cause: str
    tolerated: bool
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class DashboardSpec(BaseModel):
    """What the weekly report shows, at what grain, how often."""

    fields: list[str] = Field(default_factory=list)
    grain: str
    cadence: Cadence


class MeasurementDraft(BaseModel):
    """What the model returns for 2.5.1. No tolerance appears in it."""

    primary_source: PrimarySource = Field(
        description="Which system settles an argument about what happened."
    )
    rationale: str
    metric_definitions: list[MetricDefinition] = Field(default_factory=list)
    reconciliation_owners: list[MetricOwner] = Field(
        default_factory=list,
        description="One per metric in the computed table. Cadence and owner only.",
    )
    known_discrepancies: list[DiscrepancyDraft] = Field(default_factory=list)
    dashboard_spec: DashboardSpec
    consent_signal_mechanism: str | None = Field(
        default=None,
        description=(
            "Named only if the evidence shows one is in use — Consent Mode v2, a CMP, "
            "server-side tagging. Null when none is in evidence. Do not propose one here."
        ),
    )


class ReconciliationRule(BaseModel):
    """One metric's tolerance, with the reason it is that wide."""

    metric: str
    systems: str
    tolerance_pct: float
    cadence: Cadence | None = None
    owner: str = "unassigned"
    #: Which term set the tolerance: the policy floor, the measured share of
    #: volume nobody can tie out, or the modelled-conversion allowance.
    binding_driver: str
    observed_gap_pct: float


class KnownDiscrepancy(BaseModel):
    """2.5.1's `known_discrepancies[]`."""

    metric: str
    systems: list[str] = Field(default_factory=list)
    cause: str
    tolerated: bool
    #: Set when the node computed this one rather than the model proposing it.
    observed: bool = False
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class ConsentSignal(BaseModel):
    """PRD §13: where EU markets are in scope, the mechanism must be named."""

    required: bool
    markets: list[str] = Field(default_factory=list)
    mechanism: str | None = None


class MeasurementSourceOfTruthOutput(BaseModel):
    """2.5.1 output — which system is right, and how far apart the others may be."""

    primary_source: PrimarySource
    rationale: str = ""
    metric_definitions: list[MetricDefinition] = Field(default_factory=list)
    reconciliation: list[ReconciliationRule] = Field(default_factory=list)
    known_discrepancies: list[KnownDiscrepancy] = Field(default_factory=list)
    dashboard_spec: DashboardSpec | None = None
    consent_signal: ConsentSignal | None = None
    prerequisites: list[Prerequisite] = Field(default_factory=list)
    status: Status = "ok"
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(
        min_length=1,
        description="The `derived` rows every tolerance above resolves to (law 14).",
    )


@dataclass(slots=True)
class Measurement:
    """What 2.5.1's `gather()` assembled, held for `reason()`."""

    found: gather.Gathered
    basis: tracking.MetricBasis
    tolerances: Calculation | None = None
    gaps: list[str] = field(default_factory=list)

    @property
    def rows(self) -> list[dict[str, Any]]:
        """`measurement.reconciliation_v1`'s per-metric result."""
        if self.tolerances is None:
            return []
        computed = self.tolerances.value.get("by_metric", [])
        return [row for row in computed if isinstance(row, dict)]

    @property
    def floor_pct(self) -> float:
        """The tolerance a metric falls back to when the model names an unknown one."""
        if self.tolerances is None:
            return 0.0
        return _figure(self.tolerances.value, "floor_pct") or 0.0

    @property
    def calc_ids(self) -> list[uuid.UUID]:
        return [] if self.tolerances is None else [self.tolerances.id]

    @property
    def evidence(self) -> list[Evidence]:
        rows = list(self.found.evidence)
        if self.tolerances is not None:
            rows.append(self.tolerances.evidence)
        return rows


class MeasurementSourceOfTruthNode(LLMNode):
    """2.5.1 — which system settles an argument, and what the others may differ by."""

    spec = NodeSpec(
        id="2.5.1",
        name="measurement_source_of_truth",
        stage="2.5",
        run_stage=RunStage.PLAN,
        # §11: 2.5.1←{2.1.1}. The tracking probe and the account snapshot are
        # inputs too, but they arrive through `PlanInput` and the evidence
        # store rather than as DAG edges.
        depends_on=("2.1.1",),
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=MeasurementSourceOfTruthOutput,
        connectors=("google_ads",),
        calc=(RECONCILIATION,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        plan = ctx.require_plan()
        found = await gather.collect(
            ctx,
            gather.Need(
                tracking.CONVERSION_ACTION,
                connector="google_ads",
                params={"kinds": [tracking.CONVERSION_ACTION]},
                limit=5_000,
                # A project with no Google Ads account still gets a measurement
                # plan; it is then a proposal rather than a reading, and every
                # tolerance in it is the policy floor with a gap saying so.
                optional=True,
            ),
        )
        actions = tracking.fold_actions(found.payloads(tracking.CONVERSION_ACTION))
        basis = tracking.metrics_frame(
            actions=actions,
            readiness=plan.input.readiness,
            markets=[market.country for market in plan.input.markets],
            constants=plan.constants,
        )
        measurement = Measurement(found=found, basis=basis, gaps=list(basis.gaps))
        measurement.tolerances = await plan.calc.run(RECONCILIATION, basis.frame)
        ctx.scratch[self.spec.id] = measurement
        return measurement.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        measurement: Measurement = ctx.scratch[self.spec.id]
        computed = measurement.rows

        draft = await ctx.complete(
            MeasurementDraft,
            system=prompts.system_prompt(
                "You decide which system is the source of truth for a paid-search account, "
                "define each reported metric so two people compute it identically, and assign "
                "an owner and a cadence to each reconciliation. You never state a tolerance: "
                "the band is computed for you and you annotate it."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.evidence_block(
                    measurement.found,
                    tracking.CONVERSION_ACTION,
                    title="conversion actions in the account",
                ),
                prompts.computed_block(
                    "the tracking probe from research (node 1.5.2)", _tracking_probe(ctx)
                ),
                prompts.computed_block(
                    "the conversion taxonomy agreed in 2.1.1", ctx.output_of("2.1.1")
                ),
                prompts.computed_block(
                    "reconciliation tolerances (final — annotate, never restate)", computed
                ),
                prompts.computed_block(
                    "divergences already observed", measurement.basis.discrepancies
                ),
                prompts.coverage_block(measurement.found),
                "TASK\n"
                "  Choose the primary source and say why in one paragraph. Define every "
                "metric the plan reports: its formula, the field it is read from, its owner "
                "and how often it refreshes. For each metric in the computed tolerance table, "
                "return a cadence and an owner under the same metric name. Add any "
                "discrepancy you know of that the observed list above does not already carry. "
                "Specify the dashboard. Name the consent-signal mechanism only if the "
                "evidence shows one is in use.",
                prompts.cite_from("EVIDENCE — conversion actions in the account"),
            ),
            task_class=self.spec.task_class,
        )

        annotated = {item.metric: item for item in draft.reconciliation_owners}
        gaps = list(measurement.gaps)
        rules: list[ReconciliationRule] = []
        for row in computed:
            metric = str(row.get("metric", ""))
            owner = annotated.get(metric)
            if owner is None:
                gaps.append(
                    f"{metric} — no owner or cadence was assigned to this reconciliation; "
                    "the gate should name one."
                )
            rules.append(
                ReconciliationRule(
                    metric=metric,
                    systems=str(row.get("systems", "")),
                    tolerance_pct=_figure(row, "tolerance_pct") or measurement.floor_pct,
                    cadence=owner.cadence if owner else None,
                    owner=owner.owner if owner else "unassigned",
                    binding_driver=str(row.get("binding_driver", "")),
                    observed_gap_pct=_figure(row, "observed_gap_pct") or 0.0,
                )
            )
        reconciled = {str(row.get("metric")) for row in computed}
        # `.difference` rather than `-`: the isolation guard reads a `-` with a
        # field on either side as arithmetic, and it is right to — the cost of
        # the rule being blunt is one method call here.
        for unknown in sorted(set(annotated).difference(reconciled)):
            gaps.append(
                f"{unknown} — the plan was given an owner for a metric with no computed "
                "tolerance, so it is not reconciled."
            )

        discrepancies = [
            KnownDiscrepancy(
                metric=str(item.get("metric", "")),
                systems=list(item.get("systems") or []),
                cause=str(item.get("cause", "")),
                # An unreconcilable volume is not something a plan tolerates.
                # It is named so somebody fixes it, and 2.6.2 reads it as such.
                tolerated=False,
                observed=True,
            )
            for item in measurement.basis.discrepancies
        ]
        discrepancies.extend(
            KnownDiscrepancy(
                metric=item.metric,
                systems=item.systems,
                cause=item.cause,
                tolerated=item.tolerated,
                observed=False,
                evidence_ids=item.evidence_ids,
            )
            for item in draft.known_discrepancies
        )

        eu = list(measurement.basis.eu_markets)
        mechanism = (draft.consent_signal_mechanism or "").strip() or None
        prerequisites: list[Prerequisite] = []
        if eu and mechanism is None:
            # PRD §13: this is the one thing 2.5.1 can block a freeze on.
            prerequisites.append(
                Prerequisite(
                    task=(
                        "Name the consent-signal mechanism in use for "
                        + ", ".join(eu)
                        + " (Consent Mode v2, a CMP, or server-side tagging). Without it, "
                        "conversions in these markets are modelled and neither system's "
                        "number can be reconciled to the other."
                    ),
                    owner="unassigned",
                    blocking=True,
                )
            )

        return MeasurementSourceOfTruthOutput(
            primary_source=draft.primary_source,
            rationale=draft.rationale,
            metric_definitions=draft.metric_definitions,
            reconciliation=rules,
            known_discrepancies=discrepancies,
            dashboard_spec=draft.dashboard_spec,
            consent_signal=ConsentSignal(required=bool(eu), markets=eu, mechanism=mechanism),
            prerequisites=prerequisites,
            status="ok",
            open_gaps=gaps,
            coverage=gather.coverage_notes(measurement.found),
            calc_evidence_ids=measurement.calc_ids,
        )


# ---------------------------------------------------------------------------
# 2.5.2 — offline_conversion_plan
# ---------------------------------------------------------------------------


class GclidCaptureDraft(BaseModel):
    """Where the click id is caught and what it is written into.

    `present_today` is absent on purpose: it is observed, not proposed, and
    `tracking.gclid_signal` is the only thing allowed to answer it.
    """

    point: str = Field(description="Where in the journey the click id is captured.")
    form_field: str = Field(description="The form field or parameter it is read from.")
    storage_object: str = Field(description="The CRM object and field it is written to.")


class StageMapping(BaseModel):
    """One CRM stage, mapped to the conversion action it uploads as."""

    crm_stage: str
    ads_conversion_action: str
    value_field: str


class OfflinePlanDraft(BaseModel):
    """What the model returns for 2.5.2. It selects a path; it states no day count."""

    method: UploadMethod = Field(description="Chosen from the computed options table.")
    cadence: Cadence = Field(description="Chosen from the computed options table.")
    method_rationale: str
    gclid_capture: GclidCaptureDraft
    stage_map: list[StageMapping] = Field(default_factory=list)
    prerequisites: list[Prerequisite] = Field(default_factory=list)
    notes: str = ""


class GclidCapture(BaseModel):
    """2.5.2's `gclid_capture`, with the observation merged in."""

    point: str
    form_field: str
    storage_object: str
    present_today: bool
    #: What `present_today` rests on, and — when it is False — why that is the
    #: absence of evidence rather than evidence of absence.
    basis: str
    caveat: str | None = None
    retention_days: float | None = None
    observed_fields: list[str] = Field(default_factory=list)


class UploadPlan(BaseModel):
    """The chosen path, with every figure read off the computed table."""

    method: UploadMethod
    cadence: Cadence
    lag_days: float
    backfill_days: float
    headroom_days: float
    fits_click_window: bool
    click_upload_window_days: float
    history_limits_backfill: bool
    rationale: str = ""


class ConsentScopeOut(BaseModel):
    """Computed from gate 1.5.3. The model never writes a field of this."""

    markets_allowed: list[str] = Field(default_factory=list)
    markets_blocked: list[str] = Field(default_factory=list)
    basis: list[str] = Field(default_factory=list)
    markets_unstated: list[str] = Field(default_factory=list)


class OfflineConversionPlanOutput(BaseModel):
    """2.5.2 output — how a closed deal gets back into the ad account."""

    gclid_capture: GclidCapture | None = None
    upload: UploadPlan | None = None
    stage_map: list[StageMapping] = Field(default_factory=list)
    consent: ConsentScopeOut | None = None
    prerequisites: list[Prerequisite] = Field(default_factory=list)
    notes: str = ""
    status: Status = "ok"
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(
        min_length=1,
        description="The `derived` rows the lag, backfill and retention resolve to (law 14).",
    )


@dataclass(slots=True)
class OfflineBasis:
    """What 2.5.2's `gather()` assembled, held for `reason()`."""

    found: gather.Gathered
    consent: tracking.ConsentScope
    gclid: tracking.GclidSignal
    history_days: float | None
    window: Calculation | None = None
    gaps: list[str] = field(default_factory=list)

    @property
    def options(self) -> list[dict[str, Any]]:
        if self.window is None:
            return []
        computed = self.window.value.get("options", [])
        return [row for row in computed if isinstance(row, dict)]

    def option(self, method: str, cadence: str) -> dict[str, Any] | None:
        """One row of the computed table, or None when the pair is not on it."""
        for row in self.options:
            if row.get("method") == method and row.get("cadence") == cadence:
                return row
        return None

    @property
    def calc_ids(self) -> list[uuid.UUID]:
        return [] if self.window is None else [self.window.id]

    @property
    def evidence(self) -> list[Evidence]:
        rows = list(self.found.evidence)
        if self.window is not None:
            rows.append(self.window.evidence)
        return rows


class OfflineConversionPlanNode(LLMNode):
    """2.5.2 — how a closed-won deal becomes a conversion Google can optimise toward."""

    spec = NodeSpec(
        id="2.5.2",
        name="offline_conversion_plan",
        stage="2.5",
        run_stage=RunStage.PLAN,
        # §11: 2.5.2←{2.1.1, 2.1.4}. The consent gate and the crawled forms are
        # Stage 01 facts and arrive through `PlanInput` and the evidence store.
        depends_on=("2.1.1", "2.1.4"),
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=OfflineConversionPlanOutput,
        connectors=("google_ads",),
        calc=(UPLOAD_WINDOW,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        plan = ctx.require_plan()
        found = await gather.collect(
            ctx,
            gather.Need(
                tracking.CONVERSION_ACTION,
                connector="google_ads",
                params={"kinds": [tracking.CONVERSION_ACTION]},
                limit=5_000,
                optional=True,
            ),
            # No connector: law 13 forbids re-crawling for a fact research
            # already established, so this reads the store or reports nothing.
            gather.Need(tracking.PAGE, limit=500),
            gather.Need(crm.CRM_WON, limit=2_000),
            gather.Need(crm.CRM_LOST, limit=2_000),
        )
        actions = tracking.fold_actions(found.payloads(tracking.CONVERSION_ACTION))
        history = tracking.history_days(
            [*found.payloads(crm.CRM_WON), *found.payloads(crm.CRM_LOST)]
        )
        basis = OfflineBasis(
            found=found,
            consent=tracking.consent_scope(
                plan.input.readiness, [market.country for market in plan.input.markets]
            ),
            gclid=tracking.gclid_signal(actions=actions, pages=found.payloads(tracking.PAGE)),
            history_days=history,
        )
        if history is None:
            basis.gaps.append(
                "created_at — no CRM row carries a usable date, so the backfill depth is "
                "bounded by Google's upload window alone rather than by the history we hold."
            )
        basis.window = await plan.calc.run(
            UPLOAD_WINDOW,
            tracking.upload_options_frame(plan.constants),
            observed_history_days=history,
        )
        ctx.scratch[self.spec.id] = basis
        return basis.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        basis: OfflineBasis = ctx.scratch[self.spec.id]
        consent = basis.consent

        draft = await ctx.complete(
            OfflinePlanDraft,
            system=prompts.system_prompt(
                "You design the pipeline that carries a closed deal back into a Google Ads "
                "account as a conversion. You choose an upload path from a computed table and "
                "say where the click id is captured. You never state a lag, a backfill depth "
                "or a retention window: those are computed from the path you choose."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.evidence_block(
                    basis.found, tracking.PAGE, title="crawled landing pages and their forms"
                ),
                prompts.evidence_block(
                    basis.found,
                    tracking.CONVERSION_ACTION,
                    title="conversion actions in the account",
                ),
                prompts.computed_block(
                    "upload options (final — select one, never restate a figure)", basis.options
                ),
                prompts.computed_block(
                    "is a click id captured today?",
                    {
                        "present_today": basis.gclid.present_today,
                        "basis": basis.gclid.basis,
                        "observed_fields": list(basis.gclid.observed_fields),
                        "caveat": basis.gclid.caveat,
                    },
                ),
                prompts.computed_block(
                    "consent, from gate 1.5.3 (final — you may not widen this)",
                    {
                        "markets_allowed": list(consent.markets_allowed),
                        "markets_blocked": list(consent.markets_blocked),
                        "basis": list(consent.basis),
                        "markets_unstated": list(consent.markets_unstated),
                    },
                ),
                prompts.computed_block(
                    "the conversion taxonomy agreed in 2.1.1", ctx.output_of("2.1.1")
                ),
                prompts.computed_block(
                    "the qualified-lead definition agreed in 2.1.4", ctx.output_of("2.1.4")
                ),
                prompts.coverage_block(basis.found),
                "TASK\n"
                "  Select one method and cadence from the computed options table and say why "
                "in two sentences. Describe where the click id is captured, from which form "
                "field, and into which CRM object and field. Map each CRM stage that should "
                "reach the ad account to a conversion action and the field its value comes "
                "from. List any further prerequisite this needs, each with an owner and "
                "whether it blocks. Plan nothing for a market listed as blocked.",
                prompts.cite_from("EVIDENCE — crawled landing pages and their forms"),
            ),
            task_class=self.spec.task_class,
        )

        gaps = list(basis.gaps)
        chosen = basis.option(draft.method, draft.cadence)
        method: UploadMethod = draft.method
        cadence: Cadence = draft.cadence
        if chosen is None:
            # PRD §21 Q4: manual CSV monthly is the honest floor, and it is
            # what an unselectable choice falls back to rather than a guess.
            gaps.append(
                f"{draft.method}/{draft.cadence} is not one of the computed upload options; "
                f"the plan falls back to {DEFAULT_METHOD}/{DEFAULT_CADENCE}."
            )
            method, cadence = DEFAULT_METHOD, DEFAULT_CADENCE
            chosen = basis.option(DEFAULT_METHOD, DEFAULT_CADENCE)

        upload = None
        retention: float | None = None
        if chosen is not None:
            fits = bool(chosen.get("fits"))
            retention = _figure(chosen, "retention_days")
            upload = UploadPlan(
                method=method,
                cadence=cadence,
                lag_days=_figure(chosen, "lag_days") or 0.0,
                backfill_days=_figure(chosen, "backfill_days") or 0.0,
                headroom_days=_figure(chosen, "headroom_days") or 0.0,
                fits_click_window=fits,
                click_upload_window_days=_window(basis, "click_upload_window_days"),
                history_limits_backfill=bool(
                    basis.window is not None
                    and basis.window.value.get("history_limits_backfill", False)
                ),
                rationale=draft.method_rationale,
            )
        else:  # pragma: no cover - the table always carries the fallback pair
            gaps.append(
                "no upload option could be resolved, so the plan carries no cadence at all."
            )

        prerequisites: list[Prerequisite] = []
        if not basis.gclid.present_today:
            # PRD §21 Q3: until the CRM captures a click id, nothing else in
            # this node can happen, so it is prerequisite number one.
            prerequisites.append(
                Prerequisite(
                    task=(
                        "Capture the Google click id on every form and store it against the "
                        f"CRM record ({draft.gclid_capture.storage_object}). "
                        + basis.gclid.basis
                        + "."
                    ),
                    owner="unassigned",
                    blocking=True,
                )
            )
        if not consent.markets_allowed:
            prerequisites.append(
                Prerequisite(
                    task=(
                        "Record a lawful basis for uploading conversion data at gate 1.5.3. "
                        "No market currently carries one, so no offline conversion may be "
                        "uploaded for any market in scope."
                    ),
                    owner="data officer",
                    blocking=True,
                )
            )
        prerequisites.extend(
            Prerequisite(task=blocker, owner="data officer", blocking=True)
            for blocker in consent.blockers
        )
        if upload is not None and not upload.fits_click_window:
            prerequisites.append(
                Prerequisite(
                    task=(
                        f"Shorten the upload cadence: {method}/{cadence} leaves a conversion "
                        "waiting longer than Google will accept it after the click."
                    ),
                    owner="unassigned",
                    blocking=True,
                )
            )
        prerequisites.extend(draft.prerequisites)

        if consent.markets_unstated:
            gaps.append(
                "gate 1.5.3 never mentioned "
                + ", ".join(consent.markets_unstated)
                + ", so no offline conversion is planned for them."
            )

        return OfflineConversionPlanOutput(
            gclid_capture=GclidCapture(
                point=draft.gclid_capture.point,
                form_field=draft.gclid_capture.form_field,
                storage_object=draft.gclid_capture.storage_object,
                present_today=basis.gclid.present_today,
                basis=basis.gclid.basis,
                caveat=basis.gclid.caveat,
                # §13: the plan must name a retention window. The shortest
                # honest one is how long a click id has to survive to still be
                # uploadable, which the chosen option already computed.
                retention_days=retention,
                observed_fields=list(basis.gclid.observed_fields),
            ),
            upload=upload,
            stage_map=draft.stage_map,
            consent=ConsentScopeOut(
                markets_allowed=list(consent.markets_allowed),
                markets_blocked=list(consent.markets_blocked),
                basis=list(consent.basis),
                markets_unstated=list(consent.markets_unstated),
            ),
            prerequisites=prerequisites,
            notes=draft.notes,
            status="ok",
            open_gaps=gaps,
            coverage=gather.coverage_notes(basis.found),
            calc_evidence_ids=basis.calc_ids,
        )


# ---------------------------------------------------------------------------
# 2.5.3 — experiment_backlog
# ---------------------------------------------------------------------------


class ExperimentRating(BaseModel):
    """What the model supplies per candidate: a hypothesis and three ratings.

    The ratings are ordinal judgements on a 1–5 scale, which is what ICE is for
    and the one thing here no formula can produce. The *score* built from them
    is arithmetic and belongs to `experiments.ice_rank_v1`, which is why this
    model carries no `ice_score` field for a model to fill in.
    """

    id: str = Field(description="Exactly the candidate id from the computed table.")
    hypothesis: str = Field(
        min_length=1,
        description=(
            "One sentence: if we change X then Y improves, because Z. Argue from the basis "
            "given for this candidate, not from general practice."
        ),
    )
    impact_1_5: int = Field(ge=1, le=5, description="How much this moves the primary metric.")
    confidence_1_5: int = Field(ge=1, le=5, description="How sure the evidence makes you.")
    effort_1_5: int = Field(ge=1, le=5, description="1 is trivial, 5 is weeks of work.")


class ExperimentDraft(BaseModel):
    """2.5.3's model output — ratings, and nothing that has to add up."""

    ratings: list[ExperimentRating] = Field(default_factory=list)
    notes: str = ""


class Experiment(BaseModel):
    """One planned test, sized by arithmetic and funded by the reserve (§11)."""

    id: str
    hypothesis: str
    campaign_ref: str
    campaign_name: str
    market: str = ""
    variable: Variable
    primary_metric: TestMetric
    basis: str = Field(description="The upstream finding that raised this test.")
    baseline: float = Field(description="Baseline conversion rate, percent.")
    mde_pct: float
    required_conv_per_arm: int
    required_visitors_per_arm: int
    est_days_to_significance: int | None = None
    impact_1_5: int
    confidence_1_5: int
    effort_1_5: int
    ice_score: float
    rank: int
    earliest_wave: int | None = None
    reserve_usd: float
    funded: bool
    landing_url: str | None = None


class NotYet(BaseModel):
    """A test the plan raised and cannot run. §11's `not_yet[]`."""

    test: str
    blocked_by: str


class ExperimentBacklogOutput(BaseModel):
    """2.5.3 — what to test, in what order, and what the reserve actually pays for."""

    tests: list[Experiment] = Field(default_factory=list)
    not_yet: list[NotYet] = Field(default_factory=list)
    reserve_pool_usd: float = 0.0
    reserve_committed_usd: float = 0.0
    reserve_unspent_usd: float = 0.0
    alpha: float = 0.0
    power: float = 0.0
    notes: str = ""
    status: Status = "ok"
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(
        min_length=1,
        description="The `derived` rows the sizes, scores and reserve resolve to (law 14).",
    )


@dataclass(slots=True)
class ExperimentBasis:
    """What 2.5.3 assembled before it asked the model anything."""

    backlog: experiments.Backlog
    reserve_pool_usd: float
    sizing: Calculation | None = None
    gaps: list[str] = field(default_factory=list)

    @property
    def sized(self) -> dict[str, dict[str, Any]]:
        """The power calculation, per candidate id."""
        if self.sizing is None:
            return {}
        return {
            str(row.get("id")): row
            for row in self.sizing.value.get("tests", [])
            if isinstance(row, dict)
        }

    def prompt_rows(self) -> list[dict[str, Any]]:
        """The candidates as the model reads them: facts and sizes, no scores."""
        sized = self.sized
        rows: list[dict[str, Any]] = []
        for candidate in self.backlog.candidates:
            measured = sized.get(candidate.id, {})
            rows.append(
                {
                    "id": candidate.id,
                    "campaign": candidate.campaign_name,
                    "market": candidate.market,
                    "variable": candidate.variable,
                    "primary_metric": candidate.primary_metric,
                    "why_this_is_in_question": candidate.basis,
                    "baseline_cvr_pct": candidate.baseline_cvr_pct,
                    "monthly_budget_usd": candidate.monthly_budget_usd,
                    "launch_wave": candidate.launch_wave,
                    "landing_url": candidate.landing_url,
                    "required_conv_per_arm": measured.get("required_conv_per_arm"),
                    "est_days_to_significance": measured.get("est_days_to_significance"),
                }
            )
        return rows


class InsufficientBacklog(CalcError):
    """This plan raised no tension a test could settle, and none could be sized.

    A `CalcError` subclass for the reason `stage_2_2.InsufficientDemand` is:
    §9.1's `calc_evidence_ids: min_length=1` makes an `insufficient_input`
    output unconstructable for a node whose every figure is computed, so the
    honest failure is to stop with the missing input named.
    """


class ExperimentBacklogNode(LLMNode):
    """2.5.3 — the ranked test backlog. Sizes from arithmetic, order from judgement."""

    spec = NodeSpec(
        id="2.5.3",
        name="experiment_backlog",
        stage="2.5",
        run_stage=RunStage.PLAN,
        # §11's edge list is `2.5.3←{2.4.2, 2.2.4, 2.3.1}`. Two are added and
        # neither moves a wave: 2.2.2 is already upstream of 2.2.4 and carries
        # the learning verdict that raises a bid-strategy test, and 2.3.2 is a
        # sibling of 2.4.1 and carries `broad_match.allowed_campaigns`, which
        # is the only thing that can raise a match-type test. An undeclared
        # dependency is one `ctx.output_of` refuses.
        #
        # 2.5.2 is deliberately *not* among them. The audience rule needs the
        # consent scope, gate 1.5.3 is authoritative over it (§13), and so
        # this node computes it from `PlanInput` exactly as 2.5.2 does rather
        # than reading 2.5.2's copy and inheriting a wait on G2 with it.
        depends_on=("2.4.2", "2.2.4", "2.3.1", "2.2.2", "2.3.2"),
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=ExperimentBacklogOutput,
        connectors=(),
        calc=(SAMPLE_SIZE, ICE_RANK),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        plan = ctx.require_plan()
        consent = tracking.consent_scope(
            plan.input.readiness, [market.country for market in plan.input.markets]
        )
        backlog = experiments.build(
            structure=ctx.output_of("2.4.2"),
            allocation=ctx.output_of("2.2.4"),
            capacity=ctx.output_of("2.2.2"),
            slate=ctx.output_of("2.3.1"),
            boundaries=ctx.output_of("2.3.2"),
            consent_markets=list(consent.markets_allowed),
        )
        basis = ExperimentBasis(
            backlog=backlog, reserve_pool_usd=_reserve_pool(ctx.output_of("2.2.4"))
        )
        if not backlog.candidates:
            ctx.scratch[self.spec.id] = basis
            return []

        basis.sizing = await plan.calc.run(
            SAMPLE_SIZE,
            backlog.sizing_frame(default_mde_pct=plan.constants.get("test.min_mde_pct").value),
        )
        ctx.scratch[self.spec.id] = basis
        return [basis.sizing.evidence]

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        basis: ExperimentBasis = ctx.scratch[self.spec.id]
        plan = ctx.require_plan()
        if basis.sizing is None:
            _no_backlog(basis)

        draft = await ctx.complete(
            ExperimentDraft,
            system=prompts.system_prompt(
                "You are writing the test backlog for a paid search plan that has already "
                "been costed and approved. Every candidate below is a tension the plan "
                "itself raised; your job is the hypothesis that would settle it and a 1–5 "
                "rating of its impact, your confidence and the effort. You never write a "
                "sample size, a duration, a score or a budget — those are computed from "
                "your ratings and from the traffic forecast."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "the test candidates (final — rate every id, invent none)",
                    basis.prompt_rows(),
                ),
                prompts.computed_block(
                    "what the competition is doing, for calibrating impact",
                    _competitive_context(plan.input),
                ),
                prompts.computed_block(
                    "the channel slate agreed at G4, for the wave a test can start in",
                    (ctx.output_of("2.3.1") or {}).get("slate", []),
                ),
                "TASK\n"
                "  Return one rating object per candidate id above and no others. For each: "
                "a one-sentence hypothesis in the form 'if we change X then Y improves, "
                "because Z', argued from that candidate's stated basis; then impact, "
                "confidence and effort, each a whole number from 1 to 5. Be honest about "
                "effort — a landing-page rebuild is not a 2. Use `notes` for anything the "
                "backlog as a whole assumes.",
            ),
            task_class=self.spec.task_class,
        )

        candidates = basis.backlog.by_id()
        rated = {rating.id: rating for rating in draft.ratings if rating.id in candidates}
        gaps = list(basis.gaps)
        _report_rating_gaps(gaps, draft.ratings, rated, candidates)

        sized = basis.sized
        ranking = await plan.calc.run(
            ICE_RANK,
            _rating_frame(basis, rated, sized),
            reserve_pool_usd=basis.reserve_pool_usd,
        )
        ranked = [row for row in ranking.value.get("tests", []) if isinstance(row, dict)]

        tests: list[Experiment] = []
        not_yet = [
            NotYet(test=row["test"], blocked_by=row["blocked_by"])
            for row in basis.backlog.unsizable
        ]
        for scored in sorted(ranked, key=_rank_of):
            key = str(scored.get("id"))
            candidate = candidates.get(key)
            measured = sized.get(key)
            if candidate is None or measured is None:  # pragma: no cover — keys come from both
                continue
            rating = rated.get(key)
            blocked = scored.get("blocked_by")
            if blocked:
                not_yet.append(NotYet(test=_label(candidate), blocked_by=str(blocked)))
            tests.append(
                Experiment(
                    id=key,
                    hypothesis=rating.hypothesis if rating else _default_hypothesis(candidate),
                    campaign_ref=candidate.campaign_ref,
                    campaign_name=candidate.campaign_name,
                    market=candidate.market,
                    variable=candidate.variable,
                    primary_metric=candidate.primary_metric,
                    basis=candidate.basis,
                    baseline=_figure(measured, "baseline_cvr_pct") or 0.0,
                    mde_pct=_figure(measured, "mde_pct") or 0.0,
                    required_conv_per_arm=_count(measured, "required_conv_per_arm"),
                    required_visitors_per_arm=_count(measured, "required_visitors_per_arm"),
                    est_days_to_significance=_optional_count(measured, "est_days_to_significance"),
                    impact_1_5=_count(scored, "impact_1_5"),
                    confidence_1_5=_count(scored, "confidence_1_5"),
                    effort_1_5=_count(scored, "effort_1_5"),
                    ice_score=_figure(scored, "ice_score") or 0.0,
                    rank=_count(scored, "rank"),
                    earliest_wave=candidate.launch_wave,
                    reserve_usd=_figure(scored, "reserve_usd") or 0.0,
                    funded=bool(scored.get("funded")),
                    landing_url=candidate.landing_url,
                )
            )

        if basis.reserve_pool_usd <= 0:
            gaps.append(
                "2.2.4 set aside no experiment reserve, so every test is ranked and none is "
                "funded. Raise `experiment_reserve_pct` on the budget gate to run any of them."
            )

        return ExperimentBacklogOutput(
            tests=tests,
            not_yet=not_yet,
            reserve_pool_usd=_figure(ranking.value, "reserve_pool_usd") or 0.0,
            reserve_committed_usd=_figure(ranking.value, "reserve_committed_usd") or 0.0,
            reserve_unspent_usd=_figure(ranking.value, "reserve_unspent_usd") or 0.0,
            alpha=_figure(basis.sizing.value, "alpha") or 0.0,
            power=_figure(basis.sizing.value, "power") or 0.0,
            notes=draft.notes,
            status="ok",
            open_gaps=gaps,
            coverage=[],
            calc_evidence_ids=[basis.sizing.id, ranking.id],
        )


def _no_backlog(basis: ExperimentBasis) -> NoReturn:
    """Stop the node naming what it could not size, rather than emit an uncited list."""
    unsizable = "; ".join(row["blocked_by"] for row in basis.backlog.unsizable)
    raise InsufficientBacklog(
        "node 2.5.3 could not size a single test: "
        + (unsizable or "no campaign in this plan carries a tension a test could settle")
        + ". A backlog states conversions per arm and days to significance, and both come "
        "from `power.sample_size_v1` over a campaign's own forecast — so a plan with no "
        "click or conversion forecast has no backlog rather than an unsized one."
    )


def _rating_frame(
    basis: ExperimentBasis,
    rated: dict[str, ExperimentRating],
    sized: dict[str, dict[str, Any]],
) -> pd.DataFrame:
    """The frame `experiments.ice_rank_v1` eats: ratings joined to sample sizes.

    An unrated candidate is carried at the neutral 3/3/3 rather than dropped. A
    model that returned fourteen of fifteen ratings has not said the fifteenth
    tension does not exist, and silently losing it would leave the backlog
    quietly disagreeing with the plan that raised it.
    """
    rows: list[dict[str, Any]] = []
    for candidate in basis.backlog.candidates:
        measured = sized.get(candidate.id, {})
        rating = rated.get(candidate.id)
        rows.append(
            {
                "id": candidate.id,
                "impact_1_5": rating.impact_1_5 if rating else NEUTRAL_RATING,
                "confidence_1_5": rating.confidence_1_5 if rating else NEUTRAL_RATING,
                "effort_1_5": rating.effort_1_5 if rating else NEUTRAL_RATING,
                "required_visitors_total": measured.get("required_visitors_total"),
                "avg_cpc_usd": candidate.avg_cpc_usd,
                "est_days_to_significance": measured.get("est_days_to_significance"),
                "launch_wave": candidate.launch_wave,
            }
        )
    return pd.DataFrame(rows)


def _report_rating_gaps(
    gaps: list[str],
    returned: list[ExperimentRating],
    kept: dict[str, ExperimentRating],
    candidates: dict[str, experiments.Candidate],
) -> None:
    """Name what the model missed and what it made up. Neither is silent."""
    missing = sorted(key for key in candidates if key not in kept)
    if missing:
        gaps.append(
            f"{len(missing)} candidate(s) came back unrated and are ranked at the neutral "
            f"3/3/3 rather than dropped: {', '.join(missing[:10])}"
        )
    # `.difference()` rather than `-`: the isolation guard reads a `-` with an
    # attribute on either side as arithmetic, and it is right not to try to tell
    # a set difference from a subtraction. The method says the same thing.
    invented = sorted({rating.id for rating in returned}.difference(candidates))
    if invented:
        gaps.append(
            f"{len(invented)} rating(s) named a candidate this plan never raised and are "
            f"ignored: {', '.join(invented[:10])}"
        )


def _reserve_pool(allocation: dict[str, Any]) -> float:
    """What 2.2.4 set aside for experiments. A lookup, never a share of anything."""
    return _figure(allocation, "experiment_reserve_usd") or 0.0


def _competitive_context(plan_input: PlanInput) -> dict[str, Any]:
    """Stage 01's competitive picture, cut to what calibrates an impact rating."""
    landscape = plan_input.competitive_landscape
    return {
        "competitors": [
            {"name": row.name or row.domain, "threat": row.threat}
            for row in landscape.competitors[:8]
        ],
        "message_clusters": [row.theme for row in landscape.message_clusters[:8]],
        "whitespace": [row.claim for row in landscape.whitespace[:6]],
    }


def _label(candidate: experiments.Candidate) -> str:
    return f"{candidate.campaign_name}: {candidate.variable.replace('_', ' ')}"


def _default_hypothesis(candidate: experiments.Candidate) -> str:
    """What an unrated candidate carries in place of a model's sentence."""
    return f"Untested — {_label(candidate)}. {candidate.basis}"


def _rank_of(row: dict[str, Any]) -> int:
    value = row.get("rank")
    return value if isinstance(value, int) else 1_000_000


# ---------------------------------------------------------------------------
# merge helpers — lookups only, never arithmetic
# ---------------------------------------------------------------------------


def _figure(computed: dict[str, Any], key: str | None) -> float | None:
    """One computed figure by name. None when it was not computed.

    Every number this module outputs goes through here: a dictionary read and a
    float cast, with no operator touching a value, which is what lets
    `check_calc_isolation.py` prove the package does no arithmetic.
    """
    if not key:
        return None
    value = computed.get(key)
    if value is None or isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _count(computed: dict[str, Any], key: str) -> int:
    """A whole number off a computed row. Absent reads as zero, never as a guess."""
    value = _figure(computed, key)
    return 0 if value is None else int(value)


def _optional_count(computed: dict[str, Any], key: str) -> int | None:
    """A whole number that is allowed to be absent — `est_days_to_significance` is.

    Distinct from `_count` on purpose: a test with no click forecast has no
    date it would read out on, and rendering that as `0 days` would advertise
    an answer tomorrow.
    """
    value = _figure(computed, key)
    return None if value is None else int(value)


def _window(basis: OfflineBasis, key: str) -> float:
    """A scalar off `measurement.upload_window_v1`'s result."""
    if basis.window is None:  # pragma: no cover - reason() runs after gather()
        return 0.0
    return _figure(basis.window.value, key) or 0.0


measurement_source_of_truth = MeasurementSourceOfTruthNode()
offline_conversion_plan = OfflineConversionPlanNode()
experiment_backlog = ExperimentBacklogNode()
