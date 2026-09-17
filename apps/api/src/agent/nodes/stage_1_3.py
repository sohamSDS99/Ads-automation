"""Stage 1.3 — study the competition (PRD §10).

Four nodes. Three run unattended; `1.3.4 differentiation_claim` is a **gate** —
what we are going to claim in market is a marketing decision with legal
consequences, and PRD §16 has no auto-approve.

The shape is the same as stage 1.1: `creatives.py` computes, the model labels,
`reason()` merges. What is new here is volume. A creative corpus is 100–300 ads,
which is past what one prompt can hold and well past what one prompt can hold
*well*, so 1.3.2 reads it in batches of 25 through `batching.py` and reports how
many batches came back.

**Two deviations from PRD §10, both argued in the PR:**

1. `overlap_basis` keeps `auction` in its vocabulary but nothing emits it. The
   Google Ads API has no auction-insights resource — it is a UI-only report —
   so a node claiming an auction basis would be claiming a source it does not
   have. `paid_keywords` and `serp` are what the evidence actually supports.
2. `1.3.3` declares `depends_on=("1.3.2", "1.3.1")` where the PRD's edge list
   has only `{1.3.2}`. It changes no execution order — 1.3.1 already precedes
   1.3.2 — and makes the data dependency real rather than relying on a
   transitive one, which is what `ctx.output_of` asks for.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Literal

import structlog
from pydantic import BaseModel, Field

from agent.db.models import ApprovalRequiredRole, Evidence
from agent.llm.router import TaskClass
from agent.nodes import batching, creatives, gather, prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.nodes.stage_1_1 import PAGE

log = structlog.get_logger(__name__)

COMPETITOR_CREATIVE = "competitor_creative"
COMPETITOR_LANDING_PAGE = "competitor_landing_page"
DOMAIN_COMPETITOR = "domain_competitor"
SERP_SNAPSHOT = "serp_snapshot"
#: Ads read off a live result page by `connectors/serp.py`. A separate kind from
#: `competitor_creative` rather than the same one, because the two are different
#: claims — the archive says an advertiser has run this ad, the SERP says it was
#: on the page for one of our money terms on one named day — and because a
#: shared kind would make the store non-empty and skip the Transparency scrape.
SERP_AD = "serp_ad"
#: People Also Ask and the related-searches strip, both off the same page. Node
#: 1.4.1 seeds its keyword universe on them — demand phrased by the engine
#: rather than by a vendor's idea list or by the model.
SERP_QUESTION = "serp_question"
SERP_RELATED = "serp_related"

#: Our own money terms handed to the SERP probe. The vendor charges per SERP, so
#: this is the top of node 1.2.2's table rather than all of it.
SERP_PROBE_TERMS = 15

#: Evidence rows of competitor creative a node will read. Bounded so a project
#: that has scraped twenty advertisers over ten runs cannot load its whole
#: history into one prompt-building pass.
MAX_CORPUS_ROWS = 2_000

#: Ads reaching the output when a project does not override it in `settings`.
DEFAULT_MAX_ADS = 300

#: Ads per extraction call. Smaller than `batching.BATCH_SIZE` on purpose — an
#: ad is a paragraph of copy to read, not a two-word term to label, and 100 of
#: them in one prompt is where a model starts summarising instead of extracting.
ADS_PER_CALL = 25

#: What an ad offers as evidence for its claim. A fixed vocabulary, because the
#: report groups on it and "social proof-ish" is not a group.
ProofType = Literal[
    "customer_logo",
    "statistic",
    "certification",
    "testimonial",
    "guarantee",
    "free_trial",
    "pricing",
    "award",
    "none",
]


# ---------------------------------------------------------------------------
# 1.3.1 — competitor_set
# ---------------------------------------------------------------------------


class CompetitorLabel(BaseModel):
    """The model's read of one computed competitor row, keyed back to it."""

    domain: str = Field(description="Copy the `domain` exactly as shown.")
    name: str = Field(default="", description="The company's trading name.")
    positioning: str = Field(default="", description="One sentence on how they sell.")
    threat: Literal["direct", "adjacent", "aggregator", "irrelevant"] = "direct"


class CompetitorLabels(BaseModel):
    competitors: list[CompetitorLabel] = Field(default_factory=list)


class Competitor(BaseModel):
    """1.3.1's competitor: the model's words, `creatives.py`'s numbers."""

    domain: str
    name: str = ""
    positioning: str = ""
    threat: Literal["direct", "adjacent", "aggregator", "irrelevant"] = "direct"
    overlap_score: float
    overlap_basis: list[str] = Field(default_factory=list)
    paid_keyword_overlap: int = 0
    paid_keyword_count: int = 0
    est_paid_traffic_cost: float = 0.0
    avg_position: float | None = None
    serp_hits: int = 0
    serp_terms: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class CompetitorSet(BaseModel):
    """1.3.1 output."""

    competitors: list[Competitor] = Field(default_factory=list)
    serp_terms_checked: int = Field(
        default=0, description="The denominator behind `serp_hits`. Without it a hit count lies."
    )
    competitors_omitted: int = 0
    coverage: list[str] = Field(default_factory=list)


class CompetitorSetNode(LLMNode):
    """1.3.1 — who else is buying the demand we want."""

    spec = NodeSpec(
        id="1.3.1",
        name="competitor_set",
        stage="1.3",
        depends_on=("1.1.2", "1.2.2"),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=CompetitorSet,
        connectors=("dataforseo", "serp"),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # The SERP probe is aimed at the terms our own money already proved
        # matter, so the pull is scoped by node 1.2.2's table rather than
        # sampling the vendor's idea of our market.
        terms = _our_terms(ctx)
        probe = sorted(terms)[:SERP_PROBE_TERMS]
        found = await gather.collect(
            ctx,
            gather.Need(
                DOMAIN_COMPETITOR,
                connector="dataforseo",
                params={"domain": ctx.project.domain, "serp_keywords": probe},
            ),
            # Two sources can answer this, and the order is the preference.
            # `collect` pulls only when the store came back empty, so a
            # DataForSEO fetch that already wrote its own snapshots above ends
            # this need without spending a proxy request — and a workspace with
            # no keyword vendor still gets a SERP, a live one, with the ads on
            # it that `serp_ad` carries into node 1.3.2.
            gather.Need(
                SERP_SNAPSHOT,
                connector="serp",
                params={
                    "serp_keywords": probe,
                    "country": _country(ctx),
                    "language": _language(ctx),
                },
            ),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        rows, omitted, checked = creatives.competitor_rows(
            domain_rows=found.payloads(DOMAIN_COMPETITOR),
            domain_ids=[row.id for row in found.of(DOMAIN_COMPETITOR)],
            serp_rows=found.payloads(SERP_SNAPSHOT),
            serp_ids=[row.id for row in found.of(SERP_SNAPSHOT)],
            our_domain=ctx.project.domain,
            our_terms=_our_terms(ctx),
        )
        if not rows:
            return CompetitorSet(serp_terms_checked=checked, coverage=gather.coverage_notes(found))

        labels = await ctx.complete(
            CompetitorLabels,
            system=prompts.system_prompt(
                "You identify the advertisers a business competes with for paid clicks. "
                "The overlap has been measured for you; say who each domain is and how "
                "they sell, and be honest when one is an aggregator or a directory rather "
                "than a real competitor."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "who already buys from us (node 1.1.2)", ctx.output_of("1.1.2")
                ),
                prompts.computed_block(
                    "measured overlap (final — `overlap_score` ranks these domains against "
                    "each other, it is not a percentage of anything)",
                    [row.as_dict() for row in rows],
                ),
                prompts.coverage_block(found),
                "TASK\n"
                "  Return one entry per domain above, copying `domain` exactly. Give the "
                "company's name, one sentence on how they position themselves, and whether "
                "they are a direct competitor, an adjacent one, an aggregator or "
                "irrelevant to us. Do not restate any number.",
            ),
        )
        by_domain = {item.domain: item for item in labels.competitors}
        return CompetitorSet(
            competitors=[
                Competitor(
                    domain=row.domain,
                    name=_attr(by_domain, row.domain, "name"),
                    positioning=_attr(by_domain, row.domain, "positioning"),
                    threat=(by_domain[row.domain].threat if row.domain in by_domain else "direct"),
                    overlap_score=row.overlap_score,
                    overlap_basis=list(row.overlap_basis),
                    paid_keyword_overlap=row.paid_keyword_overlap,
                    paid_keyword_count=row.paid_keyword_count,
                    est_paid_traffic_cost=row.est_paid_traffic_cost,
                    avg_position=row.avg_position,
                    serp_hits=row.serp_hits,
                    serp_terms=list(row.serp_terms[:8]),
                    evidence_ids=[str(item) for item in row.evidence_ids],
                )
                for row in rows
            ],
            serp_terms_checked=checked,
            competitors_omitted=omitted,
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.3.2 — creative_corpus
# ---------------------------------------------------------------------------


class ExtractedAd(BaseModel):
    """One ad, read. The model's whole contribution to 1.3.2."""

    key: str = Field(description="Copy the `key` of the ad this describes.")
    headline: str = Field(default="", description="The ad's main line, verbatim where possible.")
    description: str = Field(default="", description="The supporting line.")
    offer: str = Field(
        default="", description="What they are actually giving — trial, demo, price."
    )
    angle: str = Field(default="", description="The argument it makes, in a few words.")
    proof_type: ProofType = "none"
    cta: str = Field(default="", description="The call to action, verbatim.")
    theme: str = Field(
        default="",
        description=(
            "A short theme name shared with every other ad making the same argument. "
            "Reuse a theme you have already used rather than inventing a near-duplicate."
        ),
    )


class ExtractedAds(BaseModel):
    ads: list[ExtractedAd] = Field(default_factory=list)


class CorpusAd(BaseModel):
    """1.3.2's ad: scraped facts plus the model's reading of the copy."""

    advertiser: str
    headline: str = ""
    description: str = ""
    offer: str = ""
    angle: str = ""
    proof_type: ProofType = "none"
    cta: str = ""
    landing_url: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    #: The stored key of the Transparency Center grid this ad was captured from,
    #: not a crop of the ad itself. Streamed through the file server in P5.
    screenshot_path: str | None = None
    format: str = "text"
    theme: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class MessageCluster(BaseModel):
    theme: str
    frequency: int
    share_pct: float = 0.0
    advertisers: list[str] = Field(default_factory=list)


class CreativeCorpus(BaseModel):
    """1.3.2 output."""

    ads: list[CorpusAd] = Field(default_factory=list)
    message_clusters: list[MessageCluster] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    ads_omitted: int = 0
    unread_ads: int = Field(
        default=0, description="Scraped but not extracted, because their batch failed."
    )
    coverage: list[str] = Field(default_factory=list)


class CreativeCorpusNode(LLMNode):
    """1.3.2 — every live competitor ad we can see, read and grouped.

    Two captures feed it. The Transparency Center scrape says what an advertiser
    has run; the SERP rows node 1.3.1 already bought say what was actually on
    the page for our own money terms, which is the half that can be missing
    entirely when a competitor advertises only on terms we never thought to
    search the archive for.

    The scrape is the connector's job and the screenshot lands on the Volume
    through `StorageBackend`; this node reads what came back. Extraction is
    batched because the corpus is hundreds of ads, and the batch count is
    reported so a partial read cannot be mistaken for a complete one.
    """

    spec = NodeSpec(
        id="1.3.2",
        name="creative_corpus",
        stage="1.3",
        depends_on=("1.3.1",),
        task_class=TaskClass.EXTRACT,
        input_model=CompetitorSet,
        output_model=CreativeCorpus,
        connectors=("transparency",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        competitors = ctx.output_of("1.3.1").get("competitors") or []
        advertisers = [
            str(item.get("name") or item.get("domain"))
            for item in competitors
            if isinstance(item, dict) and item.get("threat") != "irrelevant"
        ]
        found = await gather.collect(
            ctx,
            gather.Need(
                COMPETITOR_CREATIVE,
                connector="transparency",
                params={"advertisers": advertisers, "landing_pages": True},
                limit=MAX_CORPUS_ROWS,
            ),
            gather.Need(COMPETITOR_LANDING_PAGE, limit=200),
            # Written by node 1.3.1's SERP pull, so there is no connector here:
            # asking the proxy again would buy the same pages twice.
            gather.Need(SERP_AD, limit=MAX_CORPUS_ROWS),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        # One corpus out of two captures. `creative_rows` keys on the creative's
        # own identity, so an ad the archive and the SERP both hold collapses to
        # one row rather than being read, and counted, twice.
        scraped = found.of(COMPETITOR_CREATIVE) + found.of(SERP_AD)
        rows, omitted = creatives.creative_rows(
            [dict(row.payload) for row in scraped],
            [row.id for row in scraped],
            max_ads=_max_ads(ctx),
        )
        if not rows:
            return CreativeCorpus(coverage=gather.coverage_notes(found))

        stats = creatives.corpus_stats(rows)
        await ctx.progress(
            f"reading {len(rows)} competitor ads from {stats['advertisers']} advertisers"
        )

        outcome = await batching.in_batches(
            ctx,
            rows,
            output_model=ExtractedAds,
            system=prompts.system_prompt(
                "You read competitor search ads and extract what each one says. Quote the "
                "ad's own words for the headline, the description and the call to action. "
                "Never write copy of your own, and never describe an ad that is not in the "
                "batch in front of you."
            ),
            user_for=lambda batch, index, total: prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    f"ads to read — part {index + 1} of {total} (these and no others)",
                    [row.as_dict() for row in batch],
                ),
                "TASK\n"
                "  Return one entry per ad, copying `key` exactly. Split its text into a "
                "headline, a description, the offer it makes and its call to action. Name "
                "the angle it argues, pick the kind of proof it leans on, and give it a "
                "short theme shared with every other ad making the same argument.",
            ),
            size=ADS_PER_CALL,
            label="creative extraction",
        )

        extracted = batching.index_by(
            [ad for result in outcome.results for ad in result.ads], "key"
        )
        themes = {key: ad.theme for key, ad in extracted.items() if ad.theme}
        clusters = creatives.cluster_frequency(themes, rows)

        coverage = gather.coverage_notes(found)
        if outcome.degraded:
            coverage.append(outcome.note("creative extraction"))

        return CreativeCorpus(
            ads=[_corpus_ad(row, extracted.get(row.key)) for row in rows],
            message_clusters=[
                MessageCluster(
                    theme=str(item["theme"]),
                    frequency=int(item["frequency"]),
                    share_pct=float(item["share_pct"]),
                    advertisers=[str(name) for name in item["advertisers"]],
                )
                for item in clusters
            ],
            stats=stats,
            ads_omitted=omitted,
            unread_ads=sum(1 for row in rows if row.key not in extracted),
            coverage=coverage,
        )


def _corpus_ad(row: creatives.CreativeRow, read: ExtractedAd | None) -> CorpusAd:
    """Merge one scraped ad with the model's reading of it, if there was one.

    An ad whose batch failed still reaches the output — with its scraped facts
    and its raw text as the headline — rather than disappearing. `unread_ads`
    counts them.
    """
    return CorpusAd(
        advertiser=row.advertiser,
        headline=read.headline if read else row.creative_text[:200],
        description=read.description if read else "",
        offer=read.offer if read else "",
        angle=read.angle if read else "",
        proof_type=read.proof_type if read else "none",
        cta=read.cta if read else "",
        landing_url=row.landing_url,
        first_seen=row.first_shown,
        last_seen=row.last_shown,
        screenshot_path=row.screenshot_path,
        format=row.format,
        theme=read.theme if read else "",
        evidence_ids=[str(row.evidence_id)],
    )


# ---------------------------------------------------------------------------
# 1.3.3 — spend_estimation
# ---------------------------------------------------------------------------


class EstimateNote(BaseModel):
    """The model's caveat on one computed range. It states no figure."""

    competitor: str = Field(description="Copy the `competitor` exactly.")
    caveat: str = Field(
        default="", description="What would make this estimate wrong, in one sentence."
    )
    how_to_verify: str = Field(default="", description="The cheapest way to check it.")


class EstimateNotes(BaseModel):
    notes: list[EstimateNote] = Field(default_factory=list)


class SpendEstimateOut(BaseModel):
    """1.3.3's estimate. `method` and `basis` travel with the number, always."""

    competitor: str
    est_monthly_spend_low: float | None = None
    est_monthly_spend_high: float | None = None
    currency: str = "USD"
    method: creatives.SpendMethod
    confidence: creatives.Confidence
    basis: dict[str, Any] = Field(default_factory=dict)
    peak_months: list[int] = Field(default_factory=list)
    creatives_seen: int = 0
    caveat: str = ""
    how_to_verify: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class SpendEstimation(BaseModel):
    """1.3.3 output."""

    estimates: list[SpendEstimateOut] = Field(default_factory=list)
    disclaimer: str = Field(
        default=(
            "Every range here is modelled from indirect signals and is not a measurement "
            "of anyone's spend. Read `method` and `basis` before quoting a figure."
        )
    )
    coverage: list[str] = Field(default_factory=list)


class SpendEstimationNode(LLMNode):
    """1.3.3 — what our competitors plausibly spend, and why that is a guess.

    PRD §10: "must state method; never present as fact". Every number is
    computed in `creatives.spend_estimates`, every method is one of a closed
    vocabulary, and the model is asked only for the caveat — so there is no path
    by which a model can produce a spend figure at all.
    """

    spec = NodeSpec(
        id="1.3.3",
        name="spend_estimation",
        stage="1.3",
        depends_on=("1.3.2", "1.3.1"),
        task_class=TaskClass.EXTRACT,
        input_model=CreativeCorpus,
        output_model=SpendEstimation,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx,
            gather.Need(DOMAIN_COMPETITOR),
            gather.Need(COMPETITOR_CREATIVE, limit=2_000),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        # No SERP rows are passed: this node needs each competitor's traffic-cost
        # signal, not a re-ranking. `overlap_score` is 1.3.1's answer and is not
        # recomputed here against a different denominator.
        rows, _, _ = creatives.competitor_rows(
            domain_rows=found.payloads(DOMAIN_COMPETITOR),
            domain_ids=[row.id for row in found.of(DOMAIN_COMPETITOR)],
            serp_rows=[],
            serp_ids=[],
            our_domain=ctx.project.domain,
        )
        corpus, _ = creatives.creative_rows(
            found.payloads(COMPETITOR_CREATIVE),
            [row.id for row in found.of(COMPETITOR_CREATIVE)],
            max_ads=MAX_CORPUS_ROWS,
        )
        estimates = creatives.spend_estimates(
            rows,
            corpus,
            # 1.3.1 is the only place that knows both spellings of a company:
            # the domain the keyword vendor returned and the name the
            # Transparency Center scrape was run under.
            aliases={
                str(item.get("name") or ""): str(item.get("domain") or "")
                for item in (ctx.output_of("1.3.1").get("competitors") or [])
                if isinstance(item, dict) and item.get("name") and item.get("domain")
            },
            currency=_currency(ctx),
        )
        if not estimates:
            return SpendEstimation(coverage=gather.coverage_notes(found))

        notes = await ctx.complete(
            EstimateNotes,
            system=prompts.system_prompt(
                "You annotate competitor spend estimates that have already been computed. "
                "You never state, adjust or round a spend figure — you say what would make "
                "the estimate wrong and how to check it cheaply."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "estimates (final — `method` and `basis` say how each was derived)",
                    [item.as_dict() for item in estimates],
                ),
                prompts.coverage_block(found),
                "TASK\n"
                "  Return one note per competitor, copying `competitor` exactly: the single "
                "assumption most likely to make its range wrong, and the cheapest way to "
                "check the real figure. Do not repeat or revise any number.",
            ),
        )
        by_name = {item.competitor: item for item in notes.notes}
        return SpendEstimation(
            estimates=[
                SpendEstimateOut(
                    competitor=item.competitor,
                    est_monthly_spend_low=item.est_monthly_spend_low,
                    est_monthly_spend_high=item.est_monthly_spend_high,
                    currency=item.currency,
                    method=item.method,
                    confidence=item.confidence,
                    basis=item.basis,
                    peak_months=list(item.peak_months),
                    creatives_seen=item.creatives,
                    caveat=_attr(by_name, item.competitor, "caveat"),
                    how_to_verify=_attr(by_name, item.competitor, "how_to_verify"),
                    evidence_ids=[str(value) for value in item.evidence_ids],
                )
                for item in estimates
            ],
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.3.4 ⛳ — differentiation_claim
# ---------------------------------------------------------------------------


class Whitespace(BaseModel):
    claim: str = Field(description="Something true about us that nobody in the corpus says.")
    why_unsaid: str = Field(default="", description="Why the competitors do not say it.")
    our_proof: str = Field(default="", description="What in the evidence supports it.")
    risk: str = Field(default="", description="What could go wrong if we say it.")
    evidence_ids: list[str] = Field(default_factory=list)


class DifferentiationClaim(BaseModel):
    """1.3.4 output — the proposal a marketing lead approves, edits or rejects."""

    whitespace: list[Whitespace] = Field(default_factory=list)
    recommended_claim: str = Field(default="", description="The one claim to build ads on.")
    recommended_rationale: str = ""
    substantiation_required: list[str] = Field(
        default_factory=list, description="What must be true, and provable, before we run it."
    )
    rejected_claims: list[str] = Field(
        default_factory=list,
        description="Claims considered and dropped for compliance or evidence reasons.",
    )
    confidence: float = Field(default=0.0, ge=0, le=1)
    reviewer_notes: str = Field(default="", description="What the approver should check first.")
    coverage: list[str] = Field(default_factory=list)


class DifferentiationClaimNode(LLMNode):
    """1.3.4 — the gate. What we say that nobody else is saying.

    It sees three things a claim has to survive: the competitor corpus (1.3.2),
    what we actually sell (1.1.1) and the compliance guardrails a human already
    approved (1.1.5). A claim the guardrails prohibit is not whitespace, it is a
    disapproval waiting to happen, and this is the node positioned to know that.
    """

    spec = NodeSpec(
        id="1.3.4",
        name="differentiation_claim",
        stage="1.3",
        depends_on=("1.3.2", "1.1.1", "1.1.5"),
        gate=True,
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.SYNTHESIZE,
        input_model=CreativeCorpus,
        output_model=DifferentiationClaim,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx,
            gather.Need(COMPETITOR_CREATIVE, limit=400),
            gather.Need(COMPETITOR_LANDING_PAGE, limit=60),
            gather.Need(PAGE, limit=60),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    def system_prompt(self, ctx: RunContext) -> str:
        return prompts.system_prompt(
            "You find the claim a business can make that its competitors are not making, "
            "for a marketing lead to approve. You are the proposal, not the decision. A "
            "claim must be true of this business from the evidence, must not be prohibited "
            "by the approved compliance guardrails, and must be substantiable — say plainly "
            "what proof it would need."
        )

    def user_prompt(self, ctx: RunContext, ev: Sequence[Evidence]) -> str:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        corpus = ctx.output_of("1.3.2")
        return prompts.compose(
            prompts.project_block(ctx.project),
            prompts.computed_block("what we sell (node 1.1.1)", ctx.output_of("1.1.1")),
            prompts.computed_block(
                "compliance guardrails, approved by legal (node 1.1.5) — a claim these "
                "prohibit may not be recommended",
                ctx.output_of("1.1.5"),
            ),
            prompts.computed_block(
                "what competitors are already saying (node 1.3.2)",
                {
                    "message_clusters": corpus.get("message_clusters", []),
                    "ads": [
                        {
                            "advertiser": ad.get("advertiser"),
                            "headline": ad.get("headline"),
                            "offer": ad.get("offer"),
                            "angle": ad.get("angle"),
                            "proof_type": ad.get("proof_type"),
                        }
                        for ad in (corpus.get("ads") or [])[:60]
                    ],
                },
            ),
            prompts.evidence_block(found, PAGE, title="our own pages", limit=25),
            prompts.evidence_block(
                found, COMPETITOR_LANDING_PAGE, title="competitor landing pages", limit=20
            ),
            prompts.coverage_block(found),
            prompts.cite_from("EVIDENCE — our own pages", "EVIDENCE — competitor landing pages"),
            "TASK\n"
            "  Return the whitespace: claims that are true of this business and absent from "
            "the competitor corpus, each with why nobody says it, our proof, and the risk of "
            "saying it. Recommend one claim to build on and say what must be substantiated "
            "before it runs. List claims you considered and dropped, and why. Set "
            "`confidence`, and tell the approver in `reviewer_notes` what to check first.",
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        result = await super().reason(ctx, ev)
        found: gather.Gathered = ctx.scratch[self.spec.id]
        return result.model_copy(update={"coverage": gather.coverage_notes(found)})


# ---------------------------------------------------------------------------
# helpers, and the module-level instances the registry discovers
# ---------------------------------------------------------------------------


def _our_terms(ctx: RunContext) -> set[str]:
    """Every search term node 1.2.2 priced, profitable or not."""
    pnl = ctx.output_of("1.2.2")
    terms: set[str] = set()
    for key in ("profitable_terms", "wasteful_terms"):
        for row in pnl.get(key) or []:
            if isinstance(row, dict) and row.get("term"):
                terms.add(str(row["term"]).strip().lower())
    return terms


def _max_ads(ctx: RunContext) -> int:
    """How many ads reach the output. A project may raise or lower it."""
    configured = ctx.project.settings.get("max_creatives")
    if configured is None:
        return DEFAULT_MAX_ADS
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        return DEFAULT_MAX_ADS


def _currency(ctx: RunContext) -> str:
    """The first configured market's currency, defaulting to USD."""
    for market in ctx.project.markets or []:
        if isinstance(market, dict) and market.get("currency"):
            return str(market["currency"]).upper()
    return "USD"


def _country(ctx: RunContext) -> str:
    """The first configured market's country, as the `gl` the SERP proxy wants.

    A project stores ISO 3166-1 alpha-2 already (PRD §6), so this is the code
    itself rather than a name that has to be looked up in a vendor's table.
    """
    for market in ctx.project.markets or []:
        if isinstance(market, dict) and market.get("country"):
            return str(market["country"]).lower()
    return ""


def _language(ctx: RunContext) -> str:
    """The first configured market's language, as `hl`. ISO 639-1, same as above."""
    for market in ctx.project.markets or []:
        if isinstance(market, dict) and market.get("language"):
            return str(market["language"]).lower()
    return ""


def _attr(labels: dict[str, Any], key: str, field_name: str) -> str:
    item = labels.get(key)
    return str(getattr(item, field_name, "")) if item is not None else ""


competitor_set = CompetitorSetNode()
creative_corpus = CreativeCorpusNode()
spend_estimation = SpendEstimationNode()
differentiation_claim = DifferentiationClaimNode()
