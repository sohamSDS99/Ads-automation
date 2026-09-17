"""Stage 1.1 — understand our own business (PRD §10).

Five nodes. Four run unattended; `1.1.5 compliance_guardrails` is a **gate** —
it writes an `Approval` and halts its branch until a human with the `approver`
role decides it, because "which claims may we make" is a legal question and
PRD §16 is explicit that there is no auto-approve.

Three of the five split their work in two, and it is worth saying why once: PRD
§18 law 3 puts *all* arithmetic in Python, so share-of-revenue, ACV, LTV,
payback and the seasonal month arrays are computed in `frames.py`, the model is
asked only for the labels a table cannot hold, and the two are merged here. The
numbers never pass through the model, so a model that miscopies one cannot put
a wrong figure in the report.

**One deviation from PRD §10**, argued in the PR: the table puts `acv` and
`gross_margin_pct` inside `products[]`, but a CRM export attributes revenue to
accounts and not to product lines, so a per-product ACV would be invented. The
measured ACV, LTV, target CAC and payback live at the top of `OfferEconomics`
in an `economics` object; `products[]` keeps the qualitative fields plus the
margin the model states as an explicit assumption.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import structlog
from pydantic import BaseModel, Field

from agent.db.models import ApprovalRequiredRole, Evidence
from agent.documents import BRAND_DOC
from agent.llm.router import TaskClass
from agent.nodes import frames, gather, prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext

log = structlog.get_logger(__name__)

CRM_WON = "crm_won"
CRM_LOST = "crm_lost"
CAMPAIGN_PERF = "campaign_perf"
CHANGE_LOG = "change_log"
PAGE = "page"


# ---------------------------------------------------------------------------
# 1.1.1 — offer_economics
# ---------------------------------------------------------------------------


class Product(BaseModel):
    """One thing this business sells, as described from the evidence."""

    name: str
    price_model: str = Field(description="subscription | one-off | usage-based | services | other")
    delivery_cost_notes: str = Field(
        default="", description="What it costs us to deliver, in one or two sentences."
    )
    evidence_ids: list[str] = Field(default_factory=list)


class EconomicsAssumptions(BaseModel):
    """The three judgements behind LTV. Stated by the model, applied in Python."""

    gross_margin_pct: float = Field(ge=0, le=100, description="Blended gross margin, 0-100.")
    expected_lifetime_months: float = Field(
        ge=1, le=240, description="How long an average customer stays, in months."
    )
    target_ltv_cac_ratio: float = Field(
        default=3.0, ge=1, le=10, description="The LTV:CAC ratio this business targets."
    )
    basis: str = Field(default="", description="Why these three numbers, from the evidence.")


class OfferEconomicsDraft(BaseModel):
    """What the model is asked for. Carries no computed figure."""

    products: list[Product] = Field(default_factory=list)
    assumptions: EconomicsAssumptions


class MeasuredEconomics(BaseModel):
    """Computed in `frames.crm_economics`. Never written by a model."""

    acv: float
    median_deal_value: float
    deals: int
    revenue: float
    gross_margin_pct: float
    lifetime_months: float
    ltv_estimate: float
    target_cac: float
    payback_months: float | None


class OfferEconomics(BaseModel):
    """1.1.1 output."""

    products: list[Product] = Field(default_factory=list)
    assumptions: EconomicsAssumptions | None = None
    economics: MeasuredEconomics | None = None
    ltv_estimate: float | None = None
    target_cac: float | None = None
    payback_months: float | None = None
    coverage: list[str] = Field(default_factory=list)


class OfferEconomicsNode(LLMNode):
    """1.1.1 — what we sell, what it is worth, and what we can afford to pay for it."""

    spec = NodeSpec(
        id="1.1.1",
        name="offer_economics",
        stage="1.1",
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=OfferEconomics,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        # The uploaded documents are where a price list actually lives. A CRM
        # export says what deals closed for; a pricing sheet says what the list
        # price was and what the tiers are, which is the difference between
        # describing history and describing the offer.
        found = await gather.collect(
            ctx, gather.Need(CRM_WON), gather.Need(BRAND_DOC, limit=120, optional=True)
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        documents = prompts.documents_block(found, BRAND_DOC)
        frame = frames.crm_frame(found.payloads(CRM_WON), [row.id for row in found.of(CRM_WON)])
        preview = frames.crm_economics(
            frame, gross_margin_pct=100.0, lifetime_months=12.0, ltv_cac_ratio=3.0
        )

        draft = await ctx.complete(
            OfferEconomicsDraft,
            system=prompts.system_prompt(
                "You establish what a business sells and what it can afford to pay for a "
                "customer. You state assumptions; you never state a number that was measured "
                "for you."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "measured closed-won revenue (yours to reason from, not to restate)",
                    {
                        "deals": preview.deals,
                        "revenue": preview.revenue,
                        "average_deal_value": preview.acv,
                        "median_deal_value": preview.median_deal_value,
                    },
                ),
                prompts.evidence_block(found, CRM_WON, title="closed-won deals"),
                documents,
                prompts.coverage_block(found),
                prompts.cite_from("EVIDENCE — closed-won deals", prompts.documents_cite(documents)),
                "TASK\n"
                "  List the products or services this business sells, each with its pricing "
                "model and what it costs to deliver. Then state three assumptions: blended "
                "gross margin, expected customer lifetime in months, and the LTV:CAC ratio "
                "this business should target. Justify all three from the evidence.",
            ),
        )

        measured = frames.crm_economics(
            frame,
            gross_margin_pct=draft.assumptions.gross_margin_pct,
            lifetime_months=draft.assumptions.expected_lifetime_months,
            ltv_cac_ratio=draft.assumptions.target_ltv_cac_ratio,
        )
        economics = MeasuredEconomics(**measured.as_dict())
        return OfferEconomics(
            products=draft.products,
            assumptions=draft.assumptions,
            economics=economics,
            ltv_estimate=economics.ltv_estimate,
            target_cac=economics.target_cac,
            payback_months=economics.payback_months,
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.1.2 — icp_profile
# ---------------------------------------------------------------------------


class SegmentLabel(BaseModel):
    """The model's description of one computed segment, keyed back to it."""

    key: str = Field(description="Copy the `key` of the segment this describes.")
    label: str
    firmographics: str = Field(default="", description="Who they are, in one or two sentences.")
    triggers: list[str] = Field(default_factory=list)
    jobs_to_be_done: list[str] = Field(default_factory=list)


class SegmentLabels(BaseModel):
    labels: list[SegmentLabel] = Field(default_factory=list)


class IcpSegment(BaseModel):
    """1.1.2's segment: the model's words, the frame's numbers."""

    label: str
    firmographics: str = ""
    triggers: list[str] = Field(default_factory=list)
    jobs_to_be_done: list[str] = Field(default_factory=list)
    industry: str
    country: str
    size_band: str
    deals: int
    revenue: float
    share_of_revenue_pct: float
    avg_deal_value: float
    evidence_ids: list[str] = Field(default_factory=list)


class IcpProfile(BaseModel):
    """1.1.2 output."""

    segments: list[IcpSegment] = Field(default_factory=list)
    segments_omitted: int = 0
    coverage: list[str] = Field(default_factory=list)


class IcpProfileNode(LLMNode):
    """1.1.2 — who already buys, ranked by the revenue they actually brought."""

    spec = NodeSpec(
        id="1.1.2",
        name="icp_profile",
        stage="1.1",
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=IcpProfile,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx, gather.Need(CRM_WON), gather.Need(BRAND_DOC, limit=120, optional=True)
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        # An ICP one-pager names the triggers and the jobs-to-be-done that a
        # deal table can only imply. The numbers still come from the frame.
        documents = prompts.documents_block(found, BRAND_DOC)
        frame = frames.crm_frame(found.payloads(CRM_WON), [row.id for row in found.of(CRM_WON)])
        segments, omitted = frames.crm_segments(frame)
        if not segments:
            return IcpProfile(coverage=gather.coverage_notes(found))

        labels = await ctx.complete(
            SegmentLabels,
            system=prompts.system_prompt(
                "You name and characterise the customer segments a business already wins. "
                "The segmentation has been computed for you; your job is to say who each "
                "segment is and why they buy."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "segments (final — do not restate these numbers)",
                    [segment.as_dict() for segment in segments],
                ),
                prompts.evidence_block(found, CRM_WON, title="closed-won deals"),
                documents,
                prompts.coverage_block(found),
                "TASK\n"
                "  For each segment above, return one entry whose `key` is copied exactly "
                "from the table. Give it a short label, a sentence of firmographics, the "
                "triggers that make this segment start looking, and the jobs they are hiring "
                "the product to do. Describe only segments in the table.",
            ),
        )
        by_key = {item.key: item for item in labels.labels}
        unknown = sorted(set(by_key) - {segment.key for segment in segments})
        if unknown:
            log.info("node.unknown_segment_keys", node_id=self.spec.id, keys=unknown)

        return IcpProfile(
            segments=[
                IcpSegment(
                    label=(by_key[segment.key].label if segment.key in by_key else segment.key),
                    firmographics=(
                        by_key[segment.key].firmographics if segment.key in by_key else ""
                    ),
                    triggers=(by_key[segment.key].triggers if segment.key in by_key else []),
                    jobs_to_be_done=(
                        by_key[segment.key].jobs_to_be_done if segment.key in by_key else []
                    ),
                    industry=segment.industry,
                    country=segment.country,
                    size_band=segment.size_band,
                    deals=segment.deals,
                    revenue=segment.revenue,
                    share_of_revenue_pct=segment.share_of_revenue_pct,
                    avg_deal_value=segment.avg_deal_value,
                    evidence_ids=[str(item) for item in segment.evidence_ids],
                )
                for segment in segments
            ],
            segments_omitted=omitted,
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.1.3 — negative_icp
# ---------------------------------------------------------------------------


class Exclusion(BaseModel):
    """Who not to pay for, and how to recognise them in a search query."""

    persona: str
    disqualifier: str
    observable_signal: str = Field(
        default="", description="What shows up in a query, a form fill or a firmographic field."
    )
    suggested_negative_terms: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class NegativeIcp(BaseModel):
    """1.1.3 output."""

    exclusions: list[Exclusion] = Field(default_factory=list)
    lost_reason_summary: list[dict[str, Any]] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class NegativeIcpNode(LLMNode):
    """1.1.3 — who we lose to and why, turned into things not to bid on."""

    spec = NodeSpec(
        id="1.1.3",
        name="negative_icp",
        stage="1.1",
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=NegativeIcp,
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx, gather.Need(CRM_LOST), gather.Need(BRAND_DOC, limit=120, optional=True)
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        # "Who we are not for" is usually written down somewhere — a
        # qualification guide, a sales playbook — long before it shows up as a
        # pattern in lost deals.
        documents = prompts.documents_block(found, BRAND_DOC)
        frame = frames.crm_frame(found.payloads(CRM_LOST), [row.id for row in found.of(CRM_LOST)])
        reasons = frames.lost_reasons(frame)

        result = await ctx.complete(
            NegativeIcp,
            system=prompts.system_prompt(
                "You work out who a business should refuse to pay for, from the deals it "
                "lost. Every exclusion must be recognisable before the click — a search "
                "term, a firmographic, a form answer."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block("closed-lost reasons by frequency", reasons),
                prompts.evidence_block(found, CRM_LOST, title="closed-lost deals"),
                documents,
                prompts.coverage_block(found),
                prompts.cite_from(
                    "EVIDENCE — closed-lost deals",
                    prompts.documents_cite(documents),
                    "the `evidence_ids` in the reasons table",
                ),
                "TASK\n"
                "  Return the personas this business should exclude. For each: the "
                "disqualifier, the signal that reveals it before we pay for a click, and "
                "negative keyword terms that would block it. Leave `lost_reason_summary` "
                "empty — it is filled in for you.",
            ),
        )
        return NegativeIcp(
            exclusions=result.exclusions,
            lost_reason_summary=reasons,
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.1.4 — market_coverage
# ---------------------------------------------------------------------------


class MarketLabel(BaseModel):
    country: str = Field(description="Copy the `country` of the market this describes.")
    language: str = ""
    currency: str = Field(default="", description="ISO 4217 code, e.g. USD.")
    note: str = ""


class MarketLabels(BaseModel):
    markets: list[MarketLabel] = Field(default_factory=list)


class Market(BaseModel):
    """1.1.4's market: months measured from won deals, labels from the model."""

    country: str
    language: str = ""
    currency: str = ""
    note: str = ""
    deals: int = 0
    monthly_deals: list[int] = Field(default_factory=list)
    demand_months: list[int] = Field(default_factory=list)
    dead_months: list[int] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class MarketCoverage(BaseModel):
    """1.1.4 output."""

    markets: list[Market] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class MarketCoverageNode(LLMNode):
    """1.1.4 — which markets buy, and in which months."""

    spec = NodeSpec(
        id="1.1.4",
        name="market_coverage",
        stage="1.1",
        depends_on=("1.1.2",),
        task_class=TaskClass.EXTRACT,
        input_model=IcpProfile,
        output_model=MarketCoverage,
        connectors=("google_ads",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx,
            gather.Need(CRM_WON),
            gather.Need(CAMPAIGN_PERF, connector="google_ads"),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        frame = frames.crm_frame(found.payloads(CRM_WON), [row.id for row in found.of(CRM_WON)])
        demand = frames.crm_demand_by_country(frame)
        configured = [
            str(market.get("country") or "").strip()
            for market in (ctx.project.markets or [])
            if isinstance(market, dict)
        ]
        if not demand and not configured:
            return MarketCoverage(coverage=gather.coverage_notes(found))

        # A market the project is configured for but has never closed a deal in
        # is still a market. It gets an all-zero demand profile rather than
        # being dropped, because "we sell here and have nothing to show for it"
        # is a finding.
        measured = {item.country: item for item in demand}
        countries = list(measured) + [name for name in configured if name not in measured]

        labels = await ctx.complete(
            MarketLabels,
            system=prompts.system_prompt(
                "You describe the markets a business sells into. Seasonality has been "
                "measured for you from closed-won dates; supply the language and currency "
                "each market trades in and a one-sentence read of its shape."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                # 1.1.4 depends on 1.1.2 in PRD §10, and this is why: which
                # markets matter is a question about who buys, not only about
                # where deals happened to close.
                prompts.computed_block(
                    "the segments that already buy (node 1.1.2)", ctx.output_of("1.1.2")
                ),
                prompts.computed_block(
                    "measured demand by country (final — months are 1-12)",
                    [item.as_dict() for item in demand],
                ),
                prompts.computed_block("countries to return", countries),
                prompts.coverage_block(found),
                "TASK\n"
                "  Return one entry per country listed above, copying `country` exactly. "
                "Give its primary selling language, its ISO 4217 currency, and one sentence "
                "on what the monthly pattern shows, read against the segments above. Do not "
                "restate the month arrays.",
            ),
        )
        by_country = {item.country: item for item in labels.markets}
        return MarketCoverage(
            markets=[
                Market(
                    country=country,
                    language=_label(by_country, country, "language"),
                    currency=_label(by_country, country, "currency"),
                    note=_label(by_country, country, "note"),
                    deals=measured[country].deals if country in measured else 0,
                    monthly_deals=(
                        list(measured[country].monthly_deals) if country in measured else [0] * 12
                    ),
                    demand_months=(
                        list(measured[country].demand_months) if country in measured else []
                    ),
                    dead_months=(
                        list(measured[country].dead_months) if country in measured else []
                    ),
                    evidence_ids=(
                        [str(item) for item in measured[country].evidence_ids]
                        if country in measured
                        else []
                    ),
                )
                for country in countries
            ],
            coverage=gather.coverage_notes(found),
        )


# ---------------------------------------------------------------------------
# 1.1.5 ⛳ — compliance_guardrails
# ---------------------------------------------------------------------------


class RegulatedTerm(BaseModel):
    term: str
    rule: str = Field(description="What the rule actually says about using this term.")
    evidence_ids: list[str] = Field(default_factory=list)


class ComplianceGuardrails(BaseModel):
    """1.1.5 output — the proposal a human approves, edits or rejects."""

    prohibited_claims: list[str] = Field(default_factory=list)
    required_disclaimers: list[str] = Field(default_factory=list)
    regulated_terms: list[RegulatedTerm] = Field(default_factory=list)
    prior_disapprovals: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0, le=1)
    reviewer_notes: str = Field(
        default="", description="What a legal reviewer should check before approving."
    )
    coverage: list[str] = Field(default_factory=list)


class ComplianceGuardrailsNode(LLMNode):
    """1.1.5 — the gate. What we may and may not say, decided by a human.

    `gate=True` means the executor stops this branch when the node's output is
    ready: it writes an `Approval` for the `approver` role, emits
    `approval.required` and leaves every other branch running (PRD §7.2 item 5).
    Nothing downstream of it proceeds until someone decides.
    """

    spec = NodeSpec(
        id="1.1.5",
        name="compliance_guardrails",
        stage="1.1",
        depends_on=("1.1.1",),
        gate=True,
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.SYNTHESIZE,
        input_model=OfferEconomics,
        output_model=ComplianceGuardrails,
        connectors=("google_ads",),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx,
            gather.Need(CHANGE_LOG, connector="google_ads"),
            gather.Need(PAGE, limit=60),
            # A compliance policy, a claims-substantiation sheet or a legal
            # review doc is uploaded far more often than it is published on the
            # website, and this is the node whose whole job is reading it.
            gather.Need(BRAND_DOC, limit=200, optional=True),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    def system_prompt(self, ctx: RunContext) -> str:
        return prompts.system_prompt(
            "You draft advertising compliance guardrails for a legal reviewer to approve. "
            "You are not the decision — you are the proposal. Be conservative: flag anything "
            "a regulator or an ad platform could object to, and say plainly where the "
            "evidence does not let you judge."
        )

    def user_prompt(self, ctx: RunContext, ev: Sequence[Evidence]) -> str:
        found: gather.Gathered = ctx.scratch[self.spec.id]
        offer = ctx.output_of("1.1.1")
        documents = prompts.documents_block(found, BRAND_DOC)
        return prompts.compose(
            prompts.project_block(ctx.project),
            prompts.computed_block("what this business sells (node 1.1.1)", offer),
            prompts.evidence_block(
                found, CHANGE_LOG, title="account change history (prior disapprovals)"
            ),
            prompts.evidence_block(found, PAGE, title="our own pages (claims and policy copy)"),
            documents,
            prompts.coverage_block(found),
            prompts.cite_from(
                "EVIDENCE — account change history (prior disapprovals)",
                "EVIDENCE — our own pages (claims and policy copy)",
                prompts.documents_cite(documents),
            ),
            "TASK\n"
            "  Return the claims this advertiser must not make, the disclaimers it must "
            "carry, and the regulated terms with the rule that governs each. List any prior "
            "disapproval you can see in the change history. Set `confidence` to how far the "
            "evidence supports this draft, and use `reviewer_notes` to tell the legal "
            "reviewer exactly what to check.",
        )

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        result = await super().reason(ctx, ev)
        found: gather.Gathered = ctx.scratch[self.spec.id]
        assert isinstance(result, ComplianceGuardrails)  # noqa: S101 — output_model is this type
        return result.model_copy(update={"coverage": gather.coverage_notes(found)})


# ---------------------------------------------------------------------------
# helpers, and the module-level instances the registry discovers
# ---------------------------------------------------------------------------


def _label(labels: dict[str, MarketLabel], country: str, field_name: str) -> str:
    item = labels.get(country)
    return str(getattr(item, field_name)) if item is not None else ""


offer_economics = OfferEconomicsNode()
icp_profile = IcpProfileNode()
negative_icp = NegativeIcpNode()
market_coverage = MarketCoverageNode()
compliance_guardrails = ComplianceGuardrailsNode()
