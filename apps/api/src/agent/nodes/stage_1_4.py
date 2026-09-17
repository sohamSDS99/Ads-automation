"""Stage 1.4 — find the demand (PRD §10).

Five nodes, none of them a gate. Their job is to turn "what do people actually
type" into a priced, classified, mapped list a campaign can be built from.

Two of the five make **no model call at all**, and that is the design rather
than an omission:

* `1.4.3 demand_metrics` is described in PRD §10 as "joined from DataForSEO, not
  generated". A join is a join. Asking a model to restate a volume it was shown
  would add cost, latency and a way to be wrong, and would buy nothing.
* `1.4.4 negative_blocklist` merges three lists that already carry their own
  reasons — a term our own money proved wasteful, a disqualifier legal already
  approved, a classification 1.4.2 already made. The value is the merge and the
  guard inside it, both of which are code.

The guard is worth naming here because it is the expensive failure this stage
prevents: `keywords.blocklist` refuses to block a term node 1.2.2 measured as
converting, whatever the other two sources say, and reports the collision in
`withheld` instead of resolving it quietly. A profitable keyword blocked because
it also pattern-matches a lost deal is invisible the moment it ships.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from agent.db.models import Evidence
from agent.llm.router import TaskClass
from agent.nodes import batching, gather, keywords, prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.nodes.gather import Gathered
from agent.nodes.stage_1_1 import PAGE
from agent.nodes.stage_1_2 import SEARCH_TERM_PNL
from agent.nodes.stage_1_3 import SERP_QUESTION, SERP_RELATED

log = structlog.get_logger(__name__)

KEYWORD_METRICS = "keyword_metrics"

#: Seed phrases handed to the vendor's idea endpoint. It takes a handful, not a
#: corpus, and the ones that earn a place are the terms our own money already
#: moved through.
VENDOR_SEEDS = 20

#: Model-proposed topics. They are hypotheses, not evidence, and every one is
#: priced by 1.4.3 before anything is built on it — so the list is small and
#: clearly sourced rather than large and indistinguishable from measurement.
MAX_TOPICS = 40

#: Terms per classification call. PRD §10 1.4.2's number.
TERMS_PER_CALL = 100

#: Unpriced terms named individually in the output. The count is always exact;
#: this bounds only the list.
MAX_UNPRICED_LISTED = 200


# ---------------------------------------------------------------------------
# 1.4.1 — keyword_universe
# ---------------------------------------------------------------------------


class SeedTopics(BaseModel):
    """Phrases the model believes a buyer might type. Hypotheses, not findings."""

    topics: list[str] = Field(
        default_factory=list,
        description="Search phrases a buyer of this product might type. Two to six words each.",
    )


class UniverseKeyword(BaseModel):
    term: str
    source: list[str] = Field(default_factory=list)
    market: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class KeywordUniverse(BaseModel):
    """1.4.1 output."""

    keywords: list[UniverseKeyword] = Field(default_factory=list)
    total_terms: int = 0
    source_counts: dict[str, int] = Field(default_factory=dict)
    terms_omitted: int = 0
    coverage: list[str] = Field(default_factory=list)


class KeywordUniverseNode(LLMNode):
    """1.4.1 — every phrase worth pricing, from seven independent sources.

    PRD §10 targets ≥ 2,000 deduped seeds. That number is only reachable by
    combining sources, which is also why `source` on each term is a list: a
    phrase our search-term report, a competitor's headline and the keyword
    vendor all produced is a different kind of candidate from one only the
    vendor guessed at, and `assemble` orders on exactly that.

    Two of the seven come off the live result pages node 1.3.1 bought —
    `serp_related` and `serp_questions`. They matter disproportionately for a
    market the keyword vendor covers thinly: they are the phrasing Google itself
    associates with the term, and they cost nothing extra because the page has
    already been paid for.
    """

    spec = NodeSpec(
        id="1.4.1",
        name="keyword_universe",
        stage="1.4",
        depends_on=("1.1.1", "1.2.2", "1.3.2"),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=KeywordUniverse,
        connectors=("dataforseo",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # Seeds are built from evidence and upstream outputs only — no model
        # call happens in `gather`, because a cache hit returns before `reason`
        # and a completion paid for there would never reach the ledger's record
        # of this node.
        found = await gather.collect(
            ctx,
            gather.Need(SEARCH_TERM_PNL, limit=5_000),
            gather.Need(PAGE, limit=200),
            # Bought by node 1.3.1's SERP pull, so neither need names a
            # connector — the pages are already in the store and asking the
            # proxy for them again would be paying twice for one fact.
            gather.Need(SERP_RELATED, limit=200),
            gather.Need(SERP_QUESTION, limit=200),
        )
        seeds = _vendor_seeds(ctx, found)
        vendor = await gather.collect(
            ctx,
            gather.Need(
                KEYWORD_METRICS,
                connector="dataforseo",
                params={"domain": ctx.project.domain, "seeds": seeds},
                limit=keywords.MAX_TERMS,
            ),
        )
        found.evidence.extend(vendor.evidence)
        found.missing.extend(vendor.missing)
        found.degraded.update(vendor.degraded)
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        market = _market(ctx)
        seeds: list[keywords.Seed] = []

        for row in found.of(SEARCH_TERM_PNL):
            term = row.payload.get("search_term")
            if term:
                seeds.append(
                    keywords.Seed(str(term), "our_search_terms", market, evidence_id=row.id)
                )

        for row in found.of(KEYWORD_METRICS):
            term = row.payload.get("keyword")
            if term:
                origin = str(row.payload.get("origin") or "vendor")
                seeds.append(
                    keywords.Seed(str(term), f"keyword_vendor_{origin}", market, evidence_id=row.id)
                )

        for row in found.of(PAGE):
            for phrase in keywords.phrases(
                " ".join(str(row.payload.get(field) or "") for field in ("title", "h1"))
            ):
                seeds.append(keywords.Seed(phrase, "our_pages", market, evidence_id=row.id))

        # The engine's own phrasing, off the result pages node 1.3.1 bought. A
        # related search is already a search phrase and is seeded whole; a
        # People Also Ask entry is a sentence, so it goes through the same
        # window extraction a page title does.
        for row in found.of(SERP_RELATED):
            for term in (row.payload.get("related") or [])[:50]:
                seeds.append(keywords.Seed(str(term), "serp_related", market, evidence_id=row.id))

        for row in found.of(SERP_QUESTION):
            for question in (row.payload.get("questions") or [])[:20]:
                for phrase in keywords.phrases(str(question), limit=4):
                    seeds.append(
                        keywords.Seed(phrase, "serp_questions", market, evidence_id=row.id)
                    )

        for ad in (ctx.output_of("1.3.2").get("ads") or [])[:400]:
            if not isinstance(ad, dict):
                continue
            text = " ".join(str(ad.get(field) or "") for field in ("headline", "offer", "angle"))
            for phrase in keywords.phrases(text, limit=8):
                seeds.append(keywords.Seed(phrase, "competitor_ads", market))

        topics = await self._topics(ctx, found)
        seeds.extend(keywords.Seed(topic, "model_hypothesis", market) for topic in topics)

        rows, dropped = keywords.assemble(seeds)
        if not rows:
            return KeywordUniverse(coverage=gather.coverage_notes(found))

        await ctx.progress(f"assembled {len(rows)} deduped keywords from {len(seeds)} candidates")
        return KeywordUniverse(
            keywords=[
                UniverseKeyword(
                    term=row.term,
                    source=list(row.sources),
                    market=list(row.markets),
                    evidence_ids=[str(item) for item in row.evidence_ids],
                )
                for row in rows
            ],
            total_terms=len(rows),
            source_counts=keywords.source_counts(rows),
            terms_omitted=dropped,
            coverage=gather.coverage_notes(found),
        )

    async def _topics(self, ctx: RunContext, found: Gathered) -> list[str]:
        """The model's guesses at how a buyer phrases this need."""
        result = await ctx.complete(
            SeedTopics,
            system=prompts.system_prompt(
                "You propose search phrases a buyer of this product might type. These are "
                "hypotheses to be priced against real search-volume data, not claims — so "
                "propose phrasings, never volumes, never demand."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block("what we sell (node 1.1.1)", ctx.output_of("1.1.1")),
                prompts.computed_block(
                    "themes our competitors advertise on (node 1.3.2)",
                    ctx.output_of("1.3.2").get("message_clusters", []),
                ),
                prompts.coverage_block(found),
                f"TASK\n"
                f"  Return up to {MAX_TOPICS} search phrases, two to six words each, that "
                f"someone with this problem would plausibly type. Cover the whole funnel: "
                f"the problem, the category, the product, and comparisons. Do not include "
                f"our brand name on its own.",
            ),
        )
        return [term for term in result.topics[:MAX_TOPICS] if keywords.normalise(term)]


# ---------------------------------------------------------------------------
# 1.4.2 — intent_classification
# ---------------------------------------------------------------------------


class TermIntent(BaseModel):
    """One term, labelled."""

    term: str = Field(description="Copy the term exactly as given.")
    intent: keywords.Intent
    funnel_stage: keywords.FunnelStage = "none"
    confidence: float = Field(default=0.5, ge=0, le=1)
    note: str = Field(default="", description="Only when the label is not obvious.")


class TermIntents(BaseModel):
    items: list[TermIntent] = Field(default_factory=list)


class ClassifiedTerm(BaseModel):
    term: str
    intent: keywords.Intent
    funnel_stage: keywords.FunnelStage = "none"
    confidence: float = 0.5
    note: str = ""
    cluster: str = ""


class ClusterSummary(BaseModel):
    """A theme, its size and what its terms want. Counted, never estimated."""

    key: str
    size: int
    intents: dict[str, int] = Field(default_factory=dict)
    sample_terms: list[str] = Field(default_factory=list)


class IntentClassification(BaseModel):
    """1.4.2 output."""

    classified: list[ClassifiedTerm] = Field(default_factory=list)
    clusters: list[ClusterSummary] = Field(default_factory=list)
    by_intent: dict[str, int] = Field(default_factory=dict)
    unclassified: list[str] = Field(default_factory=list)
    unclassified_count: int = 0
    batches: int = 0
    failed_batches: int = 0
    coverage: list[str] = Field(default_factory=list)


class IntentClassificationNode(LLMNode):
    """1.4.2 — what each phrase wants, in batches of a hundred.

    The cluster key is stamped onto every classified row rather than kept as a
    membership list on the cluster: node 1.4.5 needs full membership to map a
    theme to a page, and carrying 2,000 terms twice in one output document to
    achieve that would be the wrong trade.
    """

    spec = NodeSpec(
        id="1.4.2",
        name="intent_classification",
        stage="1.4",
        depends_on=("1.4.1",),
        task_class=TaskClass.CLASSIFY,
        input_model=KeywordUniverse,
        output_model=IntentClassification,
    )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        terms = [
            str(row.get("term"))
            for row in (ctx.output_of("1.4.1").get("keywords") or [])
            if isinstance(row, dict) and row.get("term")
        ]
        if not terms:
            return IntentClassification(coverage=["keyword_universe: empty"])

        outcome = await batching.in_batches(
            ctx,
            terms,
            output_model=TermIntents,
            system=prompts.system_prompt(
                "You label search queries by what the person typing them wants. Work only "
                "from the words in the query and the business context given. A query you "
                "cannot place is `informational` with a low confidence — never a guess at "
                "`transactional` because the product is expensive."
            ),
            user_for=lambda batch, index, total: prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    f"terms to label — part {index + 1} of {total} (label these and no others)",
                    list(batch),
                ),
                "TASK\n"
                "  Return one entry per term, copying `term` exactly. `intent` is one of "
                "transactional (ready to buy), commercial_investigation (comparing), "
                "informational (learning), navigational (looking for a named company) or "
                "irrelevant (not a buyer of this product at all). Give the funnel stage and "
                "how confident you are. Add a note only where the label is arguable.",
            ),
            size=TERMS_PER_CALL,
            label="intent classification",
        )

        labelled = batching.index_by(
            [item for result in outcome.results for item in result.items], "term"
        )
        # The model is asked to copy terms exactly and mostly does; normalising
        # here catches the case where it did not, rather than reporting a term
        # as unclassified because of a capital letter.
        by_term = {keywords.normalise(key): value for key, value in labelled.items()}

        clusters = keywords.cluster_terms(terms)
        cluster_of = {term: cluster.key for cluster in clusters for term in cluster.terms}

        classified: list[ClassifiedTerm] = []
        unclassified: list[str] = []
        for term in terms:
            normalised = keywords.normalise(term)
            found = by_term.get(normalised)
            if found is None:
                unclassified.append(term)
                continue
            classified.append(
                ClassifiedTerm(
                    term=normalised,
                    intent=found.intent,
                    funnel_stage=found.funnel_stage,
                    confidence=found.confidence,
                    note=found.note,
                    cluster=cluster_of.get(normalised, ""),
                )
            )

        coverage: list[str] = []
        if outcome.degraded:
            coverage.append(outcome.note("intent classification"))
        if unclassified:
            coverage.append(f"{len(unclassified)} terms came back unlabelled")

        return IntentClassification(
            classified=classified,
            clusters=_cluster_summaries(clusters, classified),
            by_intent=_count(row.intent for row in classified),
            unclassified=unclassified[:MAX_UNPRICED_LISTED],
            unclassified_count=len(unclassified),
            batches=outcome.batches,
            failed_batches=outcome.failed,
            coverage=coverage,
        )


# ---------------------------------------------------------------------------
# 1.4.3 — demand_metrics
# ---------------------------------------------------------------------------


class DemandMetric(BaseModel):
    term: str
    volume: int = 0
    cpc_low: float | None = None
    cpc_high: float | None = None
    competition: str | None = None
    competition_index: float | None = None
    seasonality_index: list[float] = Field(
        default_factory=list,
        description="12 values, January first, as a percent of the mean month. Empty when "
        "the vendor returned no monthly history — which is not the same as flat demand.",
    )
    trend_yoy: float | None = None
    months_observed: int = 0
    evidence_ids: list[str] = Field(default_factory=list)


class DemandTotals(BaseModel):
    terms_priced: int = 0
    terms_unpriced: int = 0
    total_monthly_volume: int = 0
    with_volume: int = 0
    with_seasonality: int = 0


class DemandMetrics(BaseModel):
    """1.4.3 output."""

    metrics: list[DemandMetric] = Field(default_factory=list)
    unpriced: list[str] = Field(default_factory=list)
    totals: DemandTotals = Field(default_factory=DemandTotals)
    coverage: list[str] = Field(default_factory=list)


class DemandMetricsNode(LLMNode):
    """1.4.3 — volume, price and seasonality, joined rather than written.

    No model is called. PRD §10 says these numbers are "joined from DataForSEO,
    not generated", and the shortest way to guarantee that is for this node to
    have no door to a model open at all.
    """

    spec = NodeSpec(
        id="1.4.3",
        name="demand_metrics",
        stage="1.4",
        depends_on=("1.4.1",),
        task_class=TaskClass.EXTRACT,
        input_model=KeywordUniverse,
        output_model=DemandMetrics,
        connectors=("dataforseo",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        terms = _universe_terms(ctx)
        found = await gather.collect(
            ctx,
            gather.Need(
                KEYWORD_METRICS,
                connector="dataforseo",
                params={"domain": ctx.project.domain, "volume_for": terms},
                limit=keywords.MAX_TERMS,
                # A second, differently-parameterised pull of the same kind: the
                # universe holds terms the vendor's own idea list never
                # contained, and they have to be priced too.
                pull_key="dataforseo:volume",
                refresh=True,
            ),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        terms = _universe_terms(ctx)
        rows, unpriced = keywords.join_demand(
            terms,
            found.payloads(KEYWORD_METRICS),
            [row.id for row in found.of(KEYWORD_METRICS)],
        )
        await ctx.progress(f"priced {len(rows)} of {len(terms)} keywords")
        return DemandMetrics(
            metrics=[
                DemandMetric(
                    term=row.term,
                    volume=row.volume,
                    cpc_low=row.cpc_low,
                    cpc_high=row.cpc_high,
                    competition=row.competition,
                    competition_index=row.competition_index,
                    seasonality_index=list(row.seasonality_index),
                    trend_yoy=row.trend_yoy,
                    months_observed=row.months_observed,
                    evidence_ids=[str(item) for item in row.evidence_ids],
                )
                for row in rows
            ],
            unpriced=unpriced[:MAX_UNPRICED_LISTED],
            totals=DemandTotals(
                terms_priced=len(rows),
                terms_unpriced=len(unpriced),
                total_monthly_volume=sum(row.volume for row in rows),
                with_volume=sum(1 for row in rows if row.volume > 0),
                with_seasonality=sum(1 for row in rows if row.seasonality_index),
            ),
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.4.4 — negative_blocklist
# ---------------------------------------------------------------------------


class NegativeKeyword(BaseModel):
    term: str
    match_type: keywords.MatchType
    reason: str = ""
    source: keywords.NegativeSource


class NegativeBlocklist(BaseModel):
    """1.4.4 output."""

    negatives: list[NegativeKeyword] = Field(default_factory=list)
    withheld: list[str] = Field(
        default_factory=list,
        description=(
            "Terms another source wanted blocked that node 1.2.2 measured converting. "
            "Not blocked, and surfaced for a human — this collision is a finding."
        ),
    )
    counts_by_source: dict[str, int] = Field(default_factory=dict)
    coverage: list[str] = Field(default_factory=list)


class NegativeBlocklistNode(LLMNode):
    """1.4.4 — what not to pay for, merged from three sources that already know.

    No model is called: every field of every row is already carried by the node
    that produced it. What this node adds is the merge order and the profitable-
    term guard, and both belong in code where they can be tested.
    """

    spec = NodeSpec(
        id="1.4.4",
        name="negative_blocklist",
        stage="1.4",
        depends_on=("1.4.2", "1.1.3", "1.2.2"),
        task_class=TaskClass.CLASSIFY,
        input_model=IntentClassification,
        output_model=NegativeBlocklist,
    )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        pnl = ctx.output_of("1.2.2")
        negative_icp = ctx.output_of("1.1.3")
        classification = ctx.output_of("1.4.2")

        lost: list[tuple[str, str]] = []
        for exclusion in negative_icp.get("exclusions") or []:
            if not isinstance(exclusion, dict):
                continue
            reason = str(exclusion.get("disqualifier") or exclusion.get("persona") or "")
            for term in exclusion.get("suggested_negative_terms") or []:
                lost.append((str(term), reason))

        irrelevant = [
            (
                str(row.get("term")),
                str(row.get("note") or "") or "Classified as not a buyer of this product.",
            )
            for row in classification.get("classified") or []
            if isinstance(row, dict) and row.get("intent") == "irrelevant"
        ]

        rows, withheld = keywords.blocklist(
            lost_reason_terms=lost,
            wasteful=[row for row in (pnl.get("wasteful_terms") or []) if isinstance(row, dict)],
            irrelevant=irrelevant,
            protect=[
                str(row.get("term"))
                for row in (pnl.get("profitable_terms") or [])
                if isinstance(row, dict) and row.get("term")
            ],
        )
        coverage: list[str] = []
        if withheld:
            coverage.append(
                f"{len(withheld)} term(s) were not blocked because 1.2.2 measured them converting"
            )
        return NegativeBlocklist(
            negatives=[
                NegativeKeyword(
                    term=row.term,
                    match_type=row.match_type,
                    reason=row.reason,
                    source=row.source,
                )
                for row in rows
            ],
            withheld=withheld,
            counts_by_source=_count(row.source for row in rows),
            coverage=coverage,
        )


# ---------------------------------------------------------------------------
# 1.4.5 — keyword_to_page_map
# ---------------------------------------------------------------------------


class ContentGap(BaseModel):
    cluster: str
    required_page_type: str = Field(
        description="What would have to exist: comparison page, pricing page, guide, glossary…"
    )
    why: str = ""
    priority: Literal["high", "medium", "low"] = "medium"


class ContentGaps(BaseModel):
    gaps: list[ContentGap] = Field(default_factory=list)


class ClusterMapping(BaseModel):
    term_cluster: str
    best_url: str | None = None
    relevance_score: float = 0.0
    verdict: keywords.Verdict = "gap"
    runner_up_url: str | None = None
    matched_tokens: list[str] = Field(default_factory=list)
    missing_tokens: list[str] = Field(default_factory=list)
    cluster_size: int = 0
    monthly_volume: int = 0
    evidence_ids: list[str] = Field(default_factory=list)


class KeywordToPageMap(BaseModel):
    """1.4.5 output."""

    mapping: list[ClusterMapping] = Field(default_factory=list)
    content_gaps: list[ContentGap] = Field(default_factory=list)
    pages_considered: int = 0
    coverage: list[str] = Field(default_factory=list)


class KeywordToPageMapNode(LLMNode):
    """1.4.5 — which of our pages answers which theme, and what is missing.

    The relevance score is weighted token coverage computed in `keywords.py`,
    and `matched_tokens`/`missing_tokens` travel with it so the number can be
    checked by reading rather than believed. The verdict is a threshold on that
    score, not an opinion. What the model is asked is the one thing left: when
    there is no page, what kind of page would there have to be.
    """

    spec = NodeSpec(
        id="1.4.5",
        name="keyword_to_page_map",
        stage="1.4",
        depends_on=("1.4.2", "1.4.3"),
        task_class=TaskClass.SYNTHESIZE,
        input_model=IntentClassification,
        output_model=KeywordToPageMap,
        connectors=("web_crawler",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx,
            gather.Need(
                PAGE,
                connector="web_crawler",
                params={"domain": ctx.project.domain},
                limit=500,
            ),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        pages = found.of(PAGE)
        clusters = _clusters_from(ctx.output_of("1.4.2"))
        if not clusters:
            return KeywordToPageMap(coverage=gather.coverage_notes(found))

        volumes = _volume_by_term(ctx.output_of("1.4.3"))
        matches = keywords.map_clusters_to_pages(
            clusters, [dict(row.payload) for row in pages], [row.id for row in pages]
        )
        by_cluster = {cluster.key: cluster for cluster in clusters}
        mapping = [
            ClusterMapping(
                term_cluster=match.cluster,
                best_url=match.best_url,
                relevance_score=match.relevance_score,
                verdict=match.verdict,
                runner_up_url=match.runner_up_url,
                matched_tokens=list(match.matched_tokens),
                missing_tokens=list(match.missing_tokens),
                cluster_size=match.cluster_size,
                monthly_volume=sum(
                    volumes.get(term, 0) for term in by_cluster[match.cluster].terms
                ),
                evidence_ids=[str(item) for item in match.evidence_ids],
            )
            for match in matches
        ]
        mapping.sort(key=lambda row: (-row.monthly_volume, row.term_cluster))

        unanswered = [row for row in mapping if row.verdict != "good_fit"]
        gaps: list[ContentGap] = []
        if unanswered:
            result = await ctx.complete(
                ContentGaps,
                system=prompts.system_prompt(
                    "You say what page a business would need in order to answer a group of "
                    "search queries it currently has no good page for. Name a page type and "
                    "why, and nothing else — you do not write the page and you do not "
                    "restate the scores."
                ),
                user=prompts.compose(
                    prompts.project_block(ctx.project),
                    prompts.computed_block(
                        "themes with no good page (final — `relevance_score` and "
                        "`monthly_volume` are measured)",
                        [
                            {
                                "cluster": row.term_cluster,
                                "verdict": row.verdict,
                                "relevance_score": row.relevance_score,
                                "monthly_volume": row.monthly_volume,
                                "cluster_size": row.cluster_size,
                                "closest_page": row.best_url,
                                "missing_tokens": row.missing_tokens,
                                "example_terms": list(by_cluster[row.term_cluster].terms[:8]),
                            }
                            for row in unanswered[:40]
                        ],
                    ),
                    prompts.coverage_block(found),
                    "TASK\n"
                    "  Return one gap per theme, copying `cluster` exactly: the kind of page "
                    "that would answer it, why that kind, and how urgent it is relative to "
                    "the other themes here.",
                ),
            )
            known = {row.term_cluster for row in unanswered}
            gaps = [gap for gap in result.gaps if gap.cluster in known]

        return KeywordToPageMap(
            mapping=mapping,
            content_gaps=gaps,
            pages_considered=len(pages),
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# helpers, and the module-level instances the registry discovers
# ---------------------------------------------------------------------------


def _market(ctx: RunContext) -> str:
    for market in ctx.project.markets or []:
        if isinstance(market, dict) and market.get("country"):
            return str(market["country"])
    return ""


def _vendor_seeds(ctx: RunContext, found: Gathered) -> list[str]:
    """The phrases handed to the vendor's idea endpoint, best evidence first."""
    ranked: list[str] = []
    for row in (ctx.output_of("1.2.2").get("profitable_terms") or [])[:VENDOR_SEEDS]:
        if isinstance(row, dict) and row.get("term"):
            ranked.append(str(row["term"]))
    for row in found.of(SEARCH_TERM_PNL):
        if len(ranked) >= VENDOR_SEEDS:
            break
        term = row.payload.get("search_term")
        if term and str(term) not in ranked:
            ranked.append(str(term))
    if not ranked:
        ranked = [ctx.project.name]
    return ranked[:VENDOR_SEEDS]


def _universe_terms(ctx: RunContext) -> list[str]:
    return [
        str(row.get("term"))
        for row in (ctx.output_of("1.4.1").get("keywords") or [])
        if isinstance(row, dict) and row.get("term")
    ]


def _clusters_from(classification: dict[str, Any]) -> list[keywords.Cluster]:
    """Rebuild cluster membership from the classified rows.

    1.4.2 stamps the cluster key on every term rather than repeating the term
    lists on the clusters, so this is where membership comes back together.
    """
    grouped: dict[str, list[str]] = {}
    for row in classification.get("classified") or []:
        if not isinstance(row, dict):
            continue
        key = str(row.get("cluster") or "")
        term = str(row.get("term") or "")
        if key and term:
            grouped.setdefault(key, []).append(term)
    return [
        keywords.Cluster(key=key, terms=tuple(sorted(terms)))
        for key, terms in sorted(grouped.items())
    ]


def _volume_by_term(metrics: dict[str, Any]) -> dict[str, int]:
    found: dict[str, int] = {}
    for row in metrics.get("metrics") or []:
        if isinstance(row, dict) and row.get("term"):
            found[keywords.normalise(row["term"])] = int(row.get("volume") or 0)
    return found


def _cluster_summaries(
    clusters: Sequence[keywords.Cluster], classified: Sequence[ClassifiedTerm]
) -> list[ClusterSummary]:
    intents: dict[str, list[str]] = {}
    for row in classified:
        if row.cluster:
            intents.setdefault(row.cluster, []).append(row.intent)
    return [
        ClusterSummary(
            key=cluster.key,
            size=len(cluster.terms),
            intents=_count(intents.get(cluster.key, [])),
            sample_terms=list(cluster.terms[:10]),
        )
        for cluster in clusters
    ]


def _count(values: Any) -> dict[str, int]:
    counts: dict[str, int] = {}
    for value in values:
        counts[str(value)] = counts.get(str(value), 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


keyword_universe = KeywordUniverseNode()
intent_classification = IntentClassificationNode()
demand_metrics = DemandMetricsNode()
negative_blocklist = NegativeBlocklistNode()
keyword_to_page_map = KeywordToPageMapNode()
