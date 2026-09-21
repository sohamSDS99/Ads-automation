"""Stage 2.5 — decide how we measure (Stage 02 PRD §11).

Two of the three nodes. `2.5.3 experiment_backlog` waits for S2-P5b: it reads
2.4.2, 2.2.4 and 2.3.1, none of which exist yet, while 2.5.1 and 2.5.2 hang off
2.1.1 and 2.1.4 and can be built the moment Stage 2.1 is green. That is the
whole reason this phase is *P5a* and not P5 — §11's edge list makes the
measurement branch independent of the budget and structure branches, and
building the independent half first is what keeps a phase shippable.

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
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from agent.db.models import Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes import gather, prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.orchestrator.plan_calc import Calculation
from agent.planning import crm, tracking

log = structlog.get_logger(__name__)

RECONCILIATION = "measurement.reconciliation_v1"
UPLOAD_WINDOW = "measurement.upload_window_v1"

Status = Literal["ok", "insufficient_input"]
PrimarySource = Literal["google_ads", "ga4", "crm", "warehouse"]
UploadMethod = Literal["ads_api", "sheets_link", "manual_csv"]
Cadence = Literal["daily", "weekly", "monthly"]
Refresh = Literal["realtime", "daily", "weekly", "monthly"]

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


def _window(basis: OfflineBasis, key: str) -> float:
    """A scalar off `measurement.upload_window_v1`'s result."""
    if basis.window is None:  # pragma: no cover - reason() runs after gather()
        return 0.0
    return _figure(basis.window.value, key) or 0.0


measurement_source_of_truth = MeasurementSourceOfTruthNode()
offline_conversion_plan = OfflineConversionPlanNode()
