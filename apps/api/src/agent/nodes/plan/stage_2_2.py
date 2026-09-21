"""Stage 2.2 — decide the budget (Stage 02 PRD §11).

Five nodes. `2.2.4 budget_allocation` carries gate **G3**, the only gate in the
product that hands a number *back* to the engine: a budget owner moves money on
the card, `POST /approvals/{id}/recalc` re-forecasts the edit, and the approved
split is what 2.3 onwards is built from.

**The shape is Stage 2.1's, for the same reason.** Every figure comes from a
registered `@formula`, is persisted as a `PlanCalc` row plus a `derived`
Evidence row, and is cited by `calc_evidence_ids`. `gather()` reads the account
and calls `ctx.plan.calc.run(...)`; `reason()` asks the model for labels,
rationale and *selections* and merges the numbers in afterwards.

**What the model decides here, precisely.** Three things, none of them a figure:

1. **Which campaign a cluster belongs to** (2.2.2). The research produced
   clusters; 2.1.3 produced campaigns; joining them is judgement about meaning,
   which is the model's job. Every number that follows is computed over that
   assignment by `planning/demand.py` and `calc/`.
2. **Which scenario to put to the budget owner** (2.2.4), out of the three
   `scenarios.envelope_v1` produced, with a reason.
3. **What a reallocation rule should watch, and what it should do** (2.2.5).
   The thresholds it watches are selected from computed figures; the shift
   sizes and cadences come from `planning_constants.yaml`.

**There are no strategic weights.** `allocation.split_v1` accepts one, and this
stage never supplies it: the proposed split is efficiency, floors and measured
caps, and nothing else. A weight the model chose would be an unexplainable
number in front of the one person in the product who is entitled to ask "why
does Germany get 18%" — and the answer to that question is the gate, where a
human moves the money and the move is recorded as theirs. `scenarios.py` makes
the same argument about never recommending `aggressive` automatically.

**The envelope is what the allocation commits, not the scenario headline.**
PRD §12 invariant 4 requires the allocation to sum to the envelope within
±0.5%. A scenario total can exceed what the demand can absorb — measured caps
are the whole point of the aggressive one — so 2.2.4 sets the envelope to the
allocated total and reports the difference as `unallocated_usd` with its
reason. An envelope that disagrees with its own lines is a plan that gets
queried instead of approved.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, NoReturn

import pandas as pd
import structlog
from pydantic import BaseModel, Field

from agent.calc.registry import CalcError
from agent.db.models import ApprovalRequiredRole, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes import gather, prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.orchestrator.plan_calc import Calculation
from agent.planning import demand

log = structlog.get_logger(__name__)

TRAFFIC = "forecast.traffic_v1"
ENVELOPE = "scenarios.envelope_v1"
SPLIT = "allocation.split_v1"
VOLUME_CHECK = "structure.volume_check_v1"

#: The `degraded_sources` entry PRD §18 names for a forecast service that would
#: not answer. Carried into the plan and displayed on the budget gate card.
FORECAST_SOURCE = "google_ads_forecast"

#: How many forecast rows a prompt is shown. The model is choosing which
#: campaign a *cluster* belongs to, and there are tens of clusters, not
#: thousands — but a twelve-month forecast is twelve rows per cluster-market, so
#: the monthly detail is deliberately withheld and the totals shown instead.
CLUSTER_ROWS = 60

#: Scenario names, in the order `scenarios.envelope_v1` emits them.
ScenarioName = Literal["cautious", "expected", "aggressive"]

Verdict = Literal["clears", "marginal", "below"]
BidStrategy = Literal["manual_cpc", "max_clicks", "max_conv", "tcpa", "troas"]
Remedy = Literal["merge", "broaden", "switch_strategy", "raise_budget", "defer_to_wave_2"]
Method = Literal["google_forecast", "derived_arithmetic"]
Status = Literal["ok", "insufficient_input", "infeasible"]

#: Which computed figure a reallocation rule's threshold resolves to. The model
#: names a basis; the node reads the value out of a calculation. A mapping
#: rather than a branch, so adding a basis is a line here and not a new
#: arithmetic path — the same device `stage_2_1.VALUE_BASIS` uses.
THRESHOLD_BASIS: dict[str, str] = {
    "target_cpa": "target_cpa_usd",
    "forecast_cpa": "forecast_cpa_usd",
    "monthly_budget": "monthly_budget_usd",
    "learning_threshold": "threshold",
    "forecast_conv_30d": "forecast_conv_30d",
}


class InsufficientDemand(CalcError):
    """The research cannot support a forecast this node must cite.

    A `CalcError` subclass for the same reason `stage_2_1.InsufficientPlanInput`
    is: it reads as "the arithmetic could not be done" rather than "the node is
    broken", and the executor's retry ladder produces the same named message
    three times, cheaply, with the last one being what a person reads.
    """


def _plan_block(ctx: RunContext) -> str:
    """What this plan is being built from. Configuration, not evidence."""
    from agent.nodes.plan.stage_2_1 import _plan_block as shared

    return shared(ctx)


# ---------------------------------------------------------------------------
# shared basis
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class Demand:
    """The forecast, computed once per node and remembered for `reason()`.

    The Stage 2.2 counterpart of `stage_2_1.Economics`, and it shares that
    class's most important property: `DerivedWriter` dedupes on
    `(plan_run_id, formula_id, inputs_hash)`, so every node in this stage cites
    **one** `forecast.traffic_v1` row rather than five copies of it. A reader
    following a number from the budget gate back to its calculation lands on
    the same row 2.2.1 published.
    """

    found: gather.Gathered
    basis: demand.DemandBasis
    traffic: Calculation | None = None
    gaps: list[str] = field(default_factory=list)
    degraded: list[str] = field(default_factory=list)

    @property
    def result(self) -> dict[str, Any]:
        return self.traffic.value if self.traffic is not None else {}

    @property
    def rows(self) -> list[dict[str, Any]]:
        """The per cluster-market-month forecast rows."""
        rows = self.result.get("forecast")
        return list(rows) if isinstance(rows, list) else []

    @property
    def month_count(self) -> int:
        count = self.result.get("month_count")
        return int(count) if isinstance(count, int) and count > 0 else 1

    @property
    def headroom_pct(self) -> float | None:
        value = self.result.get("impression_share_headroom_pct")
        return float(value) if isinstance(value, int | float) else None

    @property
    def funnel_stages(self) -> dict[tuple[str, str], str]:
        return {(group.cluster, group.market): group.funnel_stage for group in self.basis.groups}

    @property
    def calc_ids(self) -> list[uuid.UUID]:
        return [self.traffic.id] if self.traffic is not None else []

    @property
    def evidence(self) -> list[Evidence]:
        rows = list(self.found.evidence)
        if self.traffic is not None:
            rows.append(self.traffic.evidence)
        return rows

    @property
    def status(self) -> Status:
        return "ok" if self.traffic is not None else "insufficient_input"

    def totals_for_prompt(self) -> list[dict[str, Any]]:
        """Per cluster-market totals. The monthly rows are withheld on purpose.

        A twelve-month forecast over twenty cluster-markets is 240 rows, and
        the model's job here is to place a *cluster* in a campaign. Showing it
        the months would cost tokens to make the same decision less clearly.
        The roll-up itself lives in `planning/demand.py`: summing cost rows is
        arithmetic, and a plan node that can sum can also invent.
        """
        return demand.cluster_totals(
            self.rows,
            funnel_stages=self.funnel_stages,
            month_count=self.month_count,
        )[:CLUSTER_ROWS]


def _benchmark_needs() -> list[gather.Need]:
    """The account history a forecast is rated from. Read-only, and optional.

    PRD §18: "Google Ads account not connected at all — 2.2.1 runs on Stage 01
    data only". So both of these are `optional`, and their absence produces a
    named gap from `planning/demand.py` rather than a missing-evidence note the
    report would read as a degradation.
    """
    return [
        gather.Need(
            demand.CAMPAIGN_PERF,
            connector="google_ads",
            params={"kinds": [demand.CAMPAIGN_PERF]},
            limit=5_000,
            optional=True,
        ),
        gather.Need(
            demand.KEYWORD_IMPRESSION_SHARE,
            connector="google_ads",
            params={"kinds": [demand.KEYWORD_IMPRESSION_SHARE]},
            limit=5_000,
            optional=True,
        ),
    ]


def _forecast_need(basis: demand.DemandBasis, languages: dict[str, str]) -> gather.Need:
    """Ask Google to price the clusters the research produced.

    The groups have to be computed before the pull, which is why this stage
    builds the demand frame twice: once to discover the cluster-markets and
    their bids, and once more with Google's answer folded in. Both calls are to
    a pure function over the same inputs, so the cost of the second is
    arithmetic rather than I/O.
    """
    return gather.Need(
        demand.KEYWORD_FORECAST,
        connector="google_ads",
        params={
            "kinds": [demand.KEYWORD_FORECAST],
            "groups": [
                group.as_params(language=languages.get(group.market))
                for group in basis.groups
                if group.keywords and group.max_cpc_usd > 0
            ],
        },
        limit=1_000,
        optional=True,
    )


async def _demand(ctx: RunContext) -> Demand:
    """Read the account, price the demand, and record what could not be priced.

    The single place any Stage 2.2 node reaches `calc/forecast.py`. A
    `CalcError` is caught rather than raised: a forecast that cannot be built
    makes the node say `insufficient_input` naming the missing field, exactly
    as 2.1.2 does, and the budget gate then asks a human for the number.
    """
    plan = ctx.require_plan()
    found = await gather.collect(ctx, *_benchmark_needs())

    def build(forecast_rows: Sequence[dict[str, Any]] = ()) -> demand.DemandBasis:
        return demand.demand_frame(
            priced_keywords=plan.input.priced_keyword_list,
            demand_map=plan.input.demand_map,
            constants=plan.constants,
            campaign_perf=found.payloads(demand.CAMPAIGN_PERF),
            impression_share=found.payloads(demand.KEYWORD_IMPRESSION_SHARE),
            keyword_forecast=forecast_rows,
        )

    basis = build()
    degraded: list[str] = []
    if basis.usable:
        languages = {
            market.country: market.language for market in plan.input.markets if market.language
        }
        priced = await gather.collect(ctx, _forecast_need(basis, languages))
        rows = priced.payloads(demand.KEYWORD_FORECAST)
        if priced.degraded:
            # PRD §18: the forecast service refusing is a degradation, never a
            # stop. The substitution is named so the gate card can show it.
            degraded.append(FORECAST_SOURCE)
            log.warning(
                "plan.forecast_degraded",
                node_id=ctx.node_id,
                reason="; ".join(sorted(priced.degraded.values())),
            )
        if rows:
            found.evidence.extend(
                row for row in priced.evidence if row.kind == demand.KEYWORD_FORECAST
            )
            basis = build(rows)

    result = Demand(found=found, basis=basis, gaps=list(basis.gaps), degraded=degraded)
    if not basis.usable:
        log.info("plan.demand_unavailable", node_id=ctx.node_id, gaps=result.gaps)
        return result

    try:
        result.traffic = await plan.calc.run(TRAFFIC, basis.frame, method=basis.method)
    except CalcError as exc:
        result.gaps.append(f"{TRAFFIC} — {exc}")
        log.warning("plan.forecast_failed", node_id=ctx.node_id, error=str(exc))
    return result


def _insufficient(node_id: str, basis: Demand) -> NoReturn:
    """Stop the node naming the field, rather than emit an uncitable output.

    The same trade `stage_2_1._insufficient` argues: §9.1's
    `calc_evidence_ids: min_length=1` makes an `insufficient_input` output
    literally unconstructable for a node whose every figure is computed, so the
    honest failure is to stop with the missing input named. A forecast nobody
    can source is not a thin forecast — it is no forecast, and a budget gate
    built on one would be asking a person to approve arithmetic that never
    happened.
    """
    detail = "; ".join(basis.gaps) or "no forecast could be computed from the accepted research"
    raise InsufficientDemand(
        f"node {node_id} cannot forecast demand: {detail}. "
        "Every figure in a media plan is computed from priced keywords (stage 1.4) and the "
        "account's own rates; without them there is nothing to approve."
    )


def _assignment_map(
    assignments: Sequence[ClusterAssignment], known: set[str]
) -> dict[tuple[str, str], str]:
    """`(cluster, market) -> campaign_ref`, dropping references to no campaign.

    A model that invents a campaign 2.1.3 never proposed would put budget
    against something with no target and no gate decision behind it. Dropping
    the assignment sends that cluster to `demand.UNASSIGNED`, which appears on
    the gate card as its own line — visible, fundable, and obviously wrong to
    the person deciding, which is the outcome to want.
    """
    return {
        (item.cluster, item.market): item.campaign_ref
        for item in assignments
        if item.campaign_ref in known
    }


def _campaign_refs(ctx: RunContext) -> list[str]:
    """The campaigns 2.1.3 proposed, in its own order."""
    objectives = ctx.output_of("2.1.3").get("objectives") or []
    return [str(item.get("campaign_ref")) for item in objectives if item.get("campaign_ref")]


def _targets(ctx: RunContext) -> dict[str, float]:
    """Each campaign's target, as 2.1.3 computed it.

    Read off the gate's own output rather than recomputed: G1 may have been
    approved *with edits*, and the edited target is the one the budget has to
    be built against. Recomputing here would quietly overrule the marketing
    lead's decision two nodes after they made it.
    """
    targets: dict[str, float] = {}
    for item in ctx.output_of("2.1.3").get("objectives") or []:
        ref = item.get("campaign_ref")
        value = item.get("target_value")
        if ref and isinstance(value, int | float) and value > 0:
            targets[str(ref)] = float(value)
    return targets


def _revenue_campaigns(ctx: RunContext, refs: Sequence[str]) -> set[str]:
    """Campaigns whose conversion actions carry a value, per 2.1.1.

    `structure.volume_check_v1` needs it to choose between the tROAS and tCPA
    thresholds, and the taxonomy is the only place that is knowable. An account
    with no valued action cannot run tROAS whatever its volume.
    """
    actions = ctx.output_of("2.1.1").get("actions") or []
    valued = any(
        action.get("primary") and str(action.get("value_model") or "none") != "none"
        for action in actions
    )
    return set(refs) if valued else set()


# ---------------------------------------------------------------------------
# 2.2.1 — demand_forecast
# ---------------------------------------------------------------------------


class ForecastNotes(BaseModel):
    """Prose only. Every figure in this node was computed and is final."""

    method_notes: str = Field(
        min_length=1,
        description="How this forecast was arrived at and what would move it most.",
    )
    caveats: list[str] = Field(
        default_factory=list,
        description="What a reader must not conclude from these numbers.",
    )


class ForecastRow(BaseModel):
    """One cluster, in one market, in one month."""

    cluster: str
    market: str
    month: str
    impressions: int
    ctr_pct: float
    clicks: float
    avg_cpc_usd: float
    cvr_pct: float
    conversions: float
    cost_usd: float
    cpa_usd: float | None = None


class ForecastTotals(BaseModel):
    impressions: int
    clicks: float
    conversions: float
    cost_usd: float
    ctr_pct: float
    avg_cpc_usd: float
    cvr_pct: float
    cpa_usd: float | None = None


class ConfidenceBand(BaseModel):
    """What the CPC range works out to, or that there was no range to use."""

    basis: Literal["cpc_range", "point_estimate"]
    low_pct: float
    high_pct: float


class DemandForecastOutput(BaseModel):
    """2.2.1 — impressions to cost, per cluster per market per month."""

    forecast: list[ForecastRow]
    monthly_totals: list[ForecastTotals] = Field(default_factory=list)
    totals: ForecastTotals
    method: Method
    confidence_band: ConfidenceBand
    impression_share_headroom_pct: float | None = None
    method_notes: str
    caveats: list[str] = Field(default_factory=list)
    excluded: list[dict[str, Any]] = Field(default_factory=list)
    degraded_sources: list[str] = Field(default_factory=list)
    status: Status
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class DemandForecastNode(LLMNode):
    """2.2.1 — what the demand costs, before anyone argues about the budget."""

    spec = NodeSpec(
        id="2.2.1",
        name="demand_forecast",
        stage="2.2",
        run_stage=RunStage.PLAN,
        depends_on=("2.1.1",),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=DemandForecastOutput,
        connectors=("google_ads",),
        calc=(TRAFFIC,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        basis = await _demand(ctx)
        ctx.scratch[self.spec.id] = basis
        return basis.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        basis: Demand = ctx.scratch[self.spec.id]
        if basis.traffic is None:
            _insufficient(self.spec.id, basis)

        result = basis.result
        notes = await ctx.complete(
            ForecastNotes,
            system=prompts.system_prompt(
                "You explain how a paid-search demand forecast was built and what a reader "
                "should be careful about. You write prose only — every figure here was "
                "computed in pandas and is final."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "the forecast (final — do not restate or adjust)",
                    {
                        "chain": (
                            "impressions = volume x seasonality x impressionShareTarget; "
                            "clicks = impressions x CTR; cost = clicks x CPC; "
                            "conversions = clicks x CVR"
                        ),
                        "method": result.get("method"),
                        "totals": result.get("totals"),
                        "confidence_band": result.get("confidence_band"),
                        "impression_share_headroom_pct": result.get(
                            "impression_share_headroom_pct"
                        ),
                        "by_cluster": basis.totals_for_prompt(),
                        "rates_basis": (
                            basis.basis.benchmarks.basis if basis.basis.benchmarks else None
                        ),
                        "assembly_notes": basis.basis.notes,
                        "degraded_sources": basis.degraded,
                        "excluded": list(basis.traffic.result.excluded),
                        "unavailable": basis.gaps,
                    },
                ),
                prompts.coverage_block(basis.found),
                "TASK\n"
                "  Write `method_notes`: how this forecast was arrived at, which of its "
                "inputs is measured and which is assumed, and what would move it most. "
                "Then list `caveats` — what a reader must not conclude from it. If a "
                "source was degraded or a rate was a planning default, say so plainly.",
            ),
            task_class=self.spec.task_class,
        )

        return DemandForecastOutput(
            forecast=[ForecastRow.model_validate(row) for row in basis.rows],
            monthly_totals=[
                ForecastTotals.model_validate(row) for row in result.get("monthly_totals") or []
            ],
            totals=ForecastTotals.model_validate(result["totals"]),
            method=result["method"],
            confidence_band=ConfidenceBand.model_validate(result["confidence_band"]),
            impression_share_headroom_pct=result.get("impression_share_headroom_pct"),
            method_notes=notes.method_notes,
            caveats=notes.caveats,
            excluded=[dict(row) for row in basis.traffic.result.excluded],
            degraded_sources=basis.degraded,
            status=basis.status,
            open_gaps=basis.gaps,
            coverage=gather.coverage_notes(basis.found),
            calc_evidence_ids=basis.calc_ids,
        )


# ---------------------------------------------------------------------------
# 2.2.2 — learning_capacity_check
# ---------------------------------------------------------------------------


class ClusterAssignment(BaseModel):
    """One cluster-market placed in one campaign. No figures."""

    cluster: str = Field(min_length=1)
    market: str = Field(min_length=1)
    campaign_ref: str = Field(
        min_length=1,
        description="One of the campaign_refs proposed by node 2.1.3. Never a new name.",
    )
    rationale: str = Field(min_length=1)


class CapacityDraft(BaseModel):
    """What the model is asked for at 2.2.2: an assignment and an explanation."""

    assignments: list[ClusterAssignment]
    notes: str = Field(
        min_length=1,
        description="What the assignment assumes, and which clusters were hard to place.",
    )


class CampaignCapacity(BaseModel):
    """One campaign's verdict against the learning thresholds."""

    campaign_ref: str
    monthly_budget_usd: float
    forecast_cpa_usd: float
    forecast_conv_30d: float
    forecast_clicks_30d: float | None = None
    threshold: float
    verdict: Verdict
    bid_strategy_recommended: BidStrategy
    remedy: Remedy | None = None
    clusters: list[str] = Field(default_factory=list)


class LearningCapacityOutput(BaseModel):
    """2.2.2 — can each campaign gather enough conversions to be optimised."""

    campaigns: list[CampaignCapacity]
    assignments: list[ClusterAssignment] = Field(default_factory=list)
    unassigned_clusters: list[str] = Field(default_factory=list)
    structure_verdict: Literal["sound", "needs_merge", "too_thin"]
    notes: str
    excluded: list[dict[str, Any]] = Field(default_factory=list)
    status: Status
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class LearningCapacityNode(LLMNode):
    """2.2.2 — the check that decides whether a campaign can be bid automatically."""

    spec = NodeSpec(
        id="2.2.2",
        name="learning_capacity_check",
        stage="2.2",
        run_stage=RunStage.PLAN,
        # §11's edge list is `2.2.2<-{2.2.1, 2.1.3}`. 2.1.1 is added because
        # this node reads the conversion taxonomy to know whether the account
        # can run tROAS at all, and a transitive dependency that is not
        # declared is one `ctx.output_of` is entitled to refuse. It changes no
        # wave ordering: 2.1.3 already depends on 2.1.1.
        depends_on=("2.2.1", "2.1.3", "2.1.1"),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=LearningCapacityOutput,
        connectors=("google_ads",),
        calc=(TRAFFIC, VOLUME_CHECK),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        basis = await _demand(ctx)
        ctx.scratch[self.spec.id] = basis
        return basis.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        basis: Demand = ctx.scratch[self.spec.id]
        if basis.traffic is None:
            _insufficient(self.spec.id, basis)

        plan = ctx.require_plan()
        refs = _campaign_refs(ctx)
        draft = await ctx.complete(
            CapacityDraft,
            system=prompts.system_prompt(
                "You place each cluster of search demand into one of the campaigns that "
                "have already been agreed. You never invent a campaign, never merge two, "
                "and never write a figure — the budget and the conversion volume that "
                "follow from your placement are computed."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "the campaigns agreed at gate G1 (place clusters into these, and only these)",
                    ctx.output_of("2.1.3").get("objectives") or [],
                ),
                prompts.computed_block(
                    "the forecast by cluster (final — do not restate or adjust)",
                    basis.totals_for_prompt(),
                ),
                prompts.computed_block(
                    "the conversion taxonomy agreed in 2.1.1", ctx.output_of("2.1.1")
                ),
                "TASK\n"
                "  Assign every cluster-market above to exactly one `campaign_ref` from the "
                "agreed campaigns, with a one-line `rationale` naming what the cluster's "
                "intent has in common with that campaign's objective. A cluster whose "
                "demand does not belong in any agreed campaign should be left out — it "
                "will be reported as unassigned rather than funded by accident. Then write "
                "`notes` on what the assignment assumes.",
            ),
            task_class=self.spec.task_class,
        )

        known = set(refs)
        assignments = _assignment_map(draft.assignments, known)
        frame = demand.campaign_frame(
            basis.rows,
            assignments=assignments,
            month_count=basis.month_count,
            revenue_campaigns=_revenue_campaigns(ctx, refs),
        )
        checked = await plan.calc.run(VOLUME_CHECK, frame)
        clusters_by_ref: dict[str, list[str]] = {}
        for (cluster, market), ref in sorted(assignments.items()):
            clusters_by_ref.setdefault(ref, []).append(f"{cluster} ({market})")

        campaigns = [
            CampaignCapacity(
                **{key: row[key] for key in row if key in CampaignCapacity.model_fields},
                clusters=clusters_by_ref.get(str(row["campaign_ref"]), []),
            )
            for row in checked.value.get("campaigns", [])
        ]
        placed = set(assignments)
        unassigned = [
            f"{group.cluster} ({group.market})"
            for group in basis.basis.groups
            if (group.cluster, group.market) not in placed
        ]

        return LearningCapacityOutput(
            campaigns=campaigns,
            assignments=[item for item in draft.assignments if item.campaign_ref in known],
            unassigned_clusters=unassigned,
            structure_verdict=checked.value.get("structure_verdict", "sound"),
            notes=draft.notes,
            excluded=[dict(row) for row in checked.result.excluded],
            status=basis.status,
            open_gaps=basis.gaps,
            coverage=gather.coverage_notes(basis.found),
            calc_evidence_ids=[*basis.calc_ids, checked.id],
        )


# ---------------------------------------------------------------------------
# 2.2.3 — budget_scenarios
# ---------------------------------------------------------------------------


class ScenarioNarrative(BaseModel):
    """One scenario's case, in words. No figures."""

    name: ScenarioName
    case_for: str = Field(min_length=1)
    case_against: str = Field(min_length=1)


class ScenariosDraft(BaseModel):
    narratives: list[ScenarioNarrative]
    notes: str = Field(min_length=1)


class AllocationLine(BaseModel):
    """One campaign x market x funnel stage, and what it is given."""

    campaign_ref: str
    market: str
    funnel_stage: str
    usd: float
    pct: float
    forecast_cpa_usd: float
    target_cpa_usd: float
    efficiency: float
    est_conv: float
    est_clicks: float | None = None
    avg_cpc_usd: float | None = None
    #: What this unit can absorb, from the measured impression-share headroom.
    #: Travels to the gate so `POST /approvals/{id}/recalc` can tell an approver
    #: that a raise buys nothing.
    max_spend_usd: float | None = None
    floor_applied: bool = False
    cap_applied: bool = False
    below_floor: bool = False


class BudgetScenario(BaseModel):
    """One of cautious, expected, aggressive."""

    name: ScenarioName
    monthly_total_usd: float
    quarterly_total_usd: float
    allocated_usd: float
    unallocated_usd: float
    working_budget_usd: float
    experiment_reserve_usd: float
    experiment_reserve_pct: float
    est_clicks: float
    est_conv: float
    est_cpa: float | None = None
    est_pipeline_usd: float | None = None
    allocation: list[AllocationLine] = Field(default_factory=list)
    deferred: list[dict[str, Any]] = Field(default_factory=list)
    floor_applied: bool = False
    headroom_capped: bool = False
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    case_for: str = ""
    case_against: str = ""


class BudgetScenariosOutput(BaseModel):
    """2.2.3 — three envelopes, each already split across the plan."""

    scenarios: list[BudgetScenario]
    recommended: ScenarioName
    recommendation_reason: str
    minimum_viable_envelope_usd: float
    forecast_monthly_usd: float
    forecast_cpa_usd: float | None = None
    infeasible_reason: str | None = None
    notes: str
    status: Status
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class BudgetScenariosNode(LLMNode):
    """2.2.3 — what three different appetites buy, each priced and split."""

    spec = NodeSpec(
        id="2.2.3",
        name="budget_scenarios",
        stage="2.2",
        run_stage=RunStage.PLAN,
        depends_on=("2.2.1", "2.2.2", "2.1.2", "2.1.3"),
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=BudgetScenariosOutput,
        connectors=("google_ads",),
        calc=(TRAFFIC, ENVELOPE, SPLIT),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        basis = await _demand(ctx)
        if basis.traffic is None:
            ctx.scratch[self.spec.id] = basis
            return basis.evidence

        plan = ctx.require_plan()
        assignments = _assignments_from(ctx)
        units = demand.allocation_units(
            basis.rows,
            assignments=assignments,
            targets=_targets(ctx),
            month_count=basis.month_count,
            funnel_stages=basis.funnel_stages,
            headroom_pct=basis.headroom_pct,
            # The account-wide target from 2.1.2, for any unit whose campaign
            # has none of its own — a cluster the model left unassigned, most
            # often. Without it `split_v1` excludes the unit, and a run where
            # every cluster is unassigned produces no budget at all.
            default_target_cpa_usd=_blended_target(ctx),
        )
        envelope = await plan.calc.run(
            ENVELOPE,
            pd.DataFrame(basis.rows),
            campaign_count=max(len(set(assignments.values())), 1),
            headroom_pct=basis.headroom_pct,
            target_cpa_usd=_blended_target(ctx),
            acv_usd=_blended_acv(ctx),
        )
        splits: dict[str, Calculation] = {}
        for scenario in envelope.value.get("scenarios", []):
            name = str(scenario["name"])
            try:
                splits[name] = await plan.calc.run(
                    SPLIT, units, envelope_usd=scenario["monthly_total_usd"]
                )
            except CalcError as exc:
                # One scenario that cannot be split is a finding about that
                # envelope, not a dead node: the other two still answer, and
                # `infeasible_reason` carries this one to the gate card.
                basis.gaps.append(f"{SPLIT} ({name}) — {exc}")
                log.warning(
                    "plan.split_failed", node_id=self.spec.id, scenario=name, error=str(exc)
                )

        ctx.scratch[self.spec.id] = (basis, envelope, splits)
        return [*basis.evidence, envelope.evidence, *(item.evidence for item in splits.values())]

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        held = ctx.scratch[self.spec.id]
        if isinstance(held, Demand):
            _insufficient(self.spec.id, held)
        basis, envelope, splits = held

        scenarios = envelope.value.get("scenarios", [])
        draft = await ctx.complete(
            ScenariosDraft,
            system=prompts.system_prompt(
                "You argue three budget scenarios to the person who will sign one of them. "
                "You write the case for and against each. You never write a figure and "
                "never recommend — the recommendation is computed and the decision is a "
                "human's."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "the three scenarios (final — do not restate or adjust)",
                    [
                        {
                            **{
                                key: row[key]
                                for key in (
                                    "name",
                                    "monthly_total_usd",
                                    "est_clicks",
                                    "est_conv",
                                    "est_cpa",
                                    "est_pipeline_usd",
                                    "floor_applied",
                                    "headroom_capped",
                                    "assumptions",
                                    "risks",
                                )
                                if key in row
                            },
                            "allocation": _lines(splits.get(str(row["name"]))),
                        }
                        for row in scenarios
                    ],
                ),
                prompts.computed_block(
                    "the learning-capacity check from 2.2.2",
                    ctx.output_of("2.2.2").get("campaigns") or [],
                ),
                prompts.computed_block(
                    "the ceilings from 2.1.2", ctx.output_of("2.1.2").get("blended") or {}
                ),
                "TASK\n"
                "  For each of the three scenarios write `case_for` and `case_against`, "
                "grounded in the campaigns above — which of them clears its learning "
                "threshold under that envelope, which does not, and what that costs. Then "
                "write `notes` on what all three share.",
            ),
            task_class=self.spec.task_class,
        )

        cases = {str(item.name): item for item in draft.narratives}
        built: list[BudgetScenario] = []
        for row in scenarios:
            name = str(row["name"])
            split = splits.get(name)
            case = cases.get(name)
            built.append(
                BudgetScenario(
                    **{
                        key: row[key]
                        for key in row
                        if key in BudgetScenario.model_fields
                        and key not in {"allocation", "deferred"}
                    },
                    allocated_usd=_figure(split, "allocated_usd", row["monthly_total_usd"]),
                    unallocated_usd=_figure(split, "unallocated_usd", 0.0),
                    allocation=[AllocationLine.model_validate(line) for line in _lines(split)],
                    deferred=list(split.value.get("deferred", [])) if split else [],
                    case_for=case.case_for if case else "",
                    case_against=case.case_against if case else "",
                )
            )

        floor_total = float(envelope.value.get("floor_total_usd") or 0.0)
        infeasible = _infeasible_reason(built, floor_total)
        return BudgetScenariosOutput(
            scenarios=built,
            recommended=envelope.value["recommended"],
            recommendation_reason=envelope.value["recommendation_reason"],
            minimum_viable_envelope_usd=floor_total,
            forecast_monthly_usd=envelope.value["forecast_monthly_usd"],
            forecast_cpa_usd=envelope.value.get("forecast_cpa_usd"),
            infeasible_reason=infeasible,
            notes=draft.notes,
            status="infeasible" if infeasible else basis.status,
            open_gaps=basis.gaps,
            coverage=gather.coverage_notes(basis.found),
            calc_evidence_ids=[
                *basis.calc_ids,
                envelope.id,
                *(item.id for item in splits.values()),
            ],
        )


def _assignments_from(ctx: RunContext) -> dict[tuple[str, str], str]:
    """2.2.2's cluster-to-campaign assignment, read back off its output.

    Off the output rather than recomputed, so that the split a budget owner
    approves is built on the same placement the capacity check was built on.
    Two calls to the model would eventually disagree, and the disagreement
    would surface as a campaign whose forecast does not match its own verdict.
    """
    return {
        (str(item["cluster"]), str(item["market"])): str(item["campaign_ref"])
        for item in ctx.output_of("2.2.2").get("assignments") or []
        if item.get("cluster") and item.get("market") and item.get("campaign_ref")
    }


def _blended_target(ctx: RunContext) -> float | None:
    """The account-wide target CPA from 2.1.2, for the scenario recommendation."""
    blended = ctx.output_of("2.1.2").get("blended") or {}
    value = blended.get("target_cpa_won_usd") or blended.get("target_cpl_usd")
    return float(value) if isinstance(value, int | float) and value > 0 else None


def _blended_acv(ctx: RunContext) -> float | None:
    blended = ctx.output_of("2.1.2").get("blended") or {}
    value = blended.get("acv_usd")
    return float(value) if isinstance(value, int | float) and value > 0 else None


def _lines(split: Calculation | None) -> list[dict[str, Any]]:
    return list(split.value.get("allocation", [])) if split is not None else []


def _figure(split: Calculation | None, key: str, default: float) -> float:
    if split is None:
        return default
    value = split.value.get(key)
    return float(value) if isinstance(value, int | float) else default


def _infeasible_reason(scenarios: Sequence[BudgetScenario], floor_total: float) -> str | None:
    """PRD §18's "envelope too small for the slate", and its mirror image.

    Two shapes, and both are worth naming rather than rendering as a thin
    spread across everything:

    * **Too small** — even the aggressive envelope cannot fund the slate to its
      per-campaign floor, so some campaign is deferred whatever is chosen.
    * **Too large** — every scenario leaves money unallocated because the
      demand cannot absorb it. That is not a budget to celebrate; it is a
      forecast saying "there is nothing more to buy at this price".
    """
    if not scenarios:
        return "no scenario could be built from this forecast"
    if all(scenario.deferred for scenario in scenarios):
        deferred = sorted(
            {
                str(row.get("unit") or row.get("campaign_ref") or "?")
                for row in scenarios[0].deferred
            }
        )
        return (
            f"no scenario funds the whole slate to the ${floor_total:,.2f} minimum: "
            f"{', '.join(deferred)} would be deferred. Cut markets or channels, or raise "
            "the envelope."
        )
    if all(scenario.unallocated_usd > 0 for scenario in scenarios):
        return (
            "every scenario leaves budget unallocated — the forecast demand cannot absorb "
            "it at the impression share already held. The envelope is bounded by demand, "
            "not by money."
        )
    return None


# ---------------------------------------------------------------------------
# 2.2.4 ⛳ G3 — budget_allocation
# ---------------------------------------------------------------------------


class ScenarioChoice(BaseModel):
    """Which envelope to put in front of the budget owner, and why. No figures."""

    chosen_scenario: ScenarioName = Field(
        description="One of cautious, expected, aggressive. Never a fourth."
    )
    rationale: str = Field(min_length=1)
    what_would_change_it: str = Field(
        min_length=1,
        description="The one observation that would make a different scenario right.",
    )


class Envelope(BaseModel):
    """What the plan commits to per month, and per quarter.

    `monthly_cap_usd` is the **allocated** total, not the scenario headline —
    PRD §12 invariant 4 requires the allocation to sum to it, and a measured
    absorption cap can leave a scenario's headline unspendable.
    """

    monthly_cap_usd: float
    quarterly_cap_usd: float
    currency: str = "USD"
    scenario_total_usd: float
    unallocated_usd: float = 0.0
    unallocated_reason: str | None = None


class LearningWarning(BaseModel):
    """A campaign this envelope cannot get out of the learning period."""

    campaign_ref: str
    forecast_conv_30d: float
    threshold: float
    verdict: Verdict
    remedy: Remedy | None = None


class BudgetAllocationOutput(BaseModel):
    """2.2.4 ⛳ G3 — the envelope and the split a human is asked to approve."""

    chosen_scenario: ScenarioName
    rationale: str
    what_would_change_it: str
    envelope: Envelope
    allocation: list[AllocationLine]
    experiment_reserve_pct: float
    experiment_reserve_usd: float
    learning_warnings: list[LearningWarning] = Field(default_factory=list)
    deferred: list[dict[str, Any]] = Field(default_factory=list)
    #: Filled by the approver's edit, never by this node. Present in the
    #: proposal so that an edited proposal validates against the same model.
    edits_applied: list[dict[str, Any]] = Field(default_factory=list)
    degraded_sources: list[str] = Field(default_factory=list)
    confidence_band: ConfidenceBand
    status: Status
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class BudgetAllocationNode(LLMNode):
    """2.2.4 ⛳ G3 — the money, and the one gate that can hand a number back."""

    spec = NodeSpec(
        id="2.2.4",
        name="budget_allocation",
        stage="2.2",
        run_stage=RunStage.PLAN,
        depends_on=("2.2.3", "2.2.2"),
        gate=True,
        gate_key="G3",
        # §5.3 routes G3 to the budget owner. `approver` is the role; which
        # person is `Project.settings.plan_approvers["G3"]`.
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=BudgetAllocationOutput,
        connectors=("google_ads",),
        calc=(TRAFFIC, SPLIT),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        basis = await _demand(ctx)
        ctx.scratch[self.spec.id] = basis
        return basis.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        basis: Demand = ctx.scratch[self.spec.id]
        if basis.traffic is None:
            _insufficient(self.spec.id, basis)

        proposed = ctx.output_of("2.2.3")
        scenarios = {str(row["name"]): row for row in proposed.get("scenarios") or []}
        if not scenarios:
            _insufficient(self.spec.id, basis)

        choice = await ctx.complete(
            ScenarioChoice,
            system=prompts.system_prompt(
                "You choose which of three costed budget scenarios to put in front of the "
                "budget owner for signature. You choose a name and give a reason. You never "
                "write a figure and never invent a fourth scenario."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "the three scenarios and the computed recommendation (final)",
                    {
                        "recommended": proposed.get("recommended"),
                        "recommendation_reason": proposed.get("recommendation_reason"),
                        "minimum_viable_envelope_usd": proposed.get("minimum_viable_envelope_usd"),
                        "infeasible_reason": proposed.get("infeasible_reason"),
                        "scenarios": [
                            {
                                key: row.get(key)
                                for key in (
                                    "name",
                                    "monthly_total_usd",
                                    "allocated_usd",
                                    "unallocated_usd",
                                    "est_conv",
                                    "est_cpa",
                                    "est_pipeline_usd",
                                    "case_for",
                                    "case_against",
                                    "risks",
                                )
                            }
                            for row in scenarios.values()
                        ],
                    },
                ),
                prompts.computed_block(
                    "the learning-capacity check from 2.2.2",
                    ctx.output_of("2.2.2").get("campaigns") or [],
                ),
                "TASK\n"
                "  Choose `chosen_scenario`. The computed recommendation above is the "
                "default and you should depart from it only for a reason you can state in "
                "one sentence. Give the `rationale`, and `what_would_change_it` — the one "
                "observation that would make a different scenario right.",
            ),
            task_class=self.spec.task_class,
        )

        chosen = scenarios.get(choice.chosen_scenario) or scenarios[str(proposed["recommended"])]
        allocation = [
            AllocationLine.model_validate(line) for line in chosen.get("allocation") or []
        ]
        allocated = float(chosen.get("allocated_usd") or 0.0)
        unallocated = float(chosen.get("unallocated_usd") or 0.0)

        capacity = {
            str(row["campaign_ref"]): row for row in ctx.output_of("2.2.2").get("campaigns") or []
        }
        funded = {line.campaign_ref for line in allocation}
        warnings = [
            LearningWarning(
                campaign_ref=ref,
                forecast_conv_30d=row["forecast_conv_30d"],
                threshold=row["threshold"],
                verdict=row["verdict"],
                remedy=row.get("remedy"),
            )
            for ref, row in sorted(capacity.items())
            if ref in funded and row.get("verdict") != "clears"
        ]

        return BudgetAllocationOutput(
            chosen_scenario=chosen["name"],
            rationale=choice.rationale,
            what_would_change_it=choice.what_would_change_it,
            envelope=Envelope(
                monthly_cap_usd=allocated,
                quarterly_cap_usd=_quarterly(chosen, allocated),
                currency=_currency(ctx),
                scenario_total_usd=float(chosen.get("monthly_total_usd") or allocated),
                unallocated_usd=unallocated,
                unallocated_reason=(
                    "the forecast demand cannot absorb it at the impression share already held"
                    if unallocated > 0
                    else None
                ),
            ),
            allocation=allocation,
            experiment_reserve_pct=float(chosen.get("experiment_reserve_pct") or 0.0),
            experiment_reserve_usd=float(chosen.get("experiment_reserve_usd") or 0.0),
            learning_warnings=warnings,
            deferred=list(chosen.get("deferred") or []),
            degraded_sources=basis.degraded,
            confidence_band=ConfidenceBand.model_validate(basis.result["confidence_band"]),
            status=basis.status,
            open_gaps=basis.gaps,
            coverage=gather.coverage_notes(basis.found),
            # 2.2.3 wrote the split this node presents; `DerivedWriter` dedupes
            # on `(plan_run_id, formula_id, inputs_hash)`, so re-running it here
            # against the chosen envelope resolves to that same row and this
            # node can legitimately cite a `derived` row it produced.
            calc_evidence_ids=[*basis.calc_ids, *(await self._cite_split(ctx, basis, chosen))],
        )

    async def _cite_split(
        self, ctx: RunContext, basis: Demand, chosen: dict[str, Any]
    ) -> list[uuid.UUID]:
        """Re-run the chosen scenario's split so this node cites a row it produced."""
        plan = ctx.require_plan()
        units = demand.allocation_units(
            basis.rows,
            assignments=_assignments_from(ctx),
            targets=_targets(ctx),
            month_count=basis.month_count,
            funnel_stages=basis.funnel_stages,
            headroom_pct=basis.headroom_pct,
            # The account-wide target from 2.1.2, for any unit whose campaign
            # has none of its own — a cluster the model left unassigned, most
            # often. Without it `split_v1` excludes the unit, and a run where
            # every cluster is unassigned produces no budget at all.
            default_target_cpa_usd=_blended_target(ctx),
        )
        split = await plan.calc.run(SPLIT, units, envelope_usd=float(chosen["monthly_total_usd"]))
        return [split.id]


def _quarterly(scenario: dict[str, Any], allocated: float) -> float:
    """Three months of the committed envelope, as `scenarios.envelope_v1` scaled it.

    Read off the scenario rather than multiplied here: `x 3` on a field is
    arithmetic in a plan node, which the CI guard fails and law 14 forbids.
    Where the allocation committed less than the headline, the ratio is carried
    by `Envelope.scenario_total_usd` and the quarterly figure stays the
    scenario's own — a cap that bites in month one bites in months two and
    three identically, so scaling it down twice would understate the commitment.
    """
    value = scenario.get("quarterly_total_usd")
    return float(value) if isinstance(value, int | float) else allocated


def _currency(ctx: RunContext) -> str:
    """The project's first market's currency, or USD.

    `calc/` works in one unit and the whole plan is denominated in it; this
    records which one the figures are to be read as, rather than converting
    anything. A project spanning currencies is a finding for the gate card, not
    a conversion this node is entitled to make.
    """
    markets = ctx.require_plan().input.markets
    return markets[0].currency if markets else "USD"


# ---------------------------------------------------------------------------
# 2.2.5 — reallocation_rules
# ---------------------------------------------------------------------------

TriggerMetric = Literal["cpa", "cpl", "roas", "conversions", "impression_share", "spend_pace"]
Comparison = Literal["above", "below"]
ShiftSize = Literal["standard", "half", "none"]


class RuleDraft(BaseModel):
    """One reallocation rule, as the model is asked for it. No figures."""

    id: str = Field(min_length=1)
    trigger_metric: TriggerMetric
    comparison: Comparison
    threshold_basis: Literal[
        "target_cpa", "forecast_cpa", "monthly_budget", "learning_threshold", "forecast_conv_30d"
    ] = Field(description="Which already-computed figure this rule fires against. Never a number.")
    from_campaign: str = Field(min_length=1)
    to_campaign: str = Field(min_length=1)
    shift_size: ShiftSize = Field(
        description="`standard` is the configured maximum shift, `half` is a cautious one, "
        "`none` means raise the alarm and move nothing."
    )
    requires_human: bool
    rationale: str = Field(min_length=1)


class RulesDraft(BaseModel):
    rules: list[RuleDraft]
    review_cadence: Literal["weekly", "fortnightly", "monthly", "quarterly"]
    notes: str = Field(min_length=1)


class ReallocationRule(BaseModel):
    """One rule, with every figure resolved from a calculation or a constant."""

    id: str
    trigger_metric: TriggerMetric
    comparison: Comparison
    threshold: float
    threshold_basis: str
    lookback_days: float
    from_campaign: str
    to_campaign: str
    max_shift_pct: float
    max_shift_usd: float
    cooldown_days: float
    requires_human: bool
    rationale: str


class ReallocationRulesOutput(BaseModel):
    """2.2.5 — when money moves between campaigns after launch, and how much."""

    rules: list[ReallocationRule]
    review_cadence: Literal["weekly", "fortnightly", "monthly", "quarterly"]
    notes: str
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    status: Status
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class ReallocationRulesNode(LLMNode):
    """2.2.5 — the standing instructions that keep an approved budget honest."""

    spec = NodeSpec(
        id="2.2.5",
        name="reallocation_rules",
        stage="2.2",
        run_stage=RunStage.PLAN,
        depends_on=("2.2.4", "2.1.3"),
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=ReallocationRulesOutput,
        # Re-checked against the **approved** allocation, not the forecast:
        # 2.2.2 asked whether the forecast clears the threshold, and this asks
        # whether the money a human actually signed does. Different inputs,
        # different `PlanCalc` row, and it is the second one a rule must not
        # push a campaign below.
        calc=(VOLUME_CHECK,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        plan = ctx.require_plan()
        approved = ctx.output_of("2.2.4")
        frame = _approved_frame(approved, ctx)
        checked = await plan.calc.run(VOLUME_CHECK, frame)
        ctx.scratch[self.spec.id] = checked
        return [checked.evidence]

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        checked: Calculation = ctx.scratch[self.spec.id]
        plan = ctx.require_plan()
        constants = plan.constants
        approved = ctx.output_of("2.2.4")
        by_ref = {str(row["campaign_ref"]): row for row in checked.value.get("campaigns", [])}

        draft = await ctx.complete(
            RulesDraft,
            system=prompts.system_prompt(
                "You write the standing rules that move budget between campaigns after "
                "launch. You choose what each rule watches, which already-computed figure "
                "it fires against, and which campaign gives and which receives. You never "
                "write a threshold, a percentage or a number of days — every figure is "
                "filled in from the calculations and the planning constants."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "the approved envelope and split (final)",
                    {
                        "envelope": approved.get("envelope"),
                        "allocation": approved.get("allocation"),
                        "learning_warnings": approved.get("learning_warnings"),
                    },
                ),
                prompts.computed_block(
                    "each campaign against its learning threshold, at the approved budget",
                    list(by_ref.values()),
                ),
                prompts.computed_block(
                    "the targets agreed at gate G1", ctx.output_of("2.1.3").get("objectives") or []
                ),
                "TASK\n"
                "  Write the rules that should move money between these campaigns once they "
                "are running. For each, name the `trigger_metric`, whether it fires `above` "
                "or `below`, and the `threshold_basis` — which computed figure it is "
                "measured against. Name `from_campaign` and `to_campaign` from the "
                "allocation above. Choose a `shift_size`, and set `requires_human` true for "
                "anything that would take a campaign below its learning threshold or "
                "changes a market's presence. Then choose the `review_cadence`.",
            ),
            task_class=self.spec.task_class,
        )

        max_shift = constants.get("reallocation.max_shift_pct")
        lookback = constants.get("reallocation.lookback_days")
        cooldown = constants.get("reallocation.cooldown_days")
        # Both halves computed in `planning/`: the percentage ladder and the
        # money each donor can actually spare. A plan node that divided a
        # constant by two would be doing arithmetic on a planning figure, which
        # is the thing the CI guard exists to catch.
        capacity = demand.shift_capacity(
            checked.value.get("campaigns", []), max_shift_pct=max_shift.value
        )
        shift_pct = demand.shift_percentages(max_shift.value)

        rules: list[ReallocationRule] = []
        rejected: list[dict[str, Any]] = []
        for item in draft.rules:
            donor = by_ref.get(item.from_campaign)
            if donor is None or item.to_campaign not in by_ref:
                rejected.append(
                    {
                        "id": item.id,
                        "reason": (
                            f"names a campaign that has no approved budget: "
                            f"{item.from_campaign} -> {item.to_campaign}"
                        ),
                    }
                )
                continue
            figure = _threshold(by_ref, item)
            if figure is None:
                rejected.append(
                    {
                        "id": item.id,
                        "reason": (
                            f"{item.threshold_basis} is not a computed figure for "
                            f"{item.from_campaign}"
                        ),
                    }
                )
                continue
            pct = shift_pct[item.shift_size]
            rules.append(
                ReallocationRule(
                    id=item.id,
                    trigger_metric=item.trigger_metric,
                    comparison=item.comparison,
                    threshold=figure,
                    threshold_basis=item.threshold_basis,
                    lookback_days=lookback.value,
                    from_campaign=item.from_campaign,
                    to_campaign=item.to_campaign,
                    max_shift_pct=pct,
                    max_shift_usd=_shift_usd(capacity, item),
                    cooldown_days=cooldown.value,
                    # A donor that is already marginal or below cannot afford to
                    # give: taking budget off it restarts a learning period it
                    # has not finished. The model may ask for a human; a donor
                    # under its threshold gets one whether it asked or not.
                    requires_human=item.requires_human or donor.get("verdict") != "clears",
                    rationale=item.rationale,
                )
            )

        return ReallocationRulesOutput(
            rules=rules,
            review_cadence=draft.review_cadence,
            notes=draft.notes,
            rejected=rejected,
            status="ok",
            open_gaps=[],
            coverage=[],
            calc_evidence_ids=[checked.id],
        )


def _approved_frame(approved: dict[str, Any], ctx: RunContext) -> pd.DataFrame:
    """The approved split, per campaign, as `structure.volume_check_v1` reads it.

    Built in `planning/` rather than here — adding two allocation lines
    together is arithmetic, and a plan node that can add can also invent.
    """
    return demand.approved_campaign_frame(
        approved.get("allocation") or [],
        capacity=ctx.output_of("2.2.2").get("campaigns") or [],
    )


def _threshold(by_ref: dict[str, dict[str, Any]], rule: RuleDraft) -> float | None:
    """The computed figure this rule fires against, selected never invented."""
    key = THRESHOLD_BASIS.get(rule.threshold_basis)
    if key is None:  # pragma: no cover — the draft's Literal bounds it
        return None
    row = by_ref.get(rule.from_campaign, {})
    value = row.get(key)
    return float(value) if isinstance(value, int | float) else None


def _shift_usd(capacity: dict[str, dict[str, float]], rule: RuleDraft) -> float:
    """The most this rule may move, in money. Selected, never computed here.

    `planning.demand.shift_capacity` bounds it twice — by the configured
    maximum shift, and by the surplus the donor holds over its own learning
    threshold. This reads the figure that applies to the size the model asked
    for; a donor with no surplus gives zero, which reads as the refusal it is
    rather than as a small permission.
    """
    if rule.shift_size == "none":
        return 0.0
    return capacity.get(rule.from_campaign, {}).get(rule.shift_size, 0.0)


demand_forecast = DemandForecastNode()
learning_capacity_check = LearningCapacityNode()
budget_scenarios = BudgetScenariosNode()
budget_allocation = BudgetAllocationNode()
reallocation_rules = ReallocationRulesNode()
