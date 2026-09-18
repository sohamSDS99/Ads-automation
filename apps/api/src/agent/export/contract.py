"""The `ResearchReport` contract (PRD §11) — the handoff artifact to Stage 02.

Two rules give this module its shape.

**The model does not write prose that matters.** PRD §11: "`markdown` is rendered
from this object by a deterministic Jinja2 template — the LLM does not write the
final markdown." Node 1.6.1 fills *this object*; `export/markdown.py` renders it.
That is what guarantees the five export formats can never disagree, because all
five are projections of one validated payload rather than five hand-written
documents.

**A statement without evidence is not a finding.** `Claim` requires at least one
`evidence_ids` entry, and the field is named `evidence_ids` on purpose: the
executor walks node output for that exact name (`nodes.base.EVIDENCE_FIELD`) and
fails the node when a claim cites evidence it never gathered. So the constraint
is enforced twice — by this schema for shape, by the executor for provenance.

The section models mirror the node outputs of PRD §10 stages 1.1–1.5. They are
deliberately **permissive about the interior** of each record and strict about
its identity: every list element carries the handful of fields the report and
its exports actually read, plus `extra="allow"` so a node that returns a richer
record than §10 sketches does not fail validation and lose the whole run at the
last node. Losing a field in an export is recoverable; losing a 45-minute run is
not.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

#: Bumped when this contract changes shape. Stage 02 reads it before parsing, and
#: `Report.schema_version` records the version each stored report was built under.
#: Typed as the literal, not as `str`, so `ResearchReport.schema_version` can
#: default to it without widening the field the whole contract is versioned by.
SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

#: PRD §11: the executive summary is capped at 250 words. Enforced, not suggested
#: — an unbounded summary is how a "report" becomes a wall of model output.
EXECUTIVE_SUMMARY_MAX_WORDS = 250

Confidence = Literal["high", "medium", "low"]
LaunchReadiness = Literal["go", "go_with_fixes", "no_go"]
Intent = Literal[
    "transactional",
    "commercial_investigation",
    "informational",
    "navigational",
    "irrelevant",
]
MappingVerdict = Literal["good_fit", "weak_fit", "gap"]


class ReportModel(BaseModel):
    """Base for every record in the report.

    `extra="allow"` is the load-bearing setting: see the module docstring. The
    renderers read named fields; anything extra rides along into the JSON export
    untouched, which is what Stage 02 wants anyway.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class CitedModel(ReportModel):
    """A record that carries the evidence behind it.

    The field has to be **declared**, not left to `extra="allow"`, and the
    difference is not cosmetic. An undeclared `evidence_ids` survives as a list
    of strings, `ResearchReport.evidence_ids()` collects only `UUID` instances,
    and the whole readiness and competitive half of the report loses its
    citations the moment the payload round-trips through JSON — which it does,
    on every read of the stored report. Declaring it makes the round trip
    lossless and the citation index complete.
    """

    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class StrictReportModel(BaseModel):
    """Base for the few records whose shape the exports depend on exactly.

    `priced_keyword_list` becomes a CSV that someone pastes into Google Ads
    Editor. An unexpected key there is a broken import, not a richer record.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ---------------------------------------------------------------------------
# Claims — the unit of evidence-backed assertion
# ---------------------------------------------------------------------------


class Claim(ReportModel):
    """An assertion the report is prepared to stand behind.

    PRD §11. `evidence_ids` has `min_length=1` because the alternative — an
    optional citation list — makes "uncited" the path of least resistance for
    both the model and every future caller.
    """

    statement: str = Field(min_length=1)
    evidence_ids: list[uuid.UUID] = Field(min_length=1)
    confidence: Confidence


# ---------------------------------------------------------------------------
# 1.1 — Business context
# ---------------------------------------------------------------------------


class Product(CitedModel):
    """1.1.1 `offer_economics`."""

    name: str
    price_model: str | None = None
    acv: float | None = Field(default=None, description="Annual contract value, account currency")
    gross_margin_pct: float | None = Field(default=None, ge=0, le=100)
    delivery_cost_notes: str | None = None


class IcpSegment(CitedModel):
    """1.1.2 `icp_profile`.

    `firmographics` is a union because the two shipped halves of this pipeline
    disagree about it, and the disagreement is legitimate: PRD §10 does not type
    the field, node 1.1.2 asks the model for "a sentence of firmographics", and
    a structured `{employees: …, sites: …}` map is what the field looks like
    when it comes from a CRM rollup. Both render. Refusing one of them would
    fail the very last node of a forty-minute run over a section heading.
    """

    label: str
    firmographics: dict[str, Any] | str = Field(default_factory=dict)
    triggers: list[str] = Field(default_factory=list)
    jobs_to_be_done: list[str] = Field(default_factory=list)
    share_of_revenue_pct: float | None = Field(default=None, ge=0, le=100)


class IcpExclusion(CitedModel):
    """1.1.3 `negative_icp` — who we are *not* selling to, and how to spot them."""

    persona: str
    disqualifier: str
    observable_signal: str | None = None
    suggested_negative_terms: list[str] = Field(default_factory=list)


class MarketCoverage(CitedModel):
    """1.1.4 `market_coverage`."""

    country: str
    language: str | None = None
    currency: str | None = None
    demand_months: list[int] = Field(default_factory=list, description="1–12")
    dead_months: list[int] = Field(default_factory=list, description="1–12")


class RegulatedTerm(ReportModel):
    term: str
    rule: str


class ComplianceGuardrails(ReportModel):
    """1.1.5 ⛳ `compliance_guardrails` — gated on an `approver` (legal)."""

    prohibited_claims: list[str] = Field(default_factory=list)
    required_disclaimers: list[str] = Field(default_factory=list)
    regulated_terms: list[RegulatedTerm] = Field(default_factory=list)
    confidence: Confidence | None = None


class BusinessContext(ReportModel):
    """Stage 1.1 rolled up."""

    products: list[Product] = Field(default_factory=list)
    ltv_estimate: float | None = None
    target_cac: float | None = None
    payback_months: float | None = None
    segments: list[IcpSegment] = Field(default_factory=list)
    exclusions: list[IcpExclusion] = Field(default_factory=list)
    markets: list[MarketCoverage] = Field(default_factory=list)
    compliance: ComplianceGuardrails | None = None


# ---------------------------------------------------------------------------
# 1.2 — What we already ran
# ---------------------------------------------------------------------------


class PerformanceFinding(CitedModel):
    """1.2.1 `historical_performance` — one winner or one loser."""

    campaign: str
    metric_delta: str | None = None
    period: str | None = None


class ProfitableTerm(CitedModel):
    """1.2.2 `search_term_pnl`, profitable side. Arithmetic is pandas', not the model's."""

    term: str
    cost: float | None = None
    conv: float | None = None
    cpa: float | None = None
    roas: float | None = None


class WastefulTerm(CitedModel):
    """1.2.2 `search_term_pnl`, wasteful side."""

    term: str
    cost: float | None = None
    conv: float = 0.0
    recommended_action: str | None = None


class FailedExperiment(CitedModel):
    """1.2.3 `failed_experiments` — the institutional memory that stops a repeat."""

    what: str
    when: str | None = None
    outcome: str | None = None
    do_not_repeat_reason: str | None = None


class AccountLearnings(ReportModel):
    """Stage 1.2 rolled up."""

    winners: list[PerformanceFinding] = Field(default_factory=list)
    losers: list[PerformanceFinding] = Field(default_factory=list)
    structural_findings: list[str] = Field(default_factory=list)
    profitable_terms: list[ProfitableTerm] = Field(default_factory=list)
    wasteful_terms: list[WastefulTerm] = Field(default_factory=list)
    tried_and_failed: list[FailedExperiment] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 1.3 — The competition
# ---------------------------------------------------------------------------


class Competitor(CitedModel):
    """1.3.1 `competitor_set`.

    `threat` and `positioning` were reaching the payload through `extra="allow"`
    and no renderer knew they existed, which is why the competitor table led
    with youtube.com: overlap alone ranks a keyword vendor's organic-SERP
    neighbours, and on a term like "safety data sheet" those are Wikipedia, OSHA
    and YouTube. 1.3.1 had already judged them irrelevant. Declaring the field
    is what lets the table say so.
    """

    domain: str
    name: str | None = None
    overlap_score: float | None = Field(default=None, ge=0, le=1)
    overlap_basis: list[str] = Field(default_factory=list)
    #: 1.3.1's judgement: `direct`, `adjacent`, `aggregator` or `irrelevant`.
    threat: str | None = None
    #: One line on what they sell, in 1.3.1's words.
    positioning: str | None = None


class CompetitorAd(CitedModel):
    """1.3.2 `creative_corpus` — one observed ad.

    `screenshot_path` is a **storage key**, never a filesystem path (PRD §5.2);
    the PDF renderer resolves it through `storage.backend`.
    """

    advertiser: str
    headline: str | None = None
    description: str | None = None
    offer: str | None = None
    angle: str | None = None
    proof_type: str | None = None
    cta: str | None = None
    landing_url: str | None = None
    first_seen: str | None = None
    last_seen: str | None = None
    screenshot_path: str | None = None
    #: "image" | "video" | "text". Along with the advertiser, this is all the
    #: Transparency Center *grid* reliably yields — the creative renders as an
    #: image and its wording lives on the per-creative page — so it is what the
    #: report falls back to when every text field comes back empty.
    format: str | None = None
    theme: str | None = None


class MessageCluster(ReportModel):
    theme: str
    frequency: int | None = None
    advertisers: list[str] = Field(default_factory=list)


class SpendEstimate(CitedModel):
    """1.3.3 `spend_estimation`.

    `method` is required: PRD §10 says an estimate must state its method and is
    never to be presented as fact. The renderers print it next to the number.
    """

    competitor: str
    est_monthly_spend_range: str
    method: str
    confidence: Confidence | None = None
    peak_months: list[int] = Field(default_factory=list)


class Whitespace(CitedModel):
    """1.3.4 ⛳ `differentiation_claim` — gated on an `approver` (marketing)."""

    claim: str
    why_unsaid: str | None = None
    our_proof: str | None = None
    risk: str | None = None


class CompetitiveLandscape(ReportModel):
    """Stage 1.3 rolled up."""

    competitors: list[Competitor] = Field(default_factory=list)
    ads: list[CompetitorAd] = Field(default_factory=list)
    message_clusters: list[MessageCluster] = Field(default_factory=list)
    spend_estimates: list[SpendEstimate] = Field(default_factory=list)
    whitespace: list[Whitespace] = Field(default_factory=list)
    recommended_claim: str | None = None
    substantiation_required: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 1.4 — Demand
# ---------------------------------------------------------------------------


class NegativeKeyword(CitedModel):
    """1.4.4 `negative_blocklist`."""

    term: str
    match_type: Literal["broad", "phrase", "exact"] = "phrase"
    reason: str | None = None
    source: Literal["lost_reasons", "wasteful_terms", "intent_irrelevant"] | None = None


class KeywordPageMapping(CitedModel):
    """1.4.5 `keyword_to_page_map`."""

    term_cluster: str
    best_url: str | None = None
    relevance_score: float | None = Field(default=None, ge=0, le=1)
    verdict: MappingVerdict


class ContentGap(ReportModel):
    cluster: str
    required_page_type: str


class DemandMap(ReportModel):
    """Stage 1.4 rolled up. The keyword rows themselves live in
    `ResearchReport.priced_keyword_list` — they are the CSV export, so they are
    held once, at the top level, rather than duplicated inside a section."""

    total_keywords: int = Field(default=0, ge=0)
    negatives: list[NegativeKeyword] = Field(default_factory=list)
    mapping: list[KeywordPageMapping] = Field(default_factory=list)
    content_gaps: list[ContentGap] = Field(default_factory=list)


class PricedKeyword(StrictReportModel):
    """One row of the CSV export (PRD §12). Strict on purpose — see
    `StrictReportModel`.

    `seasonality_index` is twelve monthly multipliers, January first, and the
    PDF renders it as a sparkline. A list of any other length is a bug in the
    node that produced it, so it is rejected here rather than plotted wrong.
    """

    term: str = Field(min_length=1)
    market: str | None = None
    intent: Intent | None = None
    funnel_stage: str | None = None
    volume: int | None = Field(default=None, ge=0)
    cpc_low: float | None = Field(default=None, ge=0)
    cpc_high: float | None = Field(default=None, ge=0)
    competition: float | None = Field(default=None, ge=0, le=1)
    seasonality_index: list[float] = Field(default_factory=list)
    trend_yoy: float | None = None
    best_url: str | None = None
    verdict: MappingVerdict | None = None
    match_type: Literal["broad", "phrase", "exact"] = "phrase"

    @field_validator("seasonality_index")
    @classmethod
    def _twelve_or_none(cls, value: list[float]) -> list[float]:
        if value and len(value) != 12:
            raise ValueError(f"seasonality_index must hold 12 monthly values, got {len(value)}")
        return value


# ---------------------------------------------------------------------------
# 1.5 — Readiness
# ---------------------------------------------------------------------------


class PageAudit(CitedModel):
    """1.5.1 `landing_page_audit`."""

    url: str
    lcp_ms: float | None = Field(default=None, ge=0)
    cls: float | None = Field(default=None, ge=0)
    mobile_ok: bool | None = None
    form_fields_count: int | None = Field(default=None, ge=0)
    trust_signals: list[str] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)
    severity: Literal["critical", "major", "minor", "none"] | None = None


class ConversionAction(CitedModel):
    """1.5.2 `tracking_probe`."""

    name: str
    status: str | None = None
    last_conversion_at: datetime | None = None
    staleness_days: int | None = None


class SyntheticCheck(ReportModel):
    """The synthetic conversion probe: did a click we fired ourselves come back?"""

    fired_at: datetime | None = None
    observed_in_ads_api: bool | None = None
    latency_min: float | None = None
    verdict: Literal["pass", "fail", "inconclusive"] | None = None


class AudienceList(CitedModel):
    """1.5.3 ⛳ `audience_consent_check` — gated on an `approver` (data officer)."""

    name: str
    size: int | None = Field(default=None, ge=0)
    consent_basis: str | None = None
    markets_allowed: list[str] = Field(default_factory=list)
    usable: bool = False
    blocker: str | None = None


class Scenario(CitedModel):
    """1.5.4 `opportunity_sizing` — arithmetic in Python, assumptions from the model."""

    budget_usd_month: float = Field(ge=0)
    est_clicks: float | None = Field(default=None, ge=0)
    est_conv: float | None = Field(default=None, ge=0)
    est_cpa: float | None = Field(default=None, ge=0)
    est_revenue: float | None = Field(default=None, ge=0)
    assumptions: list[str] = Field(default_factory=list)
    confidence_interval: str | None = None


class Readiness(ReportModel):
    """Stage 1.5 rolled up."""

    pages: list[PageAudit] = Field(default_factory=list)
    conversion_actions: list[ConversionAction] = Field(default_factory=list)
    synthetic_check: SyntheticCheck | None = None
    alerts: list[str] = Field(default_factory=list)
    lists: list[AudienceList] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# The report
# ---------------------------------------------------------------------------


class ResearchReport(ReportModel):
    """PRD §11, verbatim in field names and order.

    This object is the single source of truth for every export. If something is
    not in here, it is not in the PDF, the DOCX, the markdown, the JSON or the
    CSV — which is the point.
    """

    schema_version: Literal["1.0"] = SCHEMA_VERSION
    project_id: uuid.UUID
    run_id: uuid.UUID
    generated_at: datetime

    executive_summary: str = Field(min_length=1)
    launch_readiness: LaunchReadiness
    launch_blockers: list[Claim] = Field(default_factory=list)

    business_context: BusinessContext = Field(default_factory=BusinessContext)
    account_learnings: AccountLearnings = Field(default_factory=AccountLearnings)
    competitive_landscape: CompetitiveLandscape = Field(default_factory=CompetitiveLandscape)
    demand_map: DemandMap = Field(default_factory=DemandMap)
    readiness: Readiness = Field(default_factory=Readiness)

    priced_keyword_list: list[PricedKeyword] = Field(default_factory=list)
    recommended_next_actions: list[Claim] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    degraded_sources: list[str] = Field(
        default_factory=list, description="Connectors that partially failed during the run"
    )
    cost_usd: Annotated[float, Field(ge=0)] = 0.0

    @field_validator("executive_summary")
    @classmethod
    def _within_word_budget(cls, value: str) -> str:
        words = len(value.split())
        if words > EXECUTIVE_SUMMARY_MAX_WORDS:
            raise ValueError(
                f"executive_summary is {words} words; PRD §11 caps it at "
                f"{EXECUTIVE_SUMMARY_MAX_WORDS}"
            )
        return value

    @field_validator("degraded_sources")
    @classmethod
    def _dedupe_sorted(cls, value: list[str]) -> list[str]:
        """Order here is cosmetic, and cosmetic differences break golden tests."""
        return sorted(set(value))

    @property
    def is_launchable(self) -> bool:
        return self.launch_readiness != "no_go"

    def evidence_ids(self) -> list[uuid.UUID]:
        """Every evidence id cited anywhere in the report, deduped and ordered.

        The Report Viewer's citation popovers fetch these in one request rather
        than one per claim, and the critique node (1.6.2) uses the same walk to
        find claims whose citations went missing.
        """
        found: dict[uuid.UUID, None] = {}
        for identifier in _walk_evidence_ids(self.model_dump(mode="python")):
            found.setdefault(identifier, None)
        return list(found)


def _walk_evidence_ids(node: Any) -> list[uuid.UUID]:
    """Depth-first collection of every `evidence_ids` entry, at any nesting depth.

    Mirrors the executor's own walk (`nodes.base.EVIDENCE_FIELD`) so the two
    cannot disagree about where a citation may live.
    """
    found: list[uuid.UUID] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "evidence_ids" and isinstance(value, list):
                found.extend(item for item in value if isinstance(item, uuid.UUID))
            else:
                found.extend(_walk_evidence_ids(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_walk_evidence_ids(item))
    return found
