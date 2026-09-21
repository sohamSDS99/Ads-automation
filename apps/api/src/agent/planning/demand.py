"""Stage 01's keyword list, assembled into the frame `forecast.traffic_v1` eats.

The Stage 2.1 analogue of this module is `planning/crm.py`, and it is here for
the same two reasons: grouping and averaging are arithmetic, so they cannot
live in `nodes/plan/`; and nothing in here produces a figure the plan asserts,
so it does not belong in `calc/` either. Every number this module computes is
an **observation** that becomes a formula's input, is recorded verbatim in
`PlanCalc.inputs`, and is hashed into `inputs_hash`. A reader who wants to know
where a forecast came from sees the volume, the CPC and the CTR it was built
from, in the row itself.

**The cluster is the research's own, not a second one.** `PricedKeyword` has no
cluster column, but node 1.4.5 already grouped the terms: `DemandMap.mapping`
carries `term_cluster -> best_url`, and every priced keyword carries the
`best_url` it was mapped to. Joining on that URL recovers Stage 01's grouping
rather than inventing a parallel one — the single most expensive kind of
duplication available here would be two different answers to "which cluster is
this term in", one on the demand map and one on the media plan. Where the join
finds nothing the fallback ladder is `funnel_stage`, then `intent`, then one
named bucket; it never silently drops a term.

**Months come from the seasonality index, not from a clock.** PT3 wants
identical inputs to give byte-identical output, and a forecast labelled with
the months since today would be a different plan every morning. `PricedKeyword.
seasonality_index` is twelve multipliers, January first, so the twelve calendar
months *are* the horizon and the labels are `Jan`..`Dec`. A plan run that
carries no seasonality anywhere gets one row per cluster-market labelled
`typical`, and says so.

**CTR and CVR are measured, or named as unmeasured.** They come from the
account's own `campaign_perf` history, narrowed to search where the channel is
recorded. With no account connected there is no measurement to be had, and PRD
§18 is explicit that 2.2.1 still runs — so the planning defaults are used, and
every row carries the basis that produced it so the gate card can say which.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

import pandas as pd
import structlog

from agent.export.contract import DemandMap, PricedKeyword
from agent.planning.constants import PlanningConstants

log = structlog.get_logger(__name__)

Method = Literal["google_forecast", "derived_arithmetic"]

#: Evidence kinds this module reads. All three are written by the read-only
#: `google_ads` connector; none of them is required.
CAMPAIGN_PERF = "campaign_perf"
KEYWORD_IMPRESSION_SHARE = "keyword_impression_share"
KEYWORD_FORECAST = "keyword_forecast"

#: The label a term with no recoverable cluster is grouped under. Named rather
#: than dropped, for the same reason `crm.UNSEGMENTED` is: a keyword list where
#: nobody mapped the terms is one cluster, not no demand.
UNCLUSTERED = "unclustered"

#: The label a term with no market is grouped under. `PricedKeyword.market` is
#: optional and a single-market project leaves it empty throughout.
UNSPECIFIED_MARKET = "-"

#: The month labels, January first, index-aligned with
#: `PricedKeyword.seasonality_index`. Calendar months rather than dates,
#: because a date needs a clock and a clock breaks PT3.
MONTHS: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

#: The single month a plan with no seasonality data forecasts over. One row per
#: cluster-market, and `scenarios.envelope_v1` divides the total by one month,
#: which is the right answer when nothing tells us how the year moves.
FLAT_MONTH = "typical"

#: `campaign.advertising_channel_type` values whose CTR and CVR are a fair
#: benchmark for a search plan. Display's CTR is an order of magnitude lower and
#: would forecast a tenth of the traffic; Performance Max blends surfaces and
#: cannot be attributed to one. Where the channel is not recorded at all the row
#: is kept — an older pull with no channel column is still our own account.
SEARCH_CHANNELS = frozenset({"SEARCH", "SEARCH_MOBILE_APP", "MULTI_CHANNEL"})

#: Columns `forecast.traffic_v1` requires, plus the optional ones it reads when
#: they are present. Declared here so a change to the formula's contract fails
#: a test in this module rather than at runtime in a node.
FRAME_COLUMNS = (
    "cluster",
    "market",
    "month",
    "ctr_pct",
    "avg_cpc_usd",
    "cvr_pct",
    "seasonality_index",
    "impression_share_target_pct",
    "cpc_low_usd",
    "cpc_high_usd",
    "current_impression_share_pct",
    "keyword_count",
)


@dataclass(frozen=True, slots=True)
class Benchmarks:
    """The click-through and conversion rates a forecast is built on.

    `basis` is the half that reaches the gate card. "account (search, 412k
    impressions)" and "planning default — no account history" produce the same
    arithmetic and deserve very different amounts of trust, and the only place
    that difference can be recorded is next to the number.
    """

    ctr_pct: float
    cvr_pct: float
    basis: str
    measured: bool
    impressions: int = 0
    clicks: int = 0

    @property
    def note(self) -> str:
        return f"CTR {self.ctr_pct:g}% and CVR {self.cvr_pct:g}% from {self.basis}"


@dataclass(frozen=True, slots=True)
class ForecastGroup:
    """One cluster-market, as the Google forecast service is asked about it.

    Carries the terms, because the connector prices a named keyword set, and
    the bid, because a forecast without one is a forecast of nothing in
    particular. The bid is the cluster's own volume-weighted CPC from the
    research — so Google is asked "what does this demand cost at what we
    already know it costs" rather than at a figure somebody picked.
    """

    cluster: str
    market: str
    keywords: tuple[str, ...]
    max_cpc_usd: float
    funnel_stage: str

    def as_params(self, *, language: str | None = None) -> dict[str, Any]:
        """The shape `google_ads.fetch(params={"groups": [...]})` reads."""
        group: dict[str, Any] = {
            "cluster": self.cluster,
            "market": self.market,
            "keywords": list(self.keywords),
            "max_cpc_usd": self.max_cpc_usd,
        }
        if language:
            group["language"] = language
        return group


@dataclass(frozen=True, slots=True)
class DemandBasis:
    """The frame a forecast is computed from, and everything it could not carry.

    Mirrors `crm.SegmentBasis` deliberately: `gaps` names what was missing in
    words a person can act on, `notes` records what is worth stating in the
    method, and `usable` answers the one question the node asks before it
    reaches for `calc/`.
    """

    frame: pd.DataFrame
    method: Method = "derived_arithmetic"
    benchmarks: Benchmarks | None = None
    gaps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    clusters: tuple[str, ...] = ()
    markets: tuple[str, ...] = ()
    months: tuple[str, ...] = ()
    keywords_used: int = 0
    keywords_dropped: int = 0
    #: One per cluster-market, in frame order. What node 2.2.1 hands the
    #: forecast connector, and nothing else reads.
    groups: tuple[ForecastGroup, ...] = ()
    #: Volume-weighted search impression share already held, 0-100, or None
    #: when the account was never pulled. `forecast.traffic_v1` turns it into
    #: the headroom the aggressive scenario is capped by.
    current_impression_share_pct: float | None = None

    @property
    def usable(self) -> bool:
        """Whether `forecast.traffic_v1` can be called on this frame at all."""
        return not self.frame.empty

    @property
    def seasonal(self) -> bool:
        return self.months != (FLAT_MONTH,)


def demand_frame(
    *,
    priced_keywords: Sequence[PricedKeyword],
    demand_map: DemandMap,
    constants: PlanningConstants,
    campaign_perf: Sequence[dict[str, Any]] = (),
    impression_share: Sequence[dict[str, Any]] = (),
    keyword_forecast: Sequence[dict[str, Any]] = (),
) -> DemandBasis:
    """One row per cluster per market per month, priced and rated.

    Four inputs, three sources, and the frame says where each came from:

    * `search_volume` / `impressions` and the CPC range — measured, from the
      research's priced keyword list, or from Google's forecast when it answered.
    * `seasonality_index` — measured, from the same list, volume-weighted.
    * `ctr_pct` and `cvr_pct` — measured from our own account, or the planning
      defaults with `benchmarks.measured` false.
    * `impression_share_target_pct` — configured, from `planning_constants.yaml`.
    """
    gaps: list[str] = []
    notes: list[str] = []

    usable_keywords = [item for item in priced_keywords if _volume(item) > 0]
    dropped = len(priced_keywords) - len(usable_keywords)
    if not priced_keywords:
        gaps.append(
            "priced_keyword_list — the accepted research carries no priced keywords, so "
            "there is no demand to forecast. Re-run stage 1.4 before planning a budget."
        )
        return DemandBasis(frame=pd.DataFrame(), gaps=gaps)
    if not usable_keywords:
        gaps.append(
            f"volume — none of the {len(priced_keywords)} priced keyword(s) carries a search "
            "volume above zero, so impressions cannot be forecast from them."
        )
        return DemandBasis(frame=pd.DataFrame(), gaps=gaps, keywords_dropped=dropped)
    if dropped:
        notes.append(
            f"{dropped} of {len(priced_keywords)} priced keyword(s) carry no search volume "
            "and are excluded from the forecast."
        )

    benchmarks = account_benchmarks(campaign_perf, constants)
    if not benchmarks.measured:
        gaps.append(
            "campaign_perf — this account has no readable click or conversion history, so "
            f"the forecast uses the planning defaults ({benchmarks.note}). Every figure "
            "derived from it is a planning assumption, not a measurement."
        )
    else:
        notes.append(benchmarks.note)

    clusters = _cluster_index(demand_map)
    forecast_by_group = _forecast_by_group(keyword_forecast)
    method: Method = "google_forecast" if forecast_by_group else "derived_arithmetic"

    grouped: dict[tuple[str, str], list[PricedKeyword]] = {}
    for keyword in usable_keywords:
        key = (_cluster_of(keyword, clusters), _market_of(keyword))
        grouped.setdefault(key, []).append(keyword)

    if forecast_by_group:
        covered = sum(1 for key in grouped if key in forecast_by_group)
        notes.append(
            f"Google's forecast service answered for {covered} of {len(grouped)} "
            "cluster-market group(s); the rest are sized from search volume at the "
            f"{constants.get('forecast.impression_share_target_pct').value:g}% "
            "impression-share target."
        )

    seasonal = any(len(item.seasonality_index) == len(MONTHS) for item in usable_keywords)
    months = MONTHS if seasonal else (FLAT_MONTH,)
    if not seasonal:
        notes.append(
            "no priced keyword carries a twelve-month seasonality index, so the forecast is "
            "one typical month rather than a year."
        )

    share_target = constants.get("forecast.impression_share_target_pct").value
    held = _impression_share(impression_share)

    records: list[dict[str, Any]] = []
    groups: list[ForecastGroup] = []
    for (cluster, market), members in grouped.items():
        volume = _sum(_volume(item) for item in members)
        funnel = _dominant_funnel(members)
        groups.append(
            ForecastGroup(
                cluster=cluster,
                market=market,
                # Sorted so the same keyword set produces the same request, and
                # so `PlanCalc.inputs_hash` does not move with report ordering.
                keywords=tuple(sorted(item.term for item in members)),
                max_cpc_usd=_weighted(members, _cpc_mid, default=0.0),
                funnel_stage=funnel,
            )
        )
        for index, month in enumerate(months):
            row: dict[str, Any] = {
                "cluster": cluster,
                "market": market,
                "month": month,
                "funnel_stage": funnel,
                "keyword_count": len(members),
                "ctr_pct": benchmarks.ctr_pct,
                "cvr_pct": benchmarks.cvr_pct,
                "impression_share_target_pct": share_target,
                "seasonality_index": (
                    # `month=index` binds the loop variable: without it every
                    # month in the year would read the last month's multiplier,
                    # which is a defect no single-month fixture could show.
                    _weighted(members, lambda item, month=index: _season(item, month), default=1.0)
                    if seasonal
                    else 1.0
                ),
                "current_impression_share_pct": held,
                "avg_cpc_usd": _weighted(members, _cpc_mid, default=0.0),
                "cpc_low_usd": _weighted(members, lambda item: _cpc(item, "cpc_low"), default=0.0),
                "cpc_high_usd": _weighted(
                    members, lambda item: _cpc(item, "cpc_high"), default=0.0
                ),
            }
            row.update(
                _volume_columns(
                    forecast_by_group.get((cluster, market)),
                    volume=volume,
                    share_target=share_target,
                    method=method,
                )
            )
            records.append(row)

    frame = pd.DataFrame.from_records(records)
    # Sorted, not insertion-ordered. `PlanCalc` dedupes on `inputs_hash`, so two
    # runs over the same keywords must hash identically — and a dict's insertion
    # order follows whatever order the research report happened to list terms in.
    frame = frame.sort_values(["cluster", "market"], kind="stable").reset_index(drop=True)
    if not seasonal:
        ordered_months: tuple[str, ...] = (FLAT_MONTH,)
    else:
        ordered_months = MONTHS
    # Row order within a cluster-market must be calendar order, not alphabetical.
    frame = _order_months(frame, ordered_months)

    priced = sum(1 for item in usable_keywords if _cpc_mid(item) > 0)
    if priced == 0:
        gaps.append(
            "cpc_low / cpc_high — no priced keyword carries a cost per click, so the "
            "forecast can size the traffic but not what it costs."
        )
    elif priced < len(usable_keywords):
        notes.append(
            f"{len(usable_keywords) - priced} keyword(s) carry no CPC and contribute volume "
            "at zero cost, which understates the envelope."
        )

    return DemandBasis(
        frame=frame,
        method=method,
        benchmarks=benchmarks,
        gaps=gaps,
        notes=notes,
        clusters=tuple(sorted({str(row["cluster"]) for row in records})),
        markets=tuple(sorted({str(row["market"]) for row in records})),
        months=ordered_months,
        keywords_used=len(usable_keywords),
        keywords_dropped=dropped,
        current_impression_share_pct=held,
        groups=tuple(sorted(groups, key=lambda group: (group.cluster, group.market))),
    )


# ---------------------------------------------------------------------------
# rolling a forecast up — nodes 2.2.2 and 2.2.3
# ---------------------------------------------------------------------------

#: What a campaign with no assignment is grouped under. A cluster the model
#: forgot to place must not vanish out of the media plan: the budget it needs
#: still has to come from somewhere, and an unassigned line on the gate card is
#: how a person finds out.
UNASSIGNED = "unassigned"

#: The funnel stage a cluster whose keywords carry none is grouped under. One
#: allocation line per campaign-market rather than none.
ALL_FUNNEL = "all"

#: Columns `structure.volume_check_v1` requires, plus what 2.2.2 reports beside
#: its verdict.
CAMPAIGN_COLUMNS = (
    "campaign_ref",
    "monthly_budget_usd",
    "forecast_cpa_usd",
    "avg_cpc_usd",
    "has_revenue_values",
)

#: Columns `allocation.split_v1` requires, plus the cap `_absorb_surplus` reads.
UNIT_COLUMNS = (
    "campaign_ref",
    "market",
    "funnel_stage",
    "forecast_cpa_usd",
    "target_cpa_usd",
    "avg_cpc_usd",
    "max_spend_usd",
)

Assignment = Mapping[tuple[str, str], str]


def campaign_frame(
    forecast: Sequence[Mapping[str, Any]],
    *,
    assignments: Assignment,
    month_count: int,
    revenue_campaigns: Collection[str] = (),
) -> pd.DataFrame:
    """The per-campaign frame `structure.volume_check_v1` checks (node 2.2.2).

    `monthly_budget_usd` here is **the forecast's own monthly cost**, not an
    approved budget — there is no approved budget at 2.2.2, the gate that sets
    one is two nodes away. So the question this frame asks is "if we buy the
    demand we forecast, does each campaign clear its learning threshold", which
    is the question that has to be answered *before* anyone is shown a budget
    to approve. Node 2.4.3 asks the same formula the same question against the
    approved allocation, and gets a different row because the inputs differ.

    `ad_group_count` and `keyword_count` are deliberately absent: the structure
    does not exist until 2.4.2, and supplying a zero would make every campaign
    read as needing a merge it has not earned. `_remedy` skips its structural
    branches on an absent count, which is the behaviour that wants.
    """
    if month_count < 1:
        raise ValueError(f"month_count must be at least 1, got {month_count}")

    totals: dict[str, dict[str, float]] = {}
    for row in forecast:
        ref = _assigned(row, assignments)
        entry = totals.setdefault(ref, {"cost": 0.0, "conversions": 0.0, "clicks": 0.0})
        entry["cost"] += _float(row.get("cost_usd"))
        entry["conversions"] += _float(row.get("conversions"))
        entry["clicks"] += _float(row.get("clicks"))

    revenue = {str(item) for item in revenue_campaigns}
    records = [
        {
            "campaign_ref": ref,
            "monthly_budget_usd": round(entry["cost"] / month_count, 2),
            "forecast_cpa_usd": (
                round(entry["cost"] / entry["conversions"], 2) if entry["conversions"] > 0 else 0.0
            ),
            "avg_cpc_usd": (
                round(entry["cost"] / entry["clicks"], 2) if entry["clicks"] > 0 else 0.0
            ),
            "forecast_conversions_total": round(entry["conversions"], 2),
            "has_revenue_values": ref in revenue,
        }
        for ref, entry in sorted(totals.items())
    ]
    return pd.DataFrame.from_records(records)


def allocation_units(
    forecast: Sequence[Mapping[str, Any]],
    *,
    assignments: Assignment,
    targets: Mapping[str, float],
    month_count: int,
    funnel_stages: Mapping[tuple[str, str], str] | None = None,
    headroom_pct: float | None = None,
) -> pd.DataFrame:
    """The per-unit frame `allocation.split_v1` divides an envelope across.

    A unit is `campaign_ref x market x funnel_stage`, which is PRD §11's
    allocation grain and finer than a campaign: one campaign running top- and
    bottom-of-funnel demand in two markets is four lines on the budget owner's
    screen, and four lines is what makes an edit meaningful.

    `max_spend_usd` is the one cap in the frame and it is **measured**: a unit
    can absorb its forecast cost plus whatever share of impressions it does not
    already hold. Without it `split_v1` would happily pour an aggressive
    envelope into the most efficient unit and forecast conversions against
    impressions that are not for sale. With no impression-share measurement
    there is no cap, which is the honest answer rather than a guessed one.
    """
    if month_count < 1:
        raise ValueError(f"month_count must be at least 1, got {month_count}")
    if headroom_pct is not None and headroom_pct < 0:
        raise ValueError(f"headroom_pct must not be negative, got {headroom_pct}")

    totals: dict[tuple[str, str, str], dict[str, float]] = {}
    for row in forecast:
        ref = _assigned(row, assignments)
        cluster = str(row.get("cluster") or "")
        market = str(row.get("market") or UNSPECIFIED_MARKET)
        # `forecast.traffic_v1` emits cluster/market/month and the metrics; the
        # funnel stage is a property of the cluster that the formula has no
        # reason to carry through. It comes back from `DemandBasis.groups`,
        # which is where it was decided.
        key = (ref, market, (funnel_stages or {}).get((cluster, market), ALL_FUNNEL))
        entry = totals.setdefault(key, {"cost": 0.0, "conversions": 0.0, "clicks": 0.0})
        entry["cost"] += _float(row.get("cost_usd"))
        entry["conversions"] += _float(row.get("conversions"))
        entry["clicks"] += _float(row.get("clicks"))

    records: list[dict[str, Any]] = []
    for (ref, market, funnel), entry in sorted(totals.items()):
        monthly_cost = entry["cost"] / month_count
        record: dict[str, Any] = {
            "campaign_ref": ref,
            "market": market,
            "funnel_stage": funnel,
            "forecast_cpa_usd": (
                round(entry["cost"] / entry["conversions"], 2) if entry["conversions"] > 0 else 0.0
            ),
            "target_cpa_usd": round(float(targets.get(ref, 0.0)), 2),
            "avg_cpc_usd": (
                round(entry["cost"] / entry["clicks"], 2) if entry["clicks"] > 0 else 0.0
            ),
            "forecast_monthly_usd": round(monthly_cost, 2),
        }
        if headroom_pct is not None and monthly_cost > 0:
            record["max_spend_usd"] = round(monthly_cost * (1 + headroom_pct / 100), 2)
        records.append(record)
    return pd.DataFrame.from_records(records)


def approved_campaign_frame(
    allocation: Sequence[Mapping[str, Any]],
    *,
    capacity: Sequence[Mapping[str, Any]] = (),
) -> pd.DataFrame:
    """The **approved** split, per campaign, for node 2.2.5's re-check.

    Node 2.2.2 asked `structure.volume_check_v1` whether the *forecast* clears
    each campaign's learning threshold. This asks whether the money a human
    actually signed does. Different inputs, a different `PlanCalc` row, and it
    is the second one a reallocation rule must not push a campaign below —
    because after launch the budget is the approved one, not the forecast.

    The CPA comes from the allocation lines, which carry the forecast CPA each
    line was split on; `has_revenue_values` is carried across from 2.2.2 rather
    than re-derived, so the two checks cannot disagree about whether a campaign
    could run tROAS.
    """
    valued = {
        str(row.get("campaign_ref")): bool(row.get("has_revenue_values"))
        for row in capacity
        if row.get("campaign_ref")
    }
    totals: dict[str, dict[str, float]] = {}
    for line in allocation:
        ref = str(line.get("campaign_ref") or UNASSIGNED)
        usd = _float(line.get("usd"))
        cpa = _float(line.get("forecast_cpa_usd"))
        cpc = _float(line.get("avg_cpc_usd"))
        entry = totals.setdefault(ref, {"usd": 0.0, "conv": 0.0, "clicks": 0.0})
        entry["usd"] += usd
        # Reconstructed from the line's own rate rather than read off `est_conv`
        # so that an approver's edit — which changes `usd` and nothing else —
        # is reflected here without the caller having to recompute anything.
        if cpa > 0:
            entry["conv"] += usd / cpa
        if cpc > 0:
            entry["clicks"] += usd / cpc

    records = [
        {
            "campaign_ref": ref,
            "monthly_budget_usd": round(entry["usd"], 2),
            "forecast_cpa_usd": (
                round(entry["usd"] / entry["conv"], 2) if entry["conv"] > 0 else 0.0
            ),
            "avg_cpc_usd": (
                round(entry["usd"] / entry["clicks"], 2) if entry["clicks"] > 0 else 0.0
            ),
            "has_revenue_values": valued.get(ref, False),
        }
        for ref, entry in sorted(totals.items())
        if entry["usd"] > 0
    ]
    return pd.DataFrame.from_records(records)


def shift_capacity(
    campaigns: Sequence[Mapping[str, Any]], *, max_shift_pct: float
) -> dict[str, dict[str, float]]:
    """How much money each campaign could give up, and still be optimisable.

    Two bounds, and the tighter one wins:

    * **Policy** — `reallocation.max_shift_pct` of the approved budget. A rule
      that can move any amount is not a reallocation rule, it is a re-plan.
    * **Learning** — the surplus over what the campaign needs to clear its
      threshold, which `structure.volume_check_v1` already reports as
      `monthly_budget_usd - needed_budget_usd`. Taking more than that restarts
      a learning period the campaign has not finished, which costs more than
      the money is worth wherever it goes.

    A campaign already at or below its threshold can give nothing, and gets
    zero rather than a small number that reads as permission.
    """
    if max_shift_pct < 0:
        raise ValueError(f"max_shift_pct must not be negative, got {max_shift_pct}")

    capacity: dict[str, dict[str, float]] = {}
    for row in campaigns:
        ref = str(row.get("campaign_ref") or "")
        if not ref:
            continue
        budget = _float(row.get("monthly_budget_usd"))
        needed = _float(row.get("needed_budget_usd"))
        surplus = max(0.0, budget - needed)
        allowed = budget * max_shift_pct / 100
        standard = round(min(surplus, allowed), 2)
        capacity[ref] = {"standard": standard, "half": round(standard / 2, 2)}
    return capacity


def cluster_totals(
    forecast: Sequence[Mapping[str, Any]],
    *,
    funnel_stages: Mapping[tuple[str, str], str] | None = None,
    month_count: int,
) -> list[dict[str, Any]]:
    """The forecast rolled up per cluster-market, for a prompt to read.

    A twelve-month forecast over twenty cluster-markets is 240 rows, and the
    only decision a model makes over it is which *cluster* belongs in which
    campaign. So the months are summed away here — in `planning/`, where adding
    cost rows together is allowed — and the model sees one line per cluster.
    """
    keys = sorted(
        {
            (str(row.get("cluster") or ""), str(row.get("market") or UNSPECIFIED_MARKET))
            for row in forecast
        }
    )
    separator = "\u241f"  # a unit separator: not a character a cluster name holds
    rolled = campaign_frame(
        forecast,
        assignments={key: separator.join(key) for key in keys},
        month_count=month_count,
    )
    stages = funnel_stages or {}
    totals: list[dict[str, Any]] = []
    for record in rolled.to_dict(orient="records"):
        cluster, _, market = str(record["campaign_ref"]).partition(separator)
        totals.append(
            {
                "cluster": cluster,
                "market": market,
                "funnel_stage": stages.get((cluster, market), ALL_FUNNEL),
                "monthly_cost_usd": record["monthly_budget_usd"],
                "forecast_cpa_usd": record["forecast_cpa_usd"],
                "avg_cpc_usd": record["avg_cpc_usd"],
                "forecast_conversions_total": record["forecast_conversions_total"],
            }
        )
    return totals


def shift_percentages(max_shift_pct: float) -> dict[str, float]:
    """The `shift_size` vocabulary, as percentages of the approved budget.

    A ladder rather than a free number: the model picks `standard`, `half` or
    `none`, and the figures behind those words come from
    `planning_constants.yaml`. Law 15 — a policy number lives in the constants
    file, never in a prompt and never in a function body.
    """
    if max_shift_pct < 0:
        raise ValueError(f"max_shift_pct must not be negative, got {max_shift_pct}")
    return {"standard": max_shift_pct, "half": round(max_shift_pct / 2, 4), "none": 0.0}


def _assigned(row: Mapping[str, Any], assignments: Assignment) -> str:
    key = (str(row.get("cluster") or ""), str(row.get("market") or UNSPECIFIED_MARKET))
    return assignments.get(key) or UNASSIGNED


def _dominant_funnel(members: Sequence[PricedKeyword]) -> str:
    """The funnel stage holding most of a cluster's volume.

    Volume-weighted rather than counted, for the same reason the CPC is: a
    cluster is whatever its traffic is, and one high-volume bottom-of-funnel
    term outweighs nine long-tail research terms in what the money buys. Ties
    break alphabetically so the answer does not depend on report ordering.
    """
    volumes: dict[str, float] = {}
    for item in members:
        stage = (item.funnel_stage or "").strip()
        if not stage:
            continue
        volumes[stage] = volumes.get(stage, 0.0) + (_volume(item) or 1.0)
    if not volumes:
        return ALL_FUNNEL
    return min(volumes, key=lambda stage: (-volumes[stage], stage))


# ---------------------------------------------------------------------------
# benchmarks
# ---------------------------------------------------------------------------


def account_benchmarks(
    campaign_perf: Sequence[dict[str, Any]], constants: PlanningConstants
) -> Benchmarks:
    """Our own CTR and CVR, narrowed to search, or the planning defaults.

    Weighted by the denominator each rate is over — impressions for CTR, clicks
    for CVR — rather than as a mean of monthly rates. A mean of rates gives a
    quiet month the same weight as a peak one, which is how a forecast comes to
    disagree with the account it was built from.
    """
    impressions = 0.0
    clicks = 0.0
    conversions = 0.0
    rows = 0
    for row in campaign_perf:
        channel = str(row.get("channel") or "").upper()
        if channel and channel not in SEARCH_CHANNELS:
            continue
        row_impressions = _float(row.get("impressions"))
        row_clicks = _float(row.get("clicks"))
        if row_impressions <= 0 and row_clicks <= 0:
            continue
        impressions += row_impressions
        clicks += row_clicks
        conversions += _float(row.get("conversions"))
        rows += 1

    default_ctr = constants.get("forecast.default_ctr_pct").value
    default_cvr = constants.get("forecast.default_cvr_pct").value
    if rows == 0 or impressions <= 0 or clicks <= 0:
        return Benchmarks(
            ctr_pct=default_ctr,
            cvr_pct=default_cvr,
            basis="the planning defaults — this account has no readable search history",
            measured=False,
        )

    return Benchmarks(
        ctr_pct=round(clicks / impressions * 100, 2),
        cvr_pct=round(conversions / clicks * 100, 2),
        basis=(
            f"this account's own search history — {int(impressions):,} impressions and "
            f"{int(clicks):,} clicks across {rows} campaign-month(s)"
        ),
        measured=True,
        impressions=int(impressions),
        clicks=int(clicks),
    )


def _impression_share(rows: Sequence[dict[str, Any]]) -> float | None:
    """Impression share already held, 0-100, weighted by impressions.

    Google reports `search_impression_share` as a fraction and the connector
    passes it through unscaled, so the conversion to a percentage happens here —
    once, at the boundary, as `_micros` does for money.
    """
    weighted = 0.0
    weight = 0.0
    for row in rows:
        share = _float(row.get("impression_share"))
        impressions = _float(row.get("impressions"))
        if share <= 0 or impressions <= 0:
            continue
        weighted += min(share, 1.0) * impressions
        weight += impressions
    if weight <= 0:
        return None
    return round(weighted / weight * 100, 2)


# ---------------------------------------------------------------------------
# clustering
# ---------------------------------------------------------------------------


def _cluster_index(demand_map: DemandMap) -> dict[str, str]:
    """`best_url -> term_cluster`, from node 1.4.5's own mapping.

    First mapping wins where two clusters claim one URL. A URL serving two
    clusters is a finding about the page rather than about the keywords, and
    re-deciding it here would put a second opinion into the media plan.
    """
    index: dict[str, str] = {}
    for mapping in demand_map.mapping:
        url = _norm(mapping.best_url or "")
        cluster = (mapping.term_cluster or "").strip()
        if url and cluster and url not in index:
            index[url] = cluster
    return index


def _cluster_of(keyword: PricedKeyword, index: dict[str, str]) -> str:
    """Stage 01's cluster for this term, or the honest fallback ladder."""
    url = _norm(keyword.best_url or "")
    if url and url in index:
        return index[url]
    if keyword.funnel_stage and keyword.funnel_stage.strip():
        return keyword.funnel_stage.strip()
    if keyword.intent:
        return str(keyword.intent)
    return UNCLUSTERED


def _market_of(keyword: PricedKeyword) -> str:
    market = (keyword.market or "").strip()
    return market or UNSPECIFIED_MARKET


# ---------------------------------------------------------------------------
# the Google forecast
# ---------------------------------------------------------------------------


def _forecast_by_group(rows: Sequence[dict[str, Any]]) -> dict[tuple[str, str], dict[str, float]]:
    """`keyword_forecast` evidence, keyed by the (cluster, market) it was asked for.

    Per group, not per term, because that is the grain Google answers at:
    `GenerateKeywordForecastMetrics` prices a whole keyword set in one reply and
    there is no read-only way to get the per-keyword breakdown (the connector
    docstring argues that in full). One group forecast twice — the same cluster
    priced at phrase and at exact — is summed rather than overwritten, because
    both rows describe the same demand being bought two ways.
    """
    by_group: dict[tuple[str, str], dict[str, float]] = {}
    for row in rows:
        cluster = str(row.get("cluster") or "").strip()
        if not cluster:
            continue
        market = str(row.get("market") or "").strip() or UNSPECIFIED_MARKET
        impressions = _float(row.get("impressions"))
        clicks = _float(row.get("clicks"))
        if impressions <= 0 and clicks <= 0:
            continue
        entry = by_group.setdefault(
            (cluster, market), {"impressions": 0.0, "clicks": 0.0, "cost": 0.0}
        )
        entry["impressions"] += impressions
        entry["clicks"] += clicks
        entry["cost"] += _float(row.get("cost"))
    return by_group


def _volume_columns(
    forecast: dict[str, float] | None,
    *,
    volume: float,
    share_target: float,
    method: Method,
) -> dict[str, Any]:
    """The volume half of a row, and the rates Google's forecast overrides.

    Under `derived_arithmetic` the only volume column is `search_volume`, and
    `forecast.traffic_v1` applies the impression-share target to it itself.

    Under `google_forecast` the column is `impressions` — Google has already
    answered "impressions we would win", and the formula deliberately does not
    apply the share target a second time. A cluster Google did **not** answer
    for still contributes, but its search volume has to cross the same boundary
    by hand: converted at the impression-share target here, so that one frame
    does not mix "impressions we would win" with "searches that happen" in the
    same column and under-count the forecast cluster by the share factor.
    """
    if method == "derived_arithmetic" or forecast is None:
        columns: dict[str, Any] = {
            "search_volume": round(volume, 2),
            "forecast_basis": "derived",
        }
        if method == "google_forecast":
            columns["impressions"] = round(volume * share_target / 100, 2)
        return columns

    impressions = forecast["impressions"]
    clicks = forecast["clicks"]
    cost = forecast["cost"]
    columns = {
        "impressions": round(impressions, 2),
        "search_volume": round(volume, 2),
        "forecast_basis": "google",
    }
    if impressions > 0 and clicks > 0:
        columns["ctr_pct"] = round(clicks / impressions * 100, 2)
    if clicks > 0 and cost > 0:
        columns["avg_cpc_usd"] = round(cost / clicks, 2)
    return columns


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _order_months(frame: pd.DataFrame, months: tuple[str, ...]) -> pd.DataFrame:
    """Calendar order within each cluster-market, stable across pandas versions."""
    if frame.empty:
        return frame
    order = {month: index for index, month in enumerate(months)}
    frame = frame.assign(_month_order=frame["month"].map(order))
    frame = frame.sort_values(["cluster", "market", "_month_order"], kind="stable").reset_index(
        drop=True
    )
    return frame.drop(columns=["_month_order"])


def _weighted(
    members: Sequence[PricedKeyword],
    value: Any,
    *,
    default: float,
) -> float:
    """Volume-weighted mean of `value(keyword)` over the members that have one.

    Weighted by volume because a cluster's CPC is what its traffic will actually
    cost, not what its terms average out to: one high-volume term at $4 and nine
    long-tail terms at $1 is a $4 cluster, and an unweighted mean would plan it
    at $1.30 and be short by a factor of three.
    """
    weighted = 0.0
    weight = 0.0
    for item in members:
        figure = value(item)
        if figure is None or figure <= 0:
            continue
        volume = _volume(item) or 1.0
        weighted += figure * volume
        weight += volume
    if weight <= 0:
        return default
    return round(weighted / weight, 4)


def _volume(keyword: PricedKeyword) -> float:
    return float(keyword.volume or 0)


def _cpc(keyword: PricedKeyword, field_name: str) -> float:
    value = getattr(keyword, field_name, None)
    return float(value) if value else 0.0


def _cpc_mid(keyword: PricedKeyword) -> float:
    """The midpoint of the research's CPC range, or whichever end it gave."""
    low = _cpc(keyword, "cpc_low")
    high = _cpc(keyword, "cpc_high")
    if low > 0 and high > 0:
        return (low + high) / 2
    return high or low


def _season(keyword: PricedKeyword, index: int) -> float:
    """This keyword's multiplier for one month, or a flat 1.0.

    A flat 1.0 is not a measurement and does not pretend to be: a term with no
    index contributes the neutral multiplier and the cluster's weighted index
    moves toward 1 in proportion to how much of its volume is unmeasured, which
    is exactly the right amount of confidence to lose.
    """
    if len(keyword.seasonality_index) != len(MONTHS):
        return 1.0
    value = keyword.seasonality_index[index]
    return float(value) if value > 0 else 1.0


def _sum(values: Iterable[float]) -> float:
    return float(sum(values))


def _float(value: Any) -> float:
    """A finite float, or zero. NaN reads as absent, never as a quantity."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


def _norm(value: str) -> str:
    return value.strip().lower()
