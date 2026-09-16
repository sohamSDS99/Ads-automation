"""Stage 1.2 — learn from what we already ran (PRD §10).

Three nodes, all reading our own Google Ads history. None is a gate.

`1.2.2 search_term_pnl` is the one the PRD calls out explicitly — "**computed in
pandas, LLM only labels/clusters**" — and it is built to make that literally
true rather than merely intended: `pnl.compute()` produces the table, the model
is shown it and asked for a recommended action per wasteful term plus a
clustering, and the merge back in `reason()` takes every number from the table
and only the labels from the model. No cost, CPA or ROAS figure in the output
has passed through a language model.

`1.2.1` works the same way for campaign deltas. `1.2.3` is qualitative — "what
did we try that did not work" — and is a straight prompt over the account's
change history.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal

import structlog
from pydantic import BaseModel, Field

from agent.db.models import Evidence
from agent.llm.router import TaskClass
from agent.nodes import frames, gather, pnl, prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.nodes.stage_1_1 import CAMPAIGN_PERF, CHANGE_LOG

log = structlog.get_logger(__name__)

SEARCH_TERM_PNL = "search_term_pnl"

#: What may be done about a term that spent money and converted nobody. A fixed
#: vocabulary rather than free text: this list is what the P4 negative-keyword
#: builder reads, and "pause it maybe?" is not actionable input to a program.
Action = Literal[
    "negative_exact",
    "negative_phrase",
    "negative_broad",
    "tighten_match_type",
    "lower_bid",
    "fix_landing_page",
    "keep_watching",
]


# ---------------------------------------------------------------------------
# 1.2.1 — historical_performance
# ---------------------------------------------------------------------------


class CampaignVerdict(BaseModel):
    """The model's read of one computed campaign row, keyed back to it."""

    campaign: str = Field(description="Copy the `campaign` name exactly.")
    verdict: Literal["winner", "loser", "neutral"]
    why: str = Field(default="", description="One sentence, from the numbers shown.")


class HistoricalReading(BaseModel):
    """What the model returns for 1.2.1."""

    verdicts: list[CampaignVerdict] = Field(default_factory=list)
    structural_findings: list[str] = Field(
        default_factory=list,
        description="Account-level patterns: budget concentration, match-type mix, drift.",
    )


class CampaignResult(BaseModel):
    """1.2.1's campaign row: the frame's numbers, the model's verdict."""

    campaign: str
    verdict: Literal["winner", "loser", "neutral"] = "neutral"
    why: str = ""
    period: str
    cost: float
    conversions: float
    conversion_value: float
    cpa: float | None = None
    roas: float | None = None
    metric_delta: float | None = Field(
        default=None, description="Percent change in CPA, first half of the window to second."
    )
    evidence_ids: list[str] = Field(default_factory=list)


class HistoricalPerformance(BaseModel):
    """1.2.1 output."""

    winners: list[CampaignResult] = Field(default_factory=list)
    losers: list[CampaignResult] = Field(default_factory=list)
    neutral: list[CampaignResult] = Field(default_factory=list)
    structural_findings: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class HistoricalPerformanceNode(LLMNode):
    """1.2.1 — which campaigns worked, which did not, and what the account does structurally."""

    spec = NodeSpec(
        id="1.2.1",
        name="historical_performance",
        stage="1.2",
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=HistoricalPerformance,
        connectors=("google_ads",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(ctx, gather.Need(CAMPAIGN_PERF, connector="google_ads"))
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        rows = frames.campaign_rollup(
            found.payloads(CAMPAIGN_PERF), [row.id for row in found.of(CAMPAIGN_PERF)]
        )
        if not rows:
            return HistoricalPerformance(coverage=gather.coverage_notes(found))

        reading = await ctx.complete(
            HistoricalReading,
            system=prompts.system_prompt(
                "You read a paid-search account's own history and say what worked. The "
                "arithmetic is done: costs, CPA, ROAS and the half-over-half CPA change are "
                "final. Judge them, do not recompute them."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "campaigns over the window (final — `cpa_delta_pct` is percent change "
                    "in CPA from the first half to the second; lower is better)",
                    [row.as_dict() for row in rows],
                ),
                prompts.coverage_block(found),
                "TASK\n"
                "  Return one verdict per campaign, copying `campaign` exactly: `winner`, "
                "`loser` or `neutral`, with one sentence of justification drawn from the "
                "numbers. Then list the structural findings — what this account does as a "
                "whole that helps or hurts it.",
            ),
        )
        verdicts = {item.campaign: item for item in reading.verdicts}
        results = [
            CampaignResult(
                campaign=row.campaign,
                verdict=verdicts[row.campaign].verdict if row.campaign in verdicts else "neutral",
                why=verdicts[row.campaign].why if row.campaign in verdicts else "",
                period=row.period,
                cost=row.cost,
                conversions=row.conversions,
                conversion_value=row.conversion_value,
                cpa=row.cpa,
                roas=row.roas,
                metric_delta=row.cpa_delta_pct,
                evidence_ids=[str(item) for item in row.evidence_ids],
            )
            for row in rows
        ]
        return HistoricalPerformance(
            winners=[item for item in results if item.verdict == "winner"],
            losers=[item for item in results if item.verdict == "loser"],
            neutral=[item for item in results if item.verdict == "neutral"],
            structural_findings=reading.structural_findings,
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.2.2 — search_term_pnl
# ---------------------------------------------------------------------------


class TermAction(BaseModel):
    """What to do about one wasteful term. The model's only numeric-adjacent job."""

    term: str = Field(description="Copy the `term` exactly.")
    recommended_action: Action
    reason: str = ""


class TermCluster(BaseModel):
    """A theme across terms, so P4 can act on groups rather than 100 strings."""

    theme: str
    verdict: Literal["profitable", "wasteful", "mixed"]
    terms: list[str] = Field(default_factory=list)
    note: str = ""


class TermLabelling(BaseModel):
    """What the model returns for 1.2.2. Contains no money."""

    actions: list[TermAction] = Field(default_factory=list)
    clusters: list[TermCluster] = Field(default_factory=list)


class ProfitableTerm(BaseModel):
    term: str
    cost: float
    conversions: float
    conversion_value: float
    cpa: float | None = None
    roas: float | None = None
    clicks: int = 0
    impressions: int = 0
    campaigns: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class WastefulTerm(BaseModel):
    term: str
    cost: float
    conversions: float = 0.0
    clicks: int = 0
    impressions: int = 0
    recommended_action: Action = "keep_watching"
    reason: str = ""
    campaigns: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class PnlTotals(BaseModel):
    terms: int = 0
    cost: float = 0.0
    conversions: float = 0.0
    conversion_value: float = 0.0
    wasted_spend: float = 0.0
    waste_pct: float = 0.0
    profitable_terms: int = 0
    wasteful_terms: int = 0
    blended_cpa: float | None = None
    blended_roas: float | None = None


class SearchTermPnlOutput(BaseModel):
    """1.2.2 output."""

    profitable_terms: list[ProfitableTerm] = Field(default_factory=list)
    wasteful_terms: list[WastefulTerm] = Field(default_factory=list)
    clusters: list[TermCluster] = Field(default_factory=list)
    totals: PnlTotals = Field(default_factory=PnlTotals)
    truncated: dict[str, int] = Field(
        default_factory=dict,
        description="Terms omitted from each list by the top-N cut. Totals still count them.",
    )
    coverage: list[str] = Field(default_factory=list)


class SearchTermPnlNode(LLMNode):
    """1.2.2 — where the money went, and what bought nothing.

    PRD §10: computed in pandas, LLM only labels and clusters. The merge at the
    bottom of `reason()` is where that promise is kept.
    """

    spec = NodeSpec(
        id="1.2.2",
        name="search_term_pnl",
        stage="1.2",
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=SearchTermPnlOutput,
        connectors=("google_ads",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(ctx, gather.Need(SEARCH_TERM_PNL, connector="google_ads"))
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        rows = found.of(SEARCH_TERM_PNL)
        table = pnl.compute([dict(row.payload) for row in rows], [row.id for row in rows])
        if table.is_empty:
            return SearchTermPnlOutput(coverage=gather.coverage_notes(found))

        await ctx.progress(
            f"priced {table.totals.terms} search terms — "
            f"{table.totals.waste_pct}% of spend converted nobody"
        )
        labelling = await ctx.complete(
            TermLabelling,
            system=prompts.system_prompt(
                "You label a search-term profit and loss statement that has already been "
                "computed. Recommend an action for each wasteful term and group the terms "
                "into themes. You never state a cost, a CPA or a ROAS."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "totals for the whole window (final)", table.totals.as_dict()
                ),
                prompts.computed_block(
                    "terms that converted (final)", [row.as_dict() for row in table.profitable]
                ),
                prompts.computed_block(
                    "terms that spent money and converted nobody (final)",
                    [row.as_dict() for row in table.wasteful],
                ),
                prompts.coverage_block(found),
                "TASK\n"
                "  Return one action per wasteful term, copying `term` exactly, chosen from "
                "the allowed values. Then cluster the terms above into themes, each with a "
                "verdict. Do not repeat any figure from the tables.",
            ),
        )
        actions = {item.term: item for item in labelling.actions}
        unknown = sorted(set(actions) - {row.term for row in table.wasteful})
        if unknown:
            log.info("node.unknown_terms", node_id=self.spec.id, count=len(unknown))

        return SearchTermPnlOutput(
            profitable_terms=[
                ProfitableTerm(
                    term=row.term,
                    cost=row.cost,
                    conversions=row.conversions,
                    conversion_value=row.conversion_value,
                    cpa=row.cpa,
                    roas=row.roas,
                    clicks=row.clicks,
                    impressions=row.impressions,
                    campaigns=list(row.campaigns),
                    evidence_ids=_ids(row.evidence_ids),
                )
                for row in table.profitable
            ],
            wasteful_terms=[
                WastefulTerm(
                    term=row.term,
                    cost=row.cost,
                    conversions=row.conversions,
                    clicks=row.clicks,
                    impressions=row.impressions,
                    recommended_action=(
                        actions[row.term].recommended_action
                        if row.term in actions
                        else "keep_watching"
                    ),
                    reason=actions[row.term].reason if row.term in actions else "",
                    campaigns=list(row.campaigns),
                    evidence_ids=_ids(row.evidence_ids),
                )
                for row in table.wasteful
            ],
            clusters=labelling.clusters,
            totals=PnlTotals(**table.totals.as_dict()),
            truncated=dict(table.truncated),
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.2.3 — failed_experiments
# ---------------------------------------------------------------------------


class FailedExperiment(BaseModel):
    what: str
    when: str = Field(default="", description="As precise as the change history allows.")
    outcome: str = ""
    do_not_repeat_reason: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class FailedExperiments(BaseModel):
    """1.2.3 output."""

    tried_and_failed: list[FailedExperiment] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class FailedExperimentsNode(LLMNode):
    """1.2.3 — what this account already tried, so P4 does not propose it again."""

    spec = NodeSpec(
        id="1.2.3",
        name="failed_experiments",
        stage="1.2",
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=FailedExperiments,
        connectors=("google_ads",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx,
            gather.Need(CHANGE_LOG, connector="google_ads"),
            gather.Need(CAMPAIGN_PERF, connector="google_ads"),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    def system_prompt(self, ctx: RunContext) -> str:
        return prompts.system_prompt(
            "You reconstruct what a paid-search account has already tried and abandoned, "
            "from its change history and the performance around each change. Only report an "
            "experiment you can see in the evidence — an absence of changes means an empty "
            "list, not a guess at what a team like this usually tries."
        )

    def user_prompt(self, ctx: RunContext, ev: Sequence[Evidence]) -> str:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        return prompts.compose(
            prompts.project_block(ctx.project),
            prompts.evidence_block(found, CHANGE_LOG, title="account change history", limit=80),
            prompts.evidence_block(found, CAMPAIGN_PERF, title="campaign performance by month"),
            prompts.coverage_block(found),
            prompts.cite_from(
                "EVIDENCE — account change history",
                "EVIDENCE — campaign performance by month",
            ),
            "TASK\n"
            "  Return each thing this account tried that did not work: what was changed, "
            "when, what happened after it, and why it should not be repeated.",
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        result = await super().reason(ctx, ev)
        found: gather.Gathered = ctx.scratch[self.spec.id]
        return result.model_copy(update={"coverage": gather.coverage_notes(found)})


def _ids(values: tuple[uuid.UUID, ...]) -> list[str]:
    return [str(item) for item in values]


historical_performance = HistoricalPerformanceNode()
search_term_pnl = SearchTermPnlNode()
failed_experiments = FailedExperimentsNode()
