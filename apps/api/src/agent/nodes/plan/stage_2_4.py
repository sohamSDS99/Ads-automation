"""Stage 2.4 — how the account is organised (Stage 02 PRD §11).

Three nodes, no gate. By the time 2.4 runs every decision a human owes has been
made: the targets at G1, the lead definition at G2, the money at G3 and the
channels at G4. What is left is construction, and construction should be
mechanical — which is why almost nothing here is asked of the model.

**What the model decides: two strings.** An ad group's `theme` and its
`primary_message`. Everything else is computed or looked up:

* which keywords exist, and which ad group each belongs to — `structure.grouping_v1`
* what each campaign may spend — `allocation.share_v1` over the split signed at G3
* what each campaign is called — `planning/naming.py` rendering 2.4.1's pattern
* which bid strategy it runs — read off node 2.2.2's verdict
* whether the result is shippable — `structure.volume_check_v1` over the built tree

**Brand is partitioned before grouping, not after.** `grouping_v1` puts two
terms with the same intent and the same landing page in one ad group, and
`sds manager` and `sds software` are exactly that pair. Grouping first and
splitting after would mean either an ad group that spans both campaigns or a
brand term sitting in a non-brand one — a blocking critique at 2.6.2 that no
negative keyword can undo. So 2.3.3's brand terms split the keyword frame and
each half is grouped on its own.

**An automated channel gets no ad groups and no structure floors.** Display,
Demand Gen, Video and Performance Max have no keyword targeting; building ad
groups for them would be a plan nobody can import, and counting their zero ad
groups against `min_ad_groups_per_campaign` would report every one of them as
structurally broken for being what it is.

**Node 2.4.3 completes the vocabulary S2-P1 deliberately left open.**
`structure.volume_check_v1` emits a `remedy` in node 2.2.2's words and says in
its own docstring that mapping those to 2.4.3's `action` is left to this node.
`ACTION_BY_REMEDY` below is that mapping, and `split` — which no remedy
produces — is reserved for the one structural fault the remedy vocabulary
cannot express: a campaign whose ad groups span more than one market. Location
targeting, budget and bid strategy are all campaign-level in Google Ads, so
such a campaign cannot be built however well it forecasts.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from agent.db.models import Evidence, RunStage
from agent.llm.router import TaskClass
from agent.nodes import gather, prompts
from agent.nodes.base import LLMNode, NodeSpec, RunContext
from agent.nodes.plan.stage_2_3 import AUTOMATED_TARGETING, CampaignType, MatchType
from agent.planning import demand, naming, structure

GROUPING = "structure.grouping_v1"
SHARE = "allocation.share_v1"
VOLUME_CHECK = "structure.volume_check_v1"

#: The live account's campaign names, for 2.4.1's collision check. `campaign_perf`
#: is date-segmented, so a campaign with no activity in the window may be absent
#: — which is why a clean collision report is reported as `checked` rather than
#: as proof that nothing collides. §10.2's dedicated `account_snapshot`
#: operation would close that gap and is not in this phase.
CAMPAIGN_PERF = demand.CAMPAIGN_PERF

Status = Literal["ok", "insufficient_input"]
Verdict = Literal["clears", "marginal", "below"]
Action = Literal["ship", "merge_into", "split", "defer"]
StructureVerdict = Literal["sound", "needs_merge", "too_thin"]

#: Node 2.2.2's remedy vocabulary, in node 2.4.3's. `merge` and `broaden` are
#: both "this is not a campaign yet, fold it into one that is"; `switch_strategy`
#: and `raise_budget` are bidding and budget changes that do not stop it
#: shipping, and travel with it as a named remedy so 2.6.2's check 6 passes.
ACTION_BY_REMEDY: dict[str, Action] = {
    "merge": "merge_into",
    "broaden": "merge_into",
    "defer_to_wave_2": "defer",
    "switch_strategy": "ship",
    "raise_budget": "ship",
}


# ---------------------------------------------------------------------------
# 2.4.1 — naming_convention
# ---------------------------------------------------------------------------


class NamingToken(BaseModel):
    token: str = Field(min_length=1)
    allowed_values: list[str] = Field(default_factory=list)
    source: str = Field(min_length=1)


class NamingDraft(BaseModel):
    """What 2.4.1 asks for: the patterns and the vocabulary. No regex."""

    patterns: dict[str, str] = Field(min_length=1)
    tokens: list[NamingToken] = Field(min_length=1)
    examples: list[str] = Field(default_factory=list)
    notes: str = Field(min_length=1)


class NamingCollision(BaseModel):
    proposed_name: str
    existing_name: str
    conflict_type: str
    resolution: str


class NamingConventionOutput(BaseModel):
    """2.4.1 — the convention, the regex that enforces it, and what it clashes with.

    Carries no figure, so it declares no `calc_evidence_ids`: a naming
    convention is a vocabulary, and there is nothing here for a formula to have
    computed.
    """

    patterns: dict[str, str]
    tokens: list[NamingToken]
    validator_regex: str
    examples: list[str]
    collisions: list[NamingCollision] = Field(default_factory=list)
    #: PRD §18: with no Google Ads account there is no collision report at all,
    #: rather than an empty one that reads like a clean bill of health.
    collision_check: Literal["checked", "skipped"]
    notes: str
    status: Status
    open_gaps: list[str] = Field(default_factory=list)
    coverage: list[str] = Field(default_factory=list)


class NamingConventionNode(LLMNode):
    """2.4.1 — the convention every generated name is then held to."""

    spec = NodeSpec(
        id="2.4.1",
        name="naming_convention",
        stage="2.4",
        run_stage=RunStage.PLAN,
        depends_on=("2.3.1",),
        task_class=TaskClass.EXTRACT,
        input_model=BaseModel,
        output_model=NamingConventionOutput,
        connectors=("google_ads",),
        calc=(),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        found = await gather.collect(
            ctx,
            gather.Need(
                CAMPAIGN_PERF,
                connector="google_ads",
                # A project with no Google Ads account still gets a convention.
                # It then gets no collision report, which §18 requires to be
                # visible rather than silently empty.
                optional=True,
                limit=5_000,
            ),
        )
        ctx.scratch[self.spec.id] = found
        return found.evidence

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        found = ctx.scratch.get(self.spec.id)
        existing = _live_campaign_names(ev)
        draft = await ctx.complete(
            NamingDraft,
            system=prompts.system_prompt(
                "You design the naming convention for a Google Ads account: the patterns "
                "and the vocabulary each placeholder may take. You write patterns like "
                "`{market} | {channel} | {brand_split}`, never a regular expression — the "
                "regex is compiled from your patterns."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "the channel slate agreed at gate G4 (every name must describe one of these)",
                    ctx.output_of("2.3.1").get("slate") or [],
                ),
                prompts.computed_block(
                    "campaign names already in the live account", sorted(existing)[:100]
                ),
                "TASK\n"
                "  Give `patterns` for at least `campaign` and `ad_group`, each a string of "
                "`{token}` placeholders and literal separators. Give the `tokens` you used, "
                "each with its `allowed_values` where the vocabulary is closed and its "
                "`source`. Leave `allowed_values` empty for a free token such as `theme`. "
                "Add `examples` and `notes`.",
            ),
            task_class=self.spec.task_class,
        )

        vocabulary = {token.token: token.allowed_values for token in draft.tokens}
        # Raises `NamingError` on a pattern the vocabulary cannot satisfy, which
        # fails the node. A convention nothing can be validated against is worse
        # than no convention: §12 invariant 5 would then pass vacuously.
        regex = naming.compile_validator(draft.patterns, vocabulary)
        proposed = _campaign_names(ctx, draft.patterns)

        return NamingConventionOutput(
            patterns=draft.patterns,
            tokens=draft.tokens,
            validator_regex=regex,
            # The node's own rendered names, not the model's suggestions: an
            # example that fails the convention's own regex is the one thing an
            # example must never be.
            examples=proposed[:6] or draft.examples,
            collisions=[
                NamingCollision(**row) for row in naming.collisions(proposed, sorted(existing))
            ]
            if existing
            else [],
            collision_check="checked" if existing else "skipped",
            notes=draft.notes,
            status="ok",
            open_gaps=[] if existing else ["account_snapshot_unavailable"],
            coverage=gather.coverage_notes(found) if found is not None else [],
        )


# ---------------------------------------------------------------------------
# 2.4.2 — account_structure
# ---------------------------------------------------------------------------


class AdGroupLabel(BaseModel):
    """What the model is asked for per ad group: two strings, no figures."""

    key: str = Field(min_length=1)
    theme: str = Field(min_length=1)
    primary_message: str = Field(min_length=1)


class StructureDraft(BaseModel):
    ad_groups: list[AdGroupLabel]
    account_negatives: list[str] = Field(default_factory=list)
    notes: str = Field(min_length=1)


class PlannedKeyword(BaseModel):
    term: str
    match_type: MatchType
    forecast_cpc_usd: float
    search_volume: int


class PlannedAdGroup(BaseModel):
    name: str
    theme: str
    landing_url: str
    primary_message: str
    keywords: list[PlannedKeyword] = Field(default_factory=list)
    negatives: list[str] = Field(default_factory=list)
    coherence: float
    market: str


class PlannedCampaign(BaseModel):
    name: str
    campaign_ref: str
    type: CampaignType
    market: str
    language: str | None = None
    monthly_budget_usd: float
    daily_budget_usd: float
    bid_strategy: str
    target: float | None = None
    locations: list[str] = Field(default_factory=list)
    ad_groups: list[PlannedAdGroup] = Field(default_factory=list)
    negatives: list[str] = Field(default_factory=list)


class AccountStructureOutput(BaseModel):
    """2.4.2 — the campaign, ad group and keyword tree a person could import."""

    campaigns: list[PlannedCampaign]
    account_negatives: list[str] = Field(default_factory=list)
    orphan_terms: list[str] = Field(default_factory=list)
    coherence_scores: list[dict[str, Any]] = Field(default_factory=list)
    #: Both empty in a sound structure, and both named rather than counted so
    #: the thing to fix is in the output instead of in a log.
    duplicate_terms: list[str] = Field(default_factory=list)
    invalid_names: list[str] = Field(default_factory=list)
    notes: str
    status: Status
    open_gaps: list[str] = Field(default_factory=list)
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class AccountStructureNode(LLMNode):
    """2.4.2 — the tree everything after this stage is written into."""

    spec = NodeSpec(
        id="2.4.2",
        name="account_structure",
        stage="2.4",
        run_stage=RunStage.PLAN,
        # §11's edge list is `2.4.2<-{2.3.1, 2.3.3, 2.4.1, 2.2.4}`. 2.2.2 is
        # added because the bid strategy is read off its verdict rather than
        # chosen here, and an undeclared dependency is one `ctx.output_of` may
        # refuse. No wave changes: 2.2.4 already depends on 2.2.2.
        depends_on=("2.3.1", "2.3.3", "2.4.1", "2.2.4", "2.2.2"),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=AccountStructureOutput,
        connectors=(),
        calc=(GROUPING, SHARE),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        plan = ctx.require_plan()
        approved = ctx.output_of("2.2.4")
        allocation = list(approved.get("allocation") or [])
        envelope = float((approved.get("envelope") or {}).get("monthly_cap_usd") or 0.0)
        convention = ctx.output_of("2.4.1")
        patterns = dict(convention.get("patterns") or {})
        validator = re.compile(str(convention.get("validator_regex") or ".*"))
        brand = ctx.output_of("2.3.3")
        brand_terms = [str(term.get("term")) for term in brand.get("brand_terms") or []]
        brand_ref = str((brand.get("brand_campaign") or {}).get("campaign_ref") or "")
        capacity = {
            str(row.get("campaign_ref")): row
            for row in ctx.output_of("2.2.2").get("campaigns") or []
        }

        rows = _campaign_rows(ctx, brand_ref)
        grouped, orphans, gaps, calc_ids = await _group_per_campaign(
            ctx, rows, brand_terms=brand_terms
        )

        draft = await ctx.complete(
            StructureDraft,
            system=prompts.system_prompt(
                "You name and position ad groups that have already been formed. You are "
                "given each ad group's landing page, its intent and the terms in it; you "
                "return a short `theme` and one line of `primary_message`. You never move "
                "a keyword, never create an ad group and never write a figure."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "the ad groups, already formed (final — label these, do not change them)",
                    _ad_groups_for_prompt(grouped),
                ),
                prompts.computed_block(
                    "the brand policy from 2.3.3, which is why brand sits in its own campaign",
                    {"brand_terms": brand_terms, "brand_campaign_ref": brand_ref},
                ),
                "TASK\n"
                "  For every ad group above give its `key` back unchanged, a short `theme` "
                "in title case suitable for a campaign name, and one line of "
                "`primary_message` — what an ad in this group should promise. Then list "
                "any `account_negatives` that should never be bought anywhere.",
            ),
            task_class=self.spec.task_class,
        )

        labels = {item.key: item for item in draft.ad_groups}
        shares = await plan.calc.run(
            SHARE,
            structure.share_frame(allocation, label=_share_label),
            envelope_usd=envelope,
        )
        by_group = {str(row["group"]): row for row in shares.value.get("groups", [])}

        campaigns = [
            _campaign(
                row,
                patterns=patterns,
                labels=labels,
                grouped=grouped.get(_ref_key(row), []),
                money=by_group.get(_ref_key(row), {}),
                capacity=capacity.get(row["campaign_ref"], {}),
                brand_terms=brand_terms if row["campaign_ref"] != brand_ref else [],
                account_negatives=draft.account_negatives,
            )
            for row in rows
        ]

        names = [
            *(campaign.name for campaign in campaigns),
            *(group.name for campaign in campaigns for group in campaign.ad_groups),
        ]
        terms = [
            keyword.term
            for campaign in campaigns
            for group in campaign.ad_groups
            for keyword in group.keywords
        ]

        return AccountStructureOutput(
            campaigns=campaigns,
            account_negatives=sorted(set(draft.account_negatives)),
            orphan_terms=orphans,
            coherence_scores=[
                {
                    "campaign": campaign.campaign_ref,
                    "ad_group": group.name,
                    "coherence": group.coherence,
                }
                for campaign in campaigns
                for group in campaign.ad_groups
            ],
            duplicate_terms=sorted({term for term in terms if terms.count(term) > 1}),
            invalid_names=[name for name in names if not validator.match(name)],
            notes=draft.notes,
            status="ok",
            open_gaps=gaps,
            calc_evidence_ids=[*calc_ids, shares.id],
        )


# ---------------------------------------------------------------------------
# 2.4.3 — structure_volume_check
# ---------------------------------------------------------------------------


class VerdictDraft(BaseModel):
    """What 2.4.3 asks for: prose about a verdict that is already computed."""

    notes: str = Field(min_length=1)
    risks: list[str] = Field(default_factory=list)


class CampaignCheck(BaseModel):
    ref: str
    name: str
    ad_group_count: int | None = None
    keyword_count: int | None = None
    forecast_clicks_30d: float | None = None
    forecast_conv_30d: float
    budget_to_cpc_ratio: float | None = None
    threshold: float
    verdict: Verdict
    remedy: str | None = None
    action: Action
    reason: str


class StructureVolumeCheckOutput(BaseModel):
    """2.4.3 — whether the tree 2.4.2 built is one that should launch."""

    campaigns: list[CampaignCheck]
    structure_verdict: StructureVerdict
    notes: str
    risks: list[str] = Field(default_factory=list)
    excluded: list[dict[str, Any]] = Field(default_factory=list)
    status: Status
    calc_evidence_ids: list[uuid.UUID] = Field(min_length=1)


class StructureVolumeCheckNode(LLMNode):
    """2.4.3 — the last mechanical check before the plan is written."""

    spec = NodeSpec(
        id="2.4.3",
        name="structure_volume_check",
        stage="2.4",
        run_stage=RunStage.PLAN,
        depends_on=("2.4.2", "2.2.4", "2.2.2"),
        task_class=TaskClass.CLASSIFY,
        input_model=BaseModel,
        output_model=StructureVolumeCheckOutput,
        connectors=(),
        calc=(VOLUME_CHECK,),
    )

    async def gather(self, ctx: RunContext) -> list[Evidence]:
        return []

    async def reason(self, ctx: RunContext, ev: list[Evidence]) -> BaseModel:
        plan = ctx.require_plan()
        built = list(ctx.output_of("2.4.2").get("campaigns") or [])
        approved = ctx.output_of("2.2.4")
        capacity = list(ctx.output_of("2.2.2").get("campaigns") or [])

        frame = structure.built_campaign_frame(
            built,
            allocation=list(approved.get("allocation") or []),
            capacity=capacity,
            automated=AUTOMATED_TARGETING,
        )
        checked = await plan.calc.run(VOLUME_CHECK, frame)
        by_ref = {str(row.get("campaign_ref")): row for row in built}
        rows = [
            _check(row, by_ref.get(str(row["campaign_ref"]), {}))
            for row in checked.value.get("campaigns", [])
        ]

        draft = await ctx.complete(
            VerdictDraft,
            system=prompts.system_prompt(
                "You write the note that accompanies a structural verdict that has already "
                "been computed. You never change a verdict and never write a figure."
            ),
            user=prompts.compose(
                prompts.project_block(ctx.project),
                prompts.computed_block(
                    "the computed verdict per campaign (final)",
                    [row.model_dump(mode="json") for row in rows],
                ),
                "TASK\n"
                "  Write `notes` explaining what this structure is and where it is weakest, "
                "and list the `risks` a reader should know about before launch.",
            ),
            task_class=self.spec.task_class,
        )

        return StructureVolumeCheckOutput(
            campaigns=rows,
            structure_verdict=_structure_verdict(rows, checked.value),
            notes=draft.notes,
            risks=draft.risks,
            excluded=[dict(row) for row in checked.result.excluded],
            status="ok",
            calc_evidence_ids=[checked.id],
        )


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _live_campaign_names(evidence: Sequence[Evidence]) -> set[str]:
    return {
        str(row.payload.get("campaign")).strip()
        for row in evidence
        if row.kind == CAMPAIGN_PERF and row.payload.get("campaign")
    }


def _campaign_rows(ctx: RunContext, brand_ref: str = "") -> list[dict[str, Any]]:
    """One row per campaign the slate puts in the account, in slate order.

    A campaign is a `campaign_ref` in a `market`: Google Ads holds location
    targeting, the budget and the bid strategy at campaign level, so one
    campaign cannot serve two markets and `nonbrand` running in US and DE is
    two campaigns, not one.
    """
    rows: list[dict[str, Any]] = []
    for entry in ctx.output_of("2.3.1").get("slate") or []:
        campaign_type = str(entry.get("campaign_type") or "")
        market = str(entry.get("market") or "")
        for ref in entry.get("campaign_refs") or []:
            rows.append(
                {
                    "campaign_ref": str(ref),
                    "market": market,
                    "campaign_type": campaign_type,
                    "brand_split": "Brand" if str(ref) == brand_ref else "NonBrand",
                    "channel": _channel(campaign_type),
                }
            )
    return rows


def _channel(campaign_type: str) -> str:
    """`performance_max` as a naming token reads `Performance Max`."""
    return campaign_type.replace("_", " ").title()


def _ref_key(row: Mapping[str, Any]) -> str:
    return f"{row['campaign_ref']}|{row['market']}"


def _share_label(line: Mapping[str, Any]) -> str:
    """Budget is per campaign **per market**, matching `_campaign_rows`."""
    return f"{line.get('campaign_ref') or ''}|{line.get('market') or ''}"


def _campaign_names(ctx: RunContext, patterns: Mapping[str, str]) -> list[str]:
    pattern = patterns.get("campaign")
    if not pattern:
        return []
    slate = ctx.output_of("2.3.1")
    brand_ref = str(slate.get("brand_campaign_ref") or "")
    names: list[str] = []
    for row in _campaign_rows(ctx, brand_ref):
        try:
            names.append(naming.render(pattern, {**row, "theme": row["campaign_ref"]}))
        except naming.NamingError:
            # A pattern this campaign cannot fill is reported by the validator
            # at 2.4.2 rather than taking the convention down with it.
            continue
    return names


async def _group_per_campaign(
    ctx: RunContext, rows: Sequence[Mapping[str, Any]], *, brand_terms: Sequence[str]
) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[str], list[uuid.UUID]]:
    """`grouping_v1` once per keyword campaign, over that campaign's own terms."""
    plan = ctx.require_plan()
    grouped: dict[str, list[dict[str, Any]]] = {}
    orphans: list[str] = []
    gaps: list[str] = []
    calc_ids: list[uuid.UUID] = []

    for row in rows:
        if row["campaign_type"] in AUTOMATED_TARGETING:
            continue
        try:
            frame = structure.keyword_frame(
                plan.input.priced_keyword_list,
                demand_map=plan.input.demand_map,
                market=row["market"],
                brand_terms=brand_terms,
            )
        except structure.StructureInputError as exc:
            gaps.append(f"{_ref_key(row)}: {exc}")
            continue

        brand_half, other_half = structure.brand_split(frame)
        mine = brand_half if row["brand_split"] == "Brand" else other_half
        if mine.empty:
            gaps.append(f"{_ref_key(row)}: no keyword in {row['market']} belongs to this campaign")
            continue

        result = await plan.calc.run(GROUPING, mine)
        calc_ids.append(result.id)
        grouped[_ref_key(row)] = [
            {**group, "market": row["market"]} for group in result.value.get("ad_groups", [])
        ]
        orphans.extend(
            f"{item['term']} ({row['market']}): {item['reason']}"
            for item in result.value.get("orphans", [])
        )
    return grouped, sorted(set(orphans)), gaps, calc_ids


def _ad_groups_for_prompt(grouped: Mapping[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {
            "key": group["key"],
            "campaign": ref,
            "landing_url": group["landing_url"],
            "intent": group["intent_label"],
            "computed_theme": group["theme"],
            "terms": [item["term"] for item in group.get("keywords", [])][:12],
        }
        for ref, groups in sorted(grouped.items())
        for group in groups
    ]


def _campaign(
    row: Mapping[str, Any],
    *,
    patterns: Mapping[str, str],
    labels: Mapping[str, AdGroupLabel],
    grouped: Sequence[Mapping[str, Any]],
    money: Mapping[str, Any],
    capacity: Mapping[str, Any],
    brand_terms: Sequence[str],
    account_negatives: Sequence[str],
) -> PlannedCampaign:
    return PlannedCampaign(
        name=_render(patterns.get("campaign"), {**row, "theme": row["campaign_ref"]}),
        campaign_ref=str(row["campaign_ref"]),
        type=row["campaign_type"],
        market=str(row["market"]),
        monthly_budget_usd=float(money.get("usd") or 0.0),
        daily_budget_usd=float(money.get("daily_usd") or 0.0),
        # Read off 2.2.2's verdict, never chosen here: two answers to "can this
        # campaign run tCPA" is how a plan contradicts its own media plan.
        bid_strategy=str(capacity.get("bid_strategy_recommended") or "manual_cpc"),
        target=money.get("target_cpa_usd"),
        locations=[str(row["market"])],
        ad_groups=[
            _ad_group(group, patterns=patterns, labels=labels, row=row) for group in grouped
        ],
        # Every brand term is a negative in every campaign that is not the brand
        # campaign. PRD §11 check 5 is what this is for.
        negatives=sorted({*brand_terms, *account_negatives}),
    )


def _ad_group(
    group: Mapping[str, Any],
    *,
    patterns: Mapping[str, str],
    labels: Mapping[str, AdGroupLabel],
    row: Mapping[str, Any],
) -> PlannedAdGroup:
    label = labels.get(str(group["key"]))
    theme = label.theme if label else str(group["theme"])
    return PlannedAdGroup(
        name=_render(patterns.get("ad_group"), {**row, "theme": theme}),
        theme=theme,
        landing_url=str(group["landing_url"]),
        primary_message=label.primary_message if label else "",
        keywords=[
            PlannedKeyword(
                term=item["term"],
                match_type=item["match_type"],
                forecast_cpc_usd=item["forecast_cpc_usd"],
                search_volume=item["search_volume"],
            )
            for item in group.get("keywords", [])
        ],
        coherence=float(group["coherence"]),
        market=str(group.get("market") or row["market"]),
    )


def _render(pattern: str | None, values: Mapping[str, Any]) -> str:
    """A name the convention could not produce is reported, not guessed at.

    Falling back to the raw ref rather than raising keeps one unfillable
    pattern from taking the whole structure down; `invalid_names` then carries
    it, and §12 invariant 5 fails the plan at validation with the name in hand.
    """
    if not pattern:
        return str(values.get("campaign_ref") or values.get("theme") or "")
    try:
        return naming.render(pattern, values)
    except naming.NamingError:
        return str(values.get("theme") or values.get("campaign_ref") or "")


def _check(row: Mapping[str, Any], built: Mapping[str, Any]) -> CampaignCheck:
    markets = _markets(built)
    remedy, action, reason = _action(row, markets=markets)
    return CampaignCheck(
        ref=str(row["campaign_ref"]),
        name=str(built.get("name") or row["campaign_ref"]),
        ad_group_count=row.get("ad_group_count"),
        keyword_count=row.get("keyword_count"),
        forecast_clicks_30d=row.get("forecast_clicks_30d"),
        forecast_conv_30d=row["forecast_conv_30d"],
        budget_to_cpc_ratio=row.get("budget_to_cpc_ratio"),
        threshold=row["threshold"],
        verdict=row["verdict"],
        remedy=remedy,
        action=action,
        reason=reason,
    )


def _markets(built: Mapping[str, Any]) -> set[str]:
    """Every market this campaign's ad groups actually serve."""
    own = str(built.get("market") or "")
    found = {
        str(group.get("market") or own)
        for group in built.get("ad_groups") or []
        if str(group.get("market") or own)
    }
    return found or ({own} if own else set())


def _action(row: Mapping[str, Any], *, markets: set[str]) -> tuple[str | None, Action, str]:
    """S2-P1 left this mapping to 2.4.3 by name. This is it.

    Ordered so the fault that makes a campaign unbuildable is reported ahead of
    the ones that merely make it a bad idea.
    """
    remedy = row.get("remedy")
    if len(markets) > 1:
        return (
            remedy,
            "split",
            f"its ad groups span {', '.join(sorted(markets))}, and location targeting, "
            "budget and bid strategy are all campaign-level in Google Ads",
        )
    if row.get("below_ad_group_floor"):
        return (
            remedy or "merge",
            "merge_into",
            "it is below the minimum ad-group count, so it is a theme rather than a campaign",
        )
    if remedy is None:
        return None, "ship", "it clears its learning threshold and is within the structure floors"
    return remedy, ACTION_BY_REMEDY[str(remedy)], f"the computed remedy is {remedy}"


def _structure_verdict(
    rows: Sequence[CampaignCheck], computed: Mapping[str, Any]
) -> StructureVerdict:
    """The calc's verdict, unless the built structure is worse than the money was.

    `volume_check_v1` judges what it was given; a campaign that has to be split
    or merged is a structural fault it has no vocabulary for, so the verdict is
    widened here rather than the formula being taught about markets.
    """
    if any(row.verdict == "below" for row in rows):
        return "too_thin"
    if any(row.action in {"merge_into", "split"} for row in rows):
        return "needs_merge"
    verdict = str(computed.get("structure_verdict") or "sound")
    if verdict == "too_thin":
        return "too_thin"
    if verdict == "needs_merge":
        return "needs_merge"
    return "sound"


naming_convention = NamingConventionNode()
account_structure = AccountStructureNode()
structure_volume_check = StructureVolumeCheckNode()
