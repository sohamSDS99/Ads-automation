"""The `CampaignPlan` contract (Stage 02 PRD §12) — the handoff to Stage 03.

The Stage 02 twin of `export/contract.py`, and it inherits that module's two
rules unchanged: the model fills the object and a Jinja2 template renders it,
so six export formats cannot disagree; and a statement without evidence is not
a finding. On top of them Stage 02 adds a third.

**A figure without a calculation is not a number.** `Number` wraps every
headline figure the plan asserts with the `calc_evidence_id` that resolves to
the `PlanCalc` row behind it. §17 PT1 is scoped to exactly these objects —
"100% of `Number` objects resolve to a `PlanCalc` row with `formula_id`,
`inputs_hash` and `calc_version`" — and `check_numbers` below is what proves
it, against the rows of this plan run rather than against a promise.

**Why not every float.** Wrapping all of them was the first design and it was
wrong twice over. A 40-campaign plan carries some 12,000 figures across its
allocation, forecast and keyword tables; wrapping each in a four-field object
triples the JSONB payload for no reader's benefit, because nobody clicks
through to the provenance of one keyword's forecast CPC. And it would have
been *weaker*, not stronger: every one of those tables is projected wholesale
from a node output that already declares `calc_evidence_ids`, so the section
carries the same provenance in one place instead of ten thousand. `Number` is
for the figures a person reads and acts on — the envelope, the ceilings, the
targets, the totals — and §17 PT1 says so by naming `Number` rather than
"every number".

**The interiors are permissive on purpose.** Like `ReportModel`, every record
here declares the handful of fields the renderers and the critique actually
read, and allows extras. §11's node outputs are richer than §12's sketch and
will get richer; a plan run that fails at node 2.6.1 because 2.4.2 grew a
field has lost forty minutes of work to a validation error that cost nobody
anything to allow.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from agent.export.contract import Claim, Confidence

#: Bumped when this contract changes shape. Stage 03 reads it before parsing,
#: and `CampaignPlan.schema_version` records the version a stored plan was
#: built under. Typed as the literal so the field the contract is versioned by
#: cannot be widened to `str` by a default.
PLAN_SCHEMA_VERSION: Final[Literal["1.0"]] = "1.0"

#: §12: the executive summary is capped at 250 words, as in Stage 01.
EXECUTIVE_SUMMARY_MAX_WORDS = 250

#: §12.2's lifecycle, as the *payload* sees it. `superseded` is deliberately
#: absent: it is a fact about a row's relationship to a newer row, which the
#: payload of an immutable frozen plan cannot learn without being rewritten,
#: and law 17 forbids rewriting it. `CampaignPlan.status` on the table carries
#: the full set; this carries what was true when the plan was written.
PlanStatus = Literal["draft", "blocked", "ready_to_freeze", "frozen"]

Unit = Literal["usd", "pct", "count", "days", "months", "ratio"]
GateKey = Literal["G1", "G2", "G3", "G4"]
Severity = Literal["blocking", "warning", "note"]


class PlanModel(BaseModel):
    """Base for every record in the plan. Extras ride along into the JSON."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)


class Number(PlanModel):
    """A figure the plan asserts, with the calculation that produced it.

    `Decimal`, not `float`: an envelope is money, invariant 4 checks the
    allocation sums to it within ±0.5%, and the last thing that argument needs
    is binary floating point deciding that 0.1 + 0.2 misses a cap.

    **`value` is stored in JSONB as a string**, because that is how pydantic
    serialises a `Decimal` — losslessly, and back to a `Decimal` on the way in.
    Anything reading the payload *through this contract* therefore sees a
    `Decimal` and need not care. Anything reading the raw JSONB — a zod schema,
    a diff, Stage 03 — sees `"40000"` and not `40000`, and a type check for
    `int | float` there will silently fall through. The S2-P6c session hit
    exactly that: a stringified value compared against a dict reported a whole
    media plan as rewritten. `tests/test_plan_exports.py` pins both halves.
    """

    value: Decimal
    unit: Unit
    #: Resolves to a `PlanCalc` row of this plan run, and through it to a
    #: `derived` Evidence row. `check_numbers` proves the whole set at once.
    calc_evidence_id: uuid.UUID
    confidence: Confidence = "medium"
    #: What the figure is called where a reader meets it. Not in §12's sketch,
    #: and here because `check_numbers` reports *which* figure failed, and
    #: "a Number in media_plan" is not something anyone can act on.
    label: str = ""

    def __str__(self) -> str:  # pragma: no cover - rendering convenience
        return f"{self.value} {self.unit}"


# ---------------------------------------------------------------------------
# provenance
# ---------------------------------------------------------------------------


class PlanSource(PlanModel):
    """What this plan was planned from (§12, §4).

    The chain back to Stage 01. §12.3 guarantees `research_run_id` is present
    because Stage 03's copy has to respect the differentiation claim and the
    compliance guardrails the research established.
    """

    research_run_id: uuid.UUID
    report_id: uuid.UUID
    acceptance_id: uuid.UUID
    accepted_by: uuid.UUID
    accepted_at: datetime
    research_schema_version: str = ""
    launch_readiness: str = ""
    override_reason: str | None = None
    degraded_sources: list[str] = Field(default_factory=list)

    @field_validator("degraded_sources")
    @classmethod
    def _dedupe_sorted(cls, value: list[str]) -> list[str]:
        return sorted(set(value))


class GateDecision(PlanModel):
    """One of the four approvals, as the plan records it (§12 invariant 3)."""

    gate_key: GateKey
    node_id: str
    name: str = Field(default="", description="The node's name, e.g. `budget_allocation`.")
    status: Literal["approved", "rejected", "pending", "expired"]
    decided_by: uuid.UUID | None = None
    decided_by_name: str = ""
    decided_at: datetime | None = None
    note: str = ""
    #: What the approver changed before approving. Empty on a clean approval,
    #: and the reason G3 is worth auditing when it is not.
    edits_applied: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_approved(self) -> bool:
        return self.status == "approved"


class Dependency(PlanModel):
    """Something that must happen before this plan can run (§12).

    Carried from a Stage 01 `launch_blocker` or raised by 2.5.1/2.5.2. A
    `blocking` dependency is what stops a freeze, so it always names an owner
    — `unassigned` is a legitimate owner and an anonymous blocker is not.
    """

    task: str = Field(min_length=1)
    owner: str = "unassigned"
    blocking: bool = False
    source: str = Field(default="", description="Where it came from: a node id or `research`.")
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# objectives — 2.1.1, 2.1.2, 2.1.3, 2.1.4
# ---------------------------------------------------------------------------


class ConversionAction(PlanModel):
    """2.1.1. What counts as a conversion, and what it is worth."""

    name: str
    ads_action_id: str | None = None
    category: str = ""
    counting: str = ""
    value_model: str = ""
    assigned_value_usd: float | None = None
    lead_to_won_rate_pct: float | None = None
    rank: int | None = None
    primary: bool = False
    include_in_conversions: bool = True
    rationale: str = ""


class SegmentCeiling(PlanModel):
    """2.1.2. What one segment can afford to pay, per lead and per customer."""

    segment: str
    acv_usd: float | None = None
    gross_margin_pct: float | None = None
    lead_to_won_pct: float | None = None
    max_cpa_won_usd: float | None = None
    max_cpl_usd: float | None = None
    target_cpl_usd: float | None = None
    target_roas: float | None = None
    payback_months: float | None = None


class CampaignObjective(PlanModel):
    """2.1.3. What one campaign is optimising toward, and what it may not exceed."""

    campaign_ref: str
    objective: str
    primary_kpi: str
    target_value: float | None = None
    ceiling_value: float | None = None
    basis: str = ""
    ramp: list[dict[str, Any]] = Field(default_factory=list)
    confidence: Confidence = "medium"
    evidence_ids: list[uuid.UUID] = Field(default_factory=list)


class QualifiedLead(PlanModel):
    """2.1.4. What sales agreed counts as a lead worth paying for."""

    required_signals: list[str] = Field(default_factory=list)
    disqualifiers: list[str] = Field(default_factory=list)
    scoring: list[dict[str, Any]] = Field(default_factory=list)
    threshold: int | None = None


class Objectives(PlanModel):
    """§12's `objectives` — stage 2.1 in one section."""

    north_star_metric: str = ""
    north_star_target: Number | None = None
    north_star_period: str = ""
    conversion_actions: list[ConversionAction] = Field(default_factory=list)
    deprecate: list[dict[str, Any]] = Field(default_factory=list)
    unit_economics: list[SegmentCeiling] = Field(default_factory=list)
    blended_max_cpl: Number | None = None
    blended_target_cpl: Number | None = None
    blended_max_cpa_won: Number | None = None
    method_notes: str = ""
    campaign_objectives: list[CampaignObjective] = Field(default_factory=list)
    qualified_lead: QualifiedLead | None = None
    expected_mql_to_sql_pct: float | None = None
    sla_response_hours: int | None = None
    routing: list[dict[str, Any]] = Field(default_factory=list)
    observed_rejection_reasons: list[str] = Field(default_factory=list)
    #: The `derived` rows every figure in this section resolves to. Section
    #: level rather than per figure — see the module docstring.
    calc_evidence_ids: list[uuid.UUID] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# media plan — 2.2.*
# ---------------------------------------------------------------------------


class ForecastRow(PlanModel):
    """2.2.1. One cluster, one market, one month."""

    cluster: str = ""
    market: str = ""
    month: str = ""
    impressions: float | None = None
    ctr_pct: float | None = None
    clicks: float | None = None
    avg_cpc_usd: float | None = None
    cvr_pct: float | None = None
    conversions: float | None = None
    cost_usd: float | None = None
    cpa_usd: float | None = None


class AllocationLine(PlanModel):
    """2.2.4. One campaign's share of the envelope, and what it buys."""

    campaign_ref: str
    market: str = ""
    funnel_stage: str = ""
    usd: float = 0.0
    pct: float = 0.0
    forecast_cpa_usd: float | None = None
    target_cpa_usd: float | None = None
    est_clicks: float | None = None
    est_conv: float | None = None
    floor_applied: bool = False
    cap_applied: bool = False
    below_floor: bool = False


class Scenario(PlanModel):
    """2.2.3. One of the three costed envelopes a budget owner chose between."""

    name: str
    monthly_total_usd: float = 0.0
    quarterly_total_usd: float | None = None
    est_clicks: float | None = None
    est_conv: float | None = None
    est_cpa: float | None = None
    est_pipeline_usd: float | None = None
    payback_months: float | None = None
    allocation: list[AllocationLine] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class Envelope(PlanModel):
    """2.2.4. What the plan commits to spend. The figure invariant 4 checks."""

    monthly_cap: Number
    quarterly_cap: Number | None = None
    currency: str = "USD"
    unallocated_usd: float = 0.0
    unallocated_reason: str | None = None


class ReallocationRule(PlanModel):
    """2.2.5. When money moves between campaigns without a new plan."""

    id: str
    trigger_metric: str = ""
    comparison: str = ""
    threshold: float | None = None
    lookback_days: float | None = None
    from_campaign: str = ""
    to_campaign: str = ""
    max_shift_pct: float | None = None
    cooldown_days: float | None = None
    requires_human: bool = True
    rationale: str = ""


class MediaPlan(PlanModel):
    """§12's `media_plan` — stage 2.2 in one section."""

    chosen_scenario: str = ""
    rationale: str = ""
    what_would_change_it: str = ""
    envelope: Envelope | None = None
    experiment_reserve: Number | None = None
    allocation: list[AllocationLine] = Field(default_factory=list)
    scenarios: list[Scenario] = Field(default_factory=list)
    forecast: list[ForecastRow] = Field(default_factory=list)
    forecast_method: str = ""
    forecast_confidence_band: dict[str, Any] = Field(default_factory=dict)
    impression_share_headroom_pct: float | None = None
    learning_warnings: list[dict[str, Any]] = Field(default_factory=list)
    reallocation_rules: list[ReallocationRule] = Field(default_factory=list)
    review_cadence: str = ""
    degraded_sources: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(default_factory=list)

    def allocated_usd(self) -> Decimal:
        """The allocation total, in `Decimal`, for invariant 4."""
        return sum((Decimal(str(line.usd)) for line in self.allocation), Decimal(0))


# ---------------------------------------------------------------------------
# channel slate — 2.3.*
# ---------------------------------------------------------------------------


class SlateEntry(PlanModel):
    """2.3.1. One campaign type in one market, and when it launches."""

    campaign_type: str
    market: str = ""
    campaign_refs: list[str] = Field(default_factory=list)
    launch_wave: int | None = None
    rationale: str = ""
    entry_criteria: list[str] = Field(default_factory=list)
    exit_criteria: list[str] = Field(default_factory=list)
    prerequisites: list[str] = Field(default_factory=list)
    est_share_of_budget_pct: float | None = None


class BrandIsolation(PlanModel):
    """2.3.3. Which terms are ours, and where they may not be bid on."""

    brand_terms: list[dict[str, Any]] = Field(default_factory=list)
    brand_campaign_ref: str = ""
    match_types: list[str] = Field(default_factory=list)
    budget_pct: float | None = None
    negatives_for_nonbrand: list[str] = Field(default_factory=list)
    reporting_rule: str = ""
    competitor_bidding_policy: str = ""


class AutomationBoundaries(PlanModel):
    """2.3.2. What Google's automation is allowed to decide for itself."""

    pmax: dict[str, Any] = Field(default_factory=dict)
    broad_match: dict[str, Any] = Field(default_factory=dict)
    overlap: list[dict[str, Any]] = Field(default_factory=list)


class ChannelSlate(PlanModel):
    """§12's `channel_slate` — stage 2.3 in one section."""

    slate: list[SlateEntry] = Field(default_factory=list)
    rejected: list[dict[str, Any]] = Field(default_factory=list)
    brand_isolation: BrandIsolation | None = None
    automation_boundaries: AutomationBoundaries | None = None
    notes: str = ""
    calc_evidence_ids: list[uuid.UUID] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# account structure — 2.4.*
# ---------------------------------------------------------------------------


class PlannedKeyword(PlanModel):
    """One keyword, as it will be imported. The EDITOR_CSV row."""

    term: str
    match_type: str = "phrase"
    forecast_cpc_usd: float | None = None
    search_volume: int | None = None


class PlannedAdGroup(PlanModel):
    """§12.3 item 1: the container Stage 03 writes every ad into."""

    name: str
    theme: str = ""
    landing_url: str = ""
    primary_message: str = ""
    market: str = ""
    keywords: list[PlannedKeyword] = Field(default_factory=list)
    negatives: list[str] = Field(default_factory=list)
    coherence: float | None = None


class PlannedCampaign(PlanModel):
    """One campaign, complete enough to build."""

    name: str
    campaign_ref: str = ""
    type: str = ""
    market: str = ""
    language: str | None = None
    monthly_budget_usd: float | None = None
    daily_budget_usd: float | None = None
    bid_strategy: str = ""
    target: float | None = None
    locations: list[str] = Field(default_factory=list)
    ad_groups: list[PlannedAdGroup] = Field(default_factory=list)
    negatives: list[str] = Field(default_factory=list)


class NamingConvention(PlanModel):
    """2.4.1. The patterns every generated name is checked against."""

    patterns: dict[str, str] = Field(default_factory=dict)
    tokens: list[dict[str, Any]] = Field(default_factory=list)
    validator_regex: str = ""
    examples: list[str] = Field(default_factory=list)
    collisions: list[dict[str, Any]] = Field(default_factory=list)
    collision_check: str = ""


class AccountStructure(PlanModel):
    """§12's `account_structure` — stage 2.4 in one section."""

    naming_convention: NamingConvention | None = None
    campaigns: list[PlannedCampaign] = Field(default_factory=list)
    account_negatives: list[str] = Field(default_factory=list)
    orphan_terms: list[str] = Field(default_factory=list)
    volume_check: list[dict[str, Any]] = Field(default_factory=list)
    structure_verdict: str = ""
    notes: str = ""
    #: 2.4.2's own findings, and **three-state on purpose**: `None` means the
    #: structure was never checked, `[]` means it was checked and was clean.
    #: An absent list defaulting to `[]` would let a reader put a green tick
    #: against something nobody verified, which is exactly the case the node
    #: computes these for. 2.6.2 re-derives both from the tree independently
    #: (§11 assertions 4 and 9); these carry what 2.4.2 itself concluded.
    duplicate_terms: list[str] | None = None
    invalid_names: list[str] | None = None
    calc_evidence_ids: list[uuid.UUID] = Field(default_factory=list)

    def counts(self) -> dict[str, int]:
        """Campaigns, ad groups and keywords. §14's EDITOR_CSV acceptance reads these."""
        ad_groups = [group for campaign in self.campaigns for group in campaign.ad_groups]
        return {
            "campaigns": len(self.campaigns),
            "ad_groups": len(ad_groups),
            "keywords": sum(len(group.keywords) for group in ad_groups),
        }


# ---------------------------------------------------------------------------
# measurement plan — 2.5.1, 2.5.2
# ---------------------------------------------------------------------------


class MeasurementPlan(PlanModel):
    """§12's `measurement_plan` — 2.5.1 and 2.5.2 in one section."""

    primary_source: str = ""
    rationale: str = ""
    metric_definitions: list[dict[str, Any]] = Field(default_factory=list)
    reconciliation: list[dict[str, Any]] = Field(default_factory=list)
    known_discrepancies: list[dict[str, Any]] = Field(default_factory=list)
    dashboard_spec: dict[str, Any] = Field(default_factory=dict)
    consent_signal: dict[str, Any] = Field(default_factory=dict)
    gclid_capture: dict[str, Any] = Field(default_factory=dict)
    upload: dict[str, Any] = Field(default_factory=dict)
    stage_map: list[dict[str, Any]] = Field(default_factory=list)
    #: §13 and PC1. The markets gate 1.5.3 permits, and the ones it refuses.
    #: A market in `markets_blocked` may carry no audience dependency, and
    #: assertion 8 of §11's critique fails blocking if it does.
    consent_markets_allowed: list[str] = Field(default_factory=list)
    consent_markets_blocked: list[str] = Field(default_factory=list)
    consent_basis: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# experiment backlog — 2.5.3
# ---------------------------------------------------------------------------


class Experiment(PlanModel):
    """§12's `experiment_backlog[]` — one ranked, sized, funded test."""

    id: str
    hypothesis: str = ""
    campaign_ref: str = ""
    campaign_name: str = ""
    market: str = ""
    variable: str = ""
    primary_metric: str = ""
    baseline: float | None = None
    mde_pct: float | None = None
    required_conv_per_arm: int | None = None
    est_days_to_significance: int | None = None
    impact_1_5: int | None = None
    confidence_1_5: int | None = None
    effort_1_5: int | None = None
    ice_score: float | None = None
    rank: int | None = None
    earliest_wave: int | None = None
    reserve_usd: float = 0.0
    funded: bool = False


# ---------------------------------------------------------------------------
# the plan
# ---------------------------------------------------------------------------


class CampaignPlan(PlanModel):
    """Stage 02 PRD §12, in field names and order.

    The single source of truth for all six exports, and the only thing Stage 03
    may read. If it is not in here it is not in the PDF, the DOCX, the
    markdown, the JSON, the Editor CSV or the workbook.
    """

    schema_version: Literal["1.0"] = PLAN_SCHEMA_VERSION
    project_id: uuid.UUID
    plan_run_id: uuid.UUID
    version: int = 0
    generated_at: datetime

    source: PlanSource
    executive_summary: str = ""
    plan_status: PlanStatus = "draft"

    objectives: Objectives = Field(default_factory=Objectives)
    media_plan: MediaPlan = Field(default_factory=MediaPlan)
    channel_slate: ChannelSlate = Field(default_factory=ChannelSlate)
    account_structure: AccountStructure = Field(default_factory=AccountStructure)
    measurement_plan: MeasurementPlan = Field(default_factory=MeasurementPlan)
    experiment_backlog: list[Experiment] = Field(default_factory=list)

    decisions: list[GateDecision] = Field(default_factory=list)
    open_dependencies: list[Dependency] = Field(default_factory=list)
    assumptions: list[Claim] = Field(default_factory=list)
    risks: list[Claim] = Field(default_factory=list)

    constants_version: str = ""
    cost_usd: Annotated[float, Field(ge=0)] = 0.0
    #: Not in §12's sketch. The critique writes its findings here so that the
    #: exported plan carries its own review — a PDF that says "ready to freeze"
    #: and a critique that said otherwise, living in two places, is how a
    #: blocked plan gets circulated as an approved one.
    critique_issues: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("executive_summary")
    @classmethod
    def _within_word_budget(cls, value: str) -> str:
        words = len(value.split())
        if words > EXECUTIVE_SUMMARY_MAX_WORDS:
            raise ValueError(
                f"executive_summary is {words} words; §12 caps it at {EXECUTIVE_SUMMARY_MAX_WORDS}"
            )
        return value

    # -- the projections every renderer and the critique share ---------------

    @property
    def is_frozen(self) -> bool:
        return self.plan_status == "frozen"

    @property
    def blocking_dependencies(self) -> list[Dependency]:
        return [item for item in self.open_dependencies if item.blocking]

    def numbers(self) -> list[Number]:
        """Every `Number` anywhere in the plan. What §17 PT1 is measured over."""
        return _walk_numbers(self)

    def evidence_ids(self) -> list[uuid.UUID]:
        """Every `evidence_ids` entry, deduped, in first-seen order."""
        return _dedupe(_walk_ids(self.model_dump(mode="python"), "evidence_ids"))

    def calc_evidence_ids(self) -> list[uuid.UUID]:
        """Every `calc_evidence_ids` entry plus every `Number`'s citation."""
        found = _walk_ids(self.model_dump(mode="python"), "calc_evidence_ids")
        found.extend(number.calc_evidence_id for number in self.numbers())
        return _dedupe(found)

    def gate(self, key: GateKey) -> GateDecision | None:
        return next((item for item in self.decisions if item.gate_key == key), None)


def _walk_numbers(node: Any, seen: set[int] | None = None) -> list[Number]:
    """Depth-first collection of `Number` instances, over models and containers.

    Walks the *models* rather than a dump, because a dumped `Number` is
    indistinguishable from any other four-key dict and `check_numbers` has to
    know it is looking at a declared figure.
    """
    seen = seen if seen is not None else set()
    if id(node) in seen:  # pragma: no cover - the contract is a tree
        return []
    found: list[Number] = []
    if isinstance(node, Number):
        return [node]
    if isinstance(node, BaseModel):
        seen.add(id(node))
        for name in type(node).model_fields:
            found.extend(_walk_numbers(getattr(node, name, None), seen))
    elif isinstance(node, dict):
        for value in node.values():
            found.extend(_walk_numbers(value, seen))
    elif isinstance(node, list | tuple):
        for item in node:
            found.extend(_walk_numbers(item, seen))
    return found


def _walk_ids(node: Any, field_name: str) -> list[uuid.UUID]:
    found: list[uuid.UUID] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == field_name and isinstance(value, list):
                found.extend(item for item in value if isinstance(item, uuid.UUID))
            else:
                found.extend(_walk_ids(value, field_name))
    elif isinstance(node, list | tuple):
        for item in node:
            found.extend(_walk_ids(item, field_name))
    return found


def _dedupe(values: list[uuid.UUID]) -> list[uuid.UUID]:
    found: dict[uuid.UUID, None] = {}
    for value in values:
        found.setdefault(value, None)
    return list(found)
