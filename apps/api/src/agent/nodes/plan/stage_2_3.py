"""Stage 2.3 — choose the campaign types (Stage 02 PRD §11).

Three nodes. `2.3.1 channel_slate` carries gate **G4**, the last of the four and
the one that fixes what the account will actually contain: which channels run,
in which market, in which wave, and what has to be true before each one starts.

**What the model decides here, and what it does not.** It decides which channel
a campaign should run as, which terms are the brand, and which surfaces may be
automated. It decides no figure: `est_share_of_budget_pct` and the brand
campaign's `budget_pct` are `allocation.share_v1` re-totalling the split the
budget owner approved at G3, and `overlap_pct` is `structure.overlap_v1` over
the demand each channel can reach. A channel's share of the budget is not a
judgement — it is the consequence of a decision a human already signed.

**Two rules the model is not allowed to overrule**, both because they are
policy rather than analysis:

* **Brand exclusions on Performance Max are mandatory** whenever PMax runs
  (Q9's default). PMax bids on brand queries by default, reports them blended
  with everything else, and the result is an account that looks like it is
  acquiring customers while it is buying its own name back. The model may
  decide whether PMax runs at all; it may not decide this.
* **A slate entry may only name campaigns that gate G1 agreed.** 2.1.3 fixed
  the campaign list and a human approved it. 2.3.1 chooses channels, not
  campaigns, and an entry naming something else is dropped and reported.

**Why an automated channel's overlap is the whole market.** A Search campaign
reaches the clusters someone pointed it at. Performance Max, Demand Gen and
Display reach whatever Google decides serves the objective, which is every
cluster in the market — including the brand cluster, and including demand
another campaign is already paying for. Modelling those two the same way would
report zero overlap between Search and PMax, which is the single most expensive
wrong answer this stage could give.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.db.models import ApprovalRequiredRole, Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes import prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.planning import structure


class SlateUnusable(ValueError):
    """The slate covers none of the budget a human approved at gate G3.

    Raised in place of the `CalcError` `allocation.share_v1` would otherwise
    throw three frames down. Both fail the node; only one of them tells the
    person reading the run what went wrong.
    """


SHARE = "allocation.share_v1"
OVERLAP = "structure.overlap_v1"

#: Google Ads channel types this product plans for. `shopping` is here so a
#: model can reject it with a reason rather than silently never considering it.
CampaignType = Literal["search", "performance_max", "display", "video", "demand_gen", "shopping"]

#: Channels whose targeting Google controls. These reach every cluster in their
#: market, not the ones a campaign was pointed at — see the module docstring.
AUTOMATED_TARGETING: frozenset[str] = frozenset(
    {"performance_max", "display", "video", "demand_gen"}
)

#: Channels that need brand exclusions before they may run alongside a brand
#: campaign. PMax is the one Q9 is about; Demand Gen has the same blend.
BRAND_EXCLUSION_REQUIRED: frozenset[str] = frozenset({"performance_max", "demand_gen"})

BrandVariant = Literal[
    "exact_brand", "misspelling", "brand_plus_category", "product_name", "competitor"
]
MatchType = Literal["broad", "phrase", "exact"]
Status = Literal["ok", "insufficient_input"]


#: The label a slate entry is totalled and reported under.
def _key(campaign_type: str, market: str) -> str:
    return f"{campaign_type}|{market}"


# ---------------------------------------------------------------------------
# 2.3.1 ⛳ G4 — channel_slate
# ---------------------------------------------------------------------------


class SlateEntryDraft(BaseModel):
    """One channel in one market, as the model proposes it. No figures."""

    campaign_type: CampaignType
    market: str = Field(min_length=1)
    campaign_refs: list[str] = Field(
        min_length=1,
        description="Campaigns agreed at gate G1 that run as this channel. Never a new name.",
    )
    launch_wave: int = Field(ge=1, le=6)
    rationale: str = Field(min_length=1)
    entry_criteria: list[str] = Field(default_factory=list)
    exit_criteria: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)


class RejectedChannel(BaseModel):
    campaign_type: CampaignType
    why_not: str = Field(min_length=1)


class SlateDraft(BaseModel):
    """What 2.3.1 asks the model for: a channel per campaign, and what it ruled out."""

    slate: list[SlateEntryDraft]
    rejected: list[RejectedChannel] = Field(default_factory=list)
    notes: str = Field(min_length=1)


class SlateEntry(BaseModel):
    """One channel in one market, with the share of the approved budget it carries."""

    campaign_type: CampaignType
    market: str
    campaign_refs: list[str]
    launch_wave: int
    rationale: str
    entry_criteria: list[str] = Field(default_factory=list)
    exit_criteria: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    est_share_of_budget_pct: float
    est_monthly_usd: float


class ChannelSlateOutput(BaseModel):
    """2.3.1 ⛳ G4 — what runs, where, and in which wave."""

    slate: list[SlateEntry]
    rejected: list[RejectedChannel] = Field(default_factory=list)
    #: The campaign the brand terms belong to, carried forward for 2.3.3 and
    #: 2.4.2. Selected from the agreed campaigns, never invented.
    brand_campaign_ref: str | None = None
    #: Approved budget that no channel entry claimed. Money with no channel is
    #: a plan that cannot be built, so it is named rather than left to a reader
    #: to notice the percentages do not reach 100.
    unplaced_campaigns: list[str] = Field(default_factory=list)
    dropped: list[str] = Field(default_factory=list)
    notes: str
    #: Filled by the approver's edit, never by this node.
    edits_applied: list[dict[str, Any]] = Field(default_factory=list)
    status: Status
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class ChannelSlateNode(LLMNode):
    """2.3.1 ⛳ G4 — the gate that fixes what the account will contain."""

    spec = NodeSpec(
        id="2.3.1",
        name="channel_slate",
        stage="2.3",
        run_stage=RunStage.PLAN,
        depends_on=("2.2.4", "2.1.3"),
        gate=True,
        gate_key="G4",
        # §5.3 routes G4 to the marketing lead. `approver` is the role; which
        # person is `Project.settings.plan_approvers["G4"]`.
        required_role=ApprovalRequiredRole.APPROVER,
        task_class=TaskClass.SYNTHESIZE,
        input_model=BaseModel,
        output_model=ChannelSlateOutput,
        connectors=(),
        calc=(SHARE,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        """Nothing to pull. Everything 2.3.1 reads is `PlanInput` or an earlier node.

        Law 13: the competitive landscape and the readiness audit were
        established by research and travel in `PlanInput`. Re-querying a
        connector for them is exactly what that law forbids.
        """
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        plan = ctx.require_plan()
        approved = ctx.output_of("2.2.4")
        allocation = list(approved.get("allocation") or [])
        envelope = float((approved.get("envelope") or {}).get("monthly_cap_usd") or 0.0)
        agreed = _agreed_refs(ctx)

        draft = await ctx.complete(
            SlateDraft,
            system=prompts.system_prompt(
                "You choose which Google Ads channel each agreed campaign should run as, "
                "in which market and in which launch wave. You never invent a campaign, "
                "never write a figure, and never put a channel in the slate that you "
                "cannot say the entry criteria for."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block(
                    "the campaigns agreed at gate G1 (use these refs, and only these)",
                    ctx.output_of("2.1.3").get("objectives") or [],
                ),
                prompts.computed_block(
                    "the budget approved at gate G3 (final — do not restate or adjust)",
                    {"envelope": approved.get("envelope"), "allocation": allocation},
                ),
                prompts.computed_block(
                    "what research found about the competitive landscape",
                    _competition(plan),
                ),
                prompts.computed_block(
                    "what research found about launch readiness", _readiness(plan)
                ),
                "TASK\n"
                "  Put every agreed campaign into exactly one channel entry per market it "
                "is funded in. For each entry give `campaign_type`, `market`, the "
                "`campaign_refs` it carries, a `launch_wave` (1 is at launch), a one-line "
                "`rationale`, the `entry_criteria` that must hold before it starts, the "
                "`exit_criteria` that would end it, and any `prerequisites` someone must "
                "do first. Then list in `rejected` the channels you considered and ruled "
                "out, each with `why_not`. Write `notes` on what the slate assumes.",
            ),
            task_class=self.spec.task_class,
        )

        kept, dropped = _kept_entries(draft.slate, agreed)
        if not kept:
            raise SlateUnusable(
                "the channel slate named none of the campaigns agreed at gate G1 "
                f"({', '.join(sorted(agreed)) or 'none were agreed'}); "
                + ("; ".join(dropped) if dropped else "the slate was empty")
            )
        placement = _placement(kept)
        frame = structure.share_frame(
            allocation,
            label=lambda line: placement.label(
                str(line.get("campaign_ref") or ""), str(line.get("market") or "")
            ),
        )
        shares = await plan.calc.run(SHARE, frame, envelope_usd=envelope)
        by_group = {str(row["group"]): row for row in shares.value.get("groups", [])}

        return ChannelSlateOutput(
            slate=[
                SlateEntry(
                    **entry.model_dump(),
                    est_share_of_budget_pct=_figure(by_group, entry, "pct"),
                    est_monthly_usd=_figure(by_group, entry, "usd"),
                )
                for entry in kept
            ],
            rejected=draft.rejected,
            brand_campaign_ref=_brand_ref(ctx, agreed),
            unplaced_campaigns=_unplaced(allocation, placement),
            dropped=dropped,
            notes=draft.notes,
            status="ok",
            calc_evidence_ids=[shares.id],
        )


# ---------------------------------------------------------------------------
# 2.3.3 — brand_isolation
# ---------------------------------------------------------------------------


class BrandTerm(BaseModel):
    term: str = Field(min_length=1)
    variant_type: BrandVariant


class BrandDraft(BaseModel):
    """What 2.3.3 asks for: which terms are ours, and the policy around them."""

    brand_terms: list[BrandTerm] = Field(min_length=1)
    brand_campaign_ref: str = Field(min_length=1)
    match_types: list[MatchType] = Field(min_length=1)
    negatives_for_nonbrand: list[str] = Field(default_factory=list)
    reporting_rule: str = Field(min_length=1)
    competitor_bidding_policy: str = Field(min_length=1)
    notes: str = Field(min_length=1)


class BrandCampaign(BaseModel):
    campaign_ref: str
    match_types: list[MatchType]
    budget_pct: float
    target: float | None = None


class BrandIsolationOutput(BaseModel):
    """2.3.3 — the brand campaign, and the negatives that keep it isolated."""

    brand_terms: list[BrandTerm]
    brand_campaign: BrandCampaign
    negatives_for_nonbrand: list[str]
    reporting_rule: str
    competitor_bidding_policy: str
    notes: str
    status: Status
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class BrandIsolationNode(LLMNode):
    """2.3.3 — keeping the money spent on our own name visible and separate."""

    spec = NodeSpec(
        id="2.3.3",
        name="brand_isolation",
        stage="2.3",
        run_stage=RunStage.PLAN,
        depends_on=("2.3.1",),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=BrandIsolationOutput,
        connectors=(),
        calc=(SHARE,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        plan = ctx.require_plan()
        approved = ctx.output_of("2.2.4")
        allocation = list(approved.get("allocation") or [])
        envelope = float((approved.get("envelope") or {}).get("monthly_cap_usd") or 0.0)
        slate = ctx.output_of("2.3.1")
        proposed_ref = str(slate.get("brand_campaign_ref") or "")

        draft = await ctx.complete(
            BrandDraft,
            system=prompts.system_prompt(
                "You decide which search terms are this company's own brand, and the "
                "policy that keeps brand spend reported separately from acquisition "
                "spend. You never write a figure."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block("the channel slate agreed at gate G4", slate.get("slate")),
                prompts.computed_block(
                    "what research found about the competitive landscape", _competition(plan)
                ),
                prompts.computed_block("the terms research priced", _brand_candidates(plan)),
                "TASK\n"
                "  List the `brand_terms` — our own name and its variants, each with a "
                "`variant_type`. Name the `brand_campaign_ref` they belong to, from the "
                "agreed campaigns. Give the `match_types` the brand campaign should use, "
                "the `negatives_for_nonbrand` that keep those terms out of every other "
                "campaign, a `reporting_rule`, and a `competitor_bidding_policy`.",
            ),
            task_class=self.spec.task_class,
        )

        ref = proposed_ref or draft.brand_campaign_ref
        frame = structure.share_frame(
            allocation,
            label=lambda line: (
                "brand" if str(line.get("campaign_ref") or "") == ref else "nonbrand"
            ),
        )
        shares = await plan.calc.run(SHARE, frame, envelope_usd=envelope)
        brand = next(
            (row for row in shares.value.get("groups", []) if row["group"] == "brand"), None
        )

        # Every brand term is a negative everywhere else, whatever the model
        # listed. PRD §11's critique check 5 is asserted in tests, so a term the
        # model forgot here becomes a blocking issue at 2.6.2 instead.
        negatives = sorted(
            {*(term.term for term in draft.brand_terms), *draft.negatives_for_nonbrand}
        )

        return BrandIsolationOutput(
            brand_terms=draft.brand_terms,
            brand_campaign=BrandCampaign(
                campaign_ref=ref,
                match_types=draft.match_types,
                budget_pct=float(brand["pct"]) if brand else 0.0,
                target=(brand or {}).get("target_cpa_usd"),
            ),
            negatives_for_nonbrand=negatives,
            reporting_rule=draft.reporting_rule,
            competitor_bidding_policy=draft.competitor_bidding_policy,
            notes=draft.notes,
            status="ok",
            calc_evidence_ids=[shares.id],
        )


# ---------------------------------------------------------------------------
# 2.3.2 — automation_boundaries
# ---------------------------------------------------------------------------


class OverlapResolution(BaseModel):
    campaign_a: str
    campaign_b: str
    resolution: str = Field(min_length=1)


class AutomationDraft(BaseModel):
    """What 2.3.2 asks for: what may be automated, and who wins a collision."""

    pmax_allowed: bool
    included_themes: list[str] = Field(default_factory=list)
    excluded_urls: list[str] = Field(default_factory=list)
    account_negatives: list[str] = Field(default_factory=list)
    broad_match_campaigns: list[str] = Field(default_factory=list)
    broad_match_guardrails: list[str] = Field(default_factory=list)
    resolutions: list[OverlapResolution] = Field(default_factory=list)
    notes: str = Field(min_length=1)
    #: Accepted so an edited proposal validates, and then ignored: the value is
    #: policy, not analysis. See `_brand_exclusion_required`.
    brand_exclusion_required: bool = True


class PmaxBoundary(BaseModel):
    allowed: bool
    included_themes: list[str] = Field(default_factory=list)
    excluded_urls: list[str] = Field(default_factory=list)
    brand_exclusion_required: bool
    account_negatives: list[str] = Field(default_factory=list)


class BroadMatchBoundary(BaseModel):
    allowed_campaigns: list[str] = Field(default_factory=list)
    guardrails: list[str] = Field(default_factory=list)


class ChannelOverlap(BaseModel):
    campaign_a: str
    campaign_b: str
    overlap_pct: float
    a_shared_pct: float
    b_shared_pct: float
    shared: list[str] = Field(default_factory=list)
    resolution: str | None = None


class AutomationBoundariesOutput(BaseModel):
    """2.3.2 — where Google's automation is allowed to decide, and where it is not."""

    pmax: PmaxBoundary
    broad_match: BroadMatchBoundary
    overlap: list[ChannelOverlap]
    notes: str
    status: Status
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class AutomationBoundariesNode(LLMNode):
    """2.3.2 — the node that stops two campaigns paying for the same click."""

    spec = NodeSpec(
        id="2.3.2",
        name="automation_boundaries",
        stage="2.3",
        run_stage=RunStage.PLAN,
        # §11's edge list is `2.3.2<-{2.3.1, 2.3.3}`. 2.2.2 is added because the
        # overlap is computed over the cluster-to-campaign assignment that node
        # made, and a dependency `ctx.output_of` is not told about is one it is
        # entitled to refuse. It changes no wave ordering: 2.3.1 already depends
        # on 2.2.4, which depends on 2.2.2.
        depends_on=("2.3.1", "2.3.3", "2.2.2"),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=AutomationBoundariesOutput,
        connectors=(),
        calc=(OVERLAP,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        plan = ctx.require_plan()
        slate = list(ctx.output_of("2.3.1").get("slate") or [])
        brand = ctx.output_of("2.3.3")
        assignments = list(ctx.output_of("2.2.2").get("assignments") or [])
        targeting = structure.targeting_map(slate, assignments, automated=AUTOMATED_TARGETING)

        draft = await ctx.complete(
            AutomationDraft,
            system=prompts.system_prompt(
                "You decide where Google's automation may choose what we buy, and where "
                "it may not. You never write a figure, and you do not decide whether "
                "brand exclusions are required — they always are."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                _plan_block(ctx),
                prompts.computed_block("the channel slate agreed at gate G4", slate),
                prompts.computed_block("the brand policy from 2.3.3", brand),
                prompts.computed_block(
                    "what each channel entry can reach (final — computed, not a proposal)",
                    {label: members for label, members in sorted(targeting.items())},
                ),
                "TASK\n"
                "  Say whether Performance Max may run at all (`pmax_allowed`) and, if it "
                "may, the `included_themes` and `excluded_urls` that bound it, plus the "
                "`account_negatives` that should never be bought anywhere. Name the "
                "`broad_match_campaigns` that may use broad match and the "
                "`broad_match_guardrails` each one runs under. For every pair of channel "
                "entries above that reach the same demand, give a `resolution` saying "
                "which one keeps it.",
            ),
            task_class=self.spec.task_class,
        )

        frame = structure.member_frame(targeting)
        overlaps = await plan.calc.run(OVERLAP, frame)
        resolutions = {
            frozenset({row.campaign_a, row.campaign_b}): row.resolution for row in draft.resolutions
        }

        slate_refs = {ref for entry in slate for ref in entry.get("campaign_refs") or []}
        brand_terms = [str(term.get("term")) for term in brand.get("brand_terms") or []]
        pmax_running = draft.pmax_allowed and any(
            entry.get("campaign_type") == "performance_max" for entry in slate
        )

        return AutomationBoundariesOutput(
            pmax=PmaxBoundary(
                allowed=draft.pmax_allowed,
                included_themes=draft.included_themes,
                excluded_urls=draft.excluded_urls,
                brand_exclusion_required=_brand_exclusion_required(slate, draft.pmax_allowed),
                # A brand term the brand campaign owns must be an account-level
                # negative on every automated surface, whether or not PMax is
                # the surface running today.
                account_negatives=sorted({*draft.account_negatives, *brand_terms}),
            ),
            broad_match=BroadMatchBoundary(
                # Broad match on a campaign that is not in the slate is a
                # guardrail for something that will not exist.
                allowed_campaigns=[ref for ref in draft.broad_match_campaigns if ref in slate_refs],
                guardrails=draft.broad_match_guardrails,
            ),
            overlap=[
                ChannelOverlap(
                    campaign_a=row["campaign_a"],
                    campaign_b=row["campaign_b"],
                    overlap_pct=row["overlap_pct"],
                    a_shared_pct=row["a_shared_pct"],
                    b_shared_pct=row["b_shared_pct"],
                    shared=row["shared"],
                    resolution=resolutions.get(frozenset({row["campaign_a"], row["campaign_b"]})),
                )
                for row in overlaps.value.get("pairs", [])
            ],
            notes=_notes(draft.notes, pmax_running=pmax_running),
            status="ok",
            calc_evidence_ids=[overlaps.id],
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _plan_block(ctx: RunContext) -> str:
    """What this plan is being built from. Configuration, not evidence."""
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
    return "\n".join(lines)


def _agreed_refs(ctx: RunContext) -> set[str]:
    """The campaigns gate G1 fixed. 2.3.1 may choose channels, not campaigns."""
    return {
        str(row.get("campaign_ref"))
        for row in ctx.output_of("2.1.3").get("objectives") or []
        if row.get("campaign_ref")
    }


def _kept_entries(
    slate: Sequence[SlateEntryDraft], agreed: set[str]
) -> tuple[list[SlateEntryDraft], list[str]]:
    kept: list[SlateEntryDraft] = []
    dropped: list[str] = []
    for entry in slate:
        unknown = [ref for ref in entry.campaign_refs if ref not in agreed]
        known = [ref for ref in entry.campaign_refs if ref in agreed]
        if unknown:
            dropped.append(
                f"{_key(entry.campaign_type, entry.market)} named "
                f"{', '.join(sorted(unknown))}, which gate G1 did not agree"
            )
        if known:
            kept.append(entry.model_copy(update={"campaign_refs": known}))
    return kept, dropped


def _placement(slate: Sequence[SlateEntryDraft]) -> _Placement:
    """`(campaign_ref, market) -> the slate entry that carries it`.

    Exact pairs first. A campaign funded in a market no entry enumerated falls
    back to the entry that names it, but **only** when exactly one entry does:
    the slate assigns channels to campaigns, so a campaign appearing once has
    only one channel it could run as, and a campaign appearing twice is a
    genuine question about which market's line this is. Ambiguity is left
    unplaced and reported rather than resolved by position.
    """
    pairs: dict[tuple[str, str], str] = {}
    by_ref: dict[str, set[str]] = {}
    for entry in slate:
        label = _key(entry.campaign_type, entry.market)
        for ref in entry.campaign_refs:
            pairs[(ref, entry.market)] = label
            by_ref.setdefault(ref, set()).add(label)
    return _Placement(
        pairs, {ref: next(iter(labels)) for ref, labels in by_ref.items() if len(labels) == 1}
    )


class _Placement(dict):  # type: ignore[type-arg]
    """`dict` of exact `(ref, market)` pairs, with a single-entry fallback by ref."""

    def __init__(self, pairs: dict[tuple[str, str], str], by_ref: dict[str, str]) -> None:
        super().__init__(pairs)
        self.by_ref = by_ref

    def label(self, ref: str, market: str) -> str:
        found = self.get((ref, market))
        return found if found is not None else self.by_ref.get(ref, "")


def _unplaced(allocation: Sequence[Mapping[str, Any]], placement: _Placement) -> list[str]:
    seen: list[str] = []
    for line in allocation:
        ref, market = str(line.get("campaign_ref") or ""), str(line.get("market") or "")
        label = f"{ref} ({market})"
        if not placement.label(ref, market) and label not in seen:
            seen.append(label)
    return seen


def _figure(
    by_group: Mapping[str, Mapping[str, Any]], entry: SlateEntryDraft, field_name: str
) -> float:
    row = by_group.get(_key(entry.campaign_type, entry.market))
    return float(row[field_name]) if row else 0.0


def _brand_ref(ctx: RunContext, agreed: set[str]) -> str | None:
    """The agreed campaign whose objective is brand defence, if there is one.

    Read off 2.1.3's own `objective` rather than guessed from a name, so a
    campaign called `defence` in German is still found.
    """
    for row in ctx.output_of("2.1.3").get("objectives") or []:
        if row.get("objective") == "brand_defense" and str(row.get("campaign_ref")) in agreed:
            return str(row["campaign_ref"])
    return None


def _notes(notes: str, *, pmax_running: bool) -> str:
    """`+` on a field read fails `check_calc_isolation.py` even for strings.

    The guard cannot tell a concatenated sentence from a computed figure, and
    the right answer is to keep both out of a node rather than to teach the
    guard an exception it would then have to get right every time.
    """
    if pmax_running:
        return notes
    return f"{notes} PMax is not in the slate."


def _brand_exclusion_required(slate: Sequence[Mapping[str, Any]], pmax_allowed: bool) -> bool:
    """Q9's default, and not the model's to relax.

    True whenever a channel that blends brand with acquisition is in the slate.
    It stays true when PMax is merely *allowed* rather than scheduled, because
    the boundary is what someone reads before turning it on.
    """
    if pmax_allowed:
        return True
    return any(str(entry.get("campaign_type")) in BRAND_EXCLUSION_REQUIRED for entry in slate)


def _competition(plan: Any) -> dict[str, Any]:
    landscape = plan.input.competitive_landscape
    return {
        "competitors": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in (getattr(landscape, "competitors", None) or [])[:10]
        ],
        "differentiation": getattr(landscape, "differentiation_claim", None),
    }


def _readiness(plan: Any) -> dict[str, Any]:
    return {
        "launch_blockers": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in (plan.input.launch_blockers or [])[:20]
        ],
        "readiness": plan.input.readiness.model_dump(mode="json")
        if hasattr(plan.input.readiness, "model_dump")
        else {},
    }


def _brand_candidates(plan: Any) -> list[dict[str, Any]]:
    """The priced terms most likely to be the brand, for the model to judge.

    Not a decision: the shortlist is every term that contains a word from the
    project's own domain, and the model says which of them are actually ours.
    """
    stem = (plan.input.product_context or {}).get("brand") or ""
    terms = [
        {"term": keyword.term, "volume": keyword.volume, "market": keyword.market}
        for keyword in plan.input.priced_keyword_list[:200]
    ]
    if not stem:
        return terms[:50]
    return [row for row in terms if str(stem).casefold() in str(row["term"]).casefold()][:50]


channel_slate = ChannelSlateNode()
automation_boundaries = AutomationBoundariesNode()
brand_isolation = BrandIsolationNode()
