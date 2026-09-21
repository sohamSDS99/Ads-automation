"""Which tests a plan could run, assembled from what the plan already decided.

The Stage 2.5 analogue of `planning/demand.py`, and here for the same two
reasons: joining and averaging are arithmetic, so they cannot live in
`nodes/plan/`; and nothing here is a figure the plan asserts, so it does not
belong in `agent/calc/` either. Every number this module computes is an
**observation** that becomes a formula's input and is recorded verbatim in
`PlanCalc.inputs`.

**The backlog is derived, not brainstormed.** A model asked to "suggest some
tests" returns the same seven ideas for every account, because it is drawing on
what is usually true rather than on what this plan decided. So the candidates
come from the plan's own unresolved tensions, each rule naming the upstream
finding that raised it:

| Variable | Raised by |
|---|---|
| `bid_strategy` | 2.2.2 gave the campaign `switch_strategy`, or a `marginal` verdict |
| `budget` | 2.2.4 capped the line at absorption, or funded it below the floor |
| `match_type` | 2.3.2 allows broad match on this campaign |
| `landing_page` | the campaign carries the largest budget behind one URL |
| `audience` | the slate carries remarketing and a market may lawfully be targeted |
| `geo` | one campaign spans more than one location |
| `ad_schedule` | the campaign clears its learning threshold, so it can afford to split |

A campaign with none of those tensions produces no candidate, which is the
correct answer: an account where nothing is in question does not need a test
backlog, it needs traffic.

The model's job on top of this is the part only judgement can supply — the
hypothesis, and the three ICE ratings. It never sizes a test and never funds
one.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

#: Days a 30-day forecast covers. Used once, to turn a monthly click forecast
#: into the daily rate `power.sample_size_v1` divides by.
FORECAST_DAYS = 30.0

#: How many landing-page tests the backlog proposes. The rule would otherwise
#: fire on every campaign, and a backlog of forty identical page tests is a
#: list nobody reads.
LANDING_PAGE_TESTS = 3

#: Campaign types whose whole premise is an audience list.
REMARKETING_TYPES = frozenset({"display_remarketing", "demand_gen", "video_remarketing"})

#: What a test is measured on, by what it changes. `primary_metric` in §11.
METRIC_BY_VARIABLE: Mapping[str, str] = {
    "bid_strategy": "cpa",
    "budget": "conv_volume",
    "match_type": "cpa",
    "landing_page": "cvr",
    "audience": "cpa",
    "geo": "cpa",
    "ad_schedule": "cpa",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    """One testable tension, before anybody has written a hypothesis about it."""

    id: str
    campaign_ref: str
    campaign_name: str
    market: str
    variable: str
    primary_metric: str
    #: The upstream finding that raised this candidate, in one sentence. Shown
    #: to the model so the hypothesis argues from the plan rather than from
    #: general practice, and carried into the output as `basis`.
    basis: str
    baseline_cvr_pct: float | None
    clicks_per_day: float | None
    avg_cpc_usd: float | None
    monthly_budget_usd: float | None
    launch_wave: int | None
    landing_url: str | None = None

    def as_row(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "campaign_ref": self.campaign_ref,
            "variable": self.variable,
            "primary_metric": self.primary_metric,
            "baseline_cvr_pct": self.baseline_cvr_pct,
            "clicks_per_day": self.clicks_per_day,
            "avg_cpc_usd": self.avg_cpc_usd,
            "launch_wave": self.launch_wave,
        }


@dataclass(slots=True)
class Backlog:
    """Every candidate the plan raised, and what could not be sized."""

    candidates: list[Candidate] = field(default_factory=list)
    #: Tensions that exist but carry no forecast, so no test can be sized for
    #: them. `not_yet[]` in §11, and named rather than dropped.
    unsizable: list[dict[str, str]] = field(default_factory=list)

    def by_id(self) -> dict[str, Candidate]:
        return {candidate.id: candidate for candidate in self.candidates}

    def sizing_frame(self, *, default_mde_pct: float, arms: int = 2) -> pd.DataFrame:
        """What `power.sample_size_v1` consumes."""
        return pd.DataFrame(
            [
                {
                    "id": candidate.id,
                    "baseline_cvr_pct": candidate.baseline_cvr_pct,
                    "mde_pct": default_mde_pct,
                    "arms": arms,
                    "clicks_per_day": candidate.clicks_per_day,
                }
                for candidate in self.candidates
            ]
        )


def build(
    *,
    structure: Mapping[str, Any],
    allocation: Mapping[str, Any],
    capacity: Mapping[str, Any],
    slate: Mapping[str, Any],
    boundaries: Mapping[str, Any] | None = None,
    consent_markets: Sequence[str] = (),
) -> Backlog:
    """Every candidate this plan's own decisions raise.

    Every argument is an upstream node output as `ctx.output_of` returns it —
    plain dicts, not models — because a node may only read another node's
    output through that door, and re-validating it here would couple 2.5.3 to
    four other nodes' contracts for no gain.
    """
    campaigns = _campaigns(structure)
    lines = _by_ref(allocation.get("allocation") or [], "campaign_ref")
    capacities = _by_ref(capacity.get("campaigns") or [], "campaign_ref")
    waves = _waves(slate)
    broad_match_allowed = set(
        ((boundaries or {}).get("broad_match") or {}).get("allowed_campaigns") or []
    )
    remarketing_planned = _has_remarketing(slate)

    backlog = Backlog()
    ranked_by_budget = sorted(
        campaigns.values(),
        key=lambda row: (
            -(_float(row.get("monthly_budget_usd")) or 0.0),
            str(row.get("name") or ""),
        ),
    )
    landing_page_refs = {
        str(row.get("campaign_ref"))
        for row in ranked_by_budget[:LANDING_PAGE_TESTS]
        if _first_landing_url(row)
    }

    for ref in sorted(campaigns):
        campaign = campaigns[ref]
        line = lines.get(ref, {})
        capacity_row = capacities.get(ref, {})
        observed = _observations(line, capacity_row)

        for variable, basis in _tensions(
            campaign=campaign,
            line=line,
            capacity=capacity_row,
            broad_match_allowed=broad_match_allowed,
            landing_page_refs=landing_page_refs,
            remarketing_planned=remarketing_planned,
            consent_markets=consent_markets,
        ):
            candidate = Candidate(
                id=f"{ref}:{variable}",
                campaign_ref=ref,
                campaign_name=str(campaign.get("name") or ref),
                market=str(campaign.get("market") or line.get("market") or ""),
                variable=variable,
                primary_metric=METRIC_BY_VARIABLE[variable],
                basis=basis,
                baseline_cvr_pct=observed["baseline_cvr_pct"],
                clicks_per_day=observed["clicks_per_day"],
                avg_cpc_usd=observed["avg_cpc_usd"],
                monthly_budget_usd=_float(campaign.get("monthly_budget_usd")),
                launch_wave=waves.get(ref),
                landing_url=_first_landing_url(campaign) if variable == "landing_page" else None,
            )
            if candidate.baseline_cvr_pct is None or candidate.clicks_per_day is None:
                backlog.unsizable.append(
                    {
                        "test": f"{candidate.campaign_name}: {variable.replace('_', ' ')}",
                        "blocked_by": (
                            "no conversion or click forecast for this campaign, so there is no "
                            "baseline to measure a lift against"
                        ),
                    }
                )
                continue
            backlog.candidates.append(candidate)

    return backlog


# ---------------------------------------------------------------------------
# the rules
# ---------------------------------------------------------------------------


def _tensions(
    *,
    campaign: Mapping[str, Any],
    line: Mapping[str, Any],
    capacity: Mapping[str, Any],
    broad_match_allowed: set[str],
    landing_page_refs: set[str],
    remarketing_planned: bool,
    consent_markets: Sequence[str],
) -> list[tuple[str, str]]:
    """Which variables are in question for one campaign, and why."""
    ref = str(campaign.get("campaign_ref") or "")
    found: list[tuple[str, str]] = []

    remedy = str(capacity.get("remedy") or "")
    verdict = str(capacity.get("verdict") or "")
    if remedy == "switch_strategy" or verdict == "marginal":
        recommended = str(capacity.get("bid_strategy_recommended") or "a different bid strategy")
        current = str(campaign.get("bid_strategy") or "its current strategy")
        found.append(
            (
                "bid_strategy",
                f"2.2.2 called this campaign {verdict or 'marginal'} on learning capacity"
                + (f" with the remedy `{remedy}`" if remedy else "")
                + f"; it is planned on {current} and {recommended} is the recommendation.",
            )
        )

    if _flag(line.get("cap_applied")):
        found.append(
            (
                "budget",
                "2.2.4 capped this line at what the forecast says the demand can absorb, so "
                "more budget is a hypothesis rather than a certainty.",
            )
        )
    elif _flag(line.get("below_floor")):
        found.append(
            (
                "budget",
                "2.2.4 funded this line below the per-campaign minimum, so whether it can "
                "work at this budget is untested.",
            )
        )

    if ref in broad_match_allowed:
        found.append(
            (
                "match_type",
                "2.3.2 permits broad match on this campaign under guardrails, which is a "
                "reach-versus-precision trade nobody has measured here.",
            )
        )

    if ref in landing_page_refs:
        found.append(
            (
                "landing_page",
                "this campaign carries one of the three largest budgets in the plan behind a "
                "single landing page, so the page is the highest-leverage variable in it.",
            )
        )

    if remarketing_planned and consent_markets:
        market = str(campaign.get("market") or "")
        if market and market in set(consent_markets):
            found.append(
                (
                    "audience",
                    f"the slate carries a remarketing channel and {market} has a lawful basis "
                    "recorded at gate 1.5.3, so an audience-list variant is testable here.",
                )
            )

    if len(campaign.get("locations") or []) > 1:
        found.append(
            (
                "geo",
                f"this campaign targets {len(campaign.get('locations') or [])} locations under "
                "one budget, so the per-location cost is unknown.",
            )
        )

    if verdict == "clears":
        found.append(
            (
                "ad_schedule",
                "2.2.2 says this campaign clears its learning threshold, so it has the "
                "conversion volume to afford splitting the day.",
            )
        )

    return found


# ---------------------------------------------------------------------------
# observations — the inputs a formula hashes, never a figure the plan asserts
# ---------------------------------------------------------------------------


def _observations(line: Mapping[str, Any], capacity: Mapping[str, Any]) -> dict[str, float | None]:
    """Baseline conversion rate, daily clicks and cost per click for one campaign.

    Read off 2.2.4's allocation line, which is itself a `PlanCalc` result, and
    falling back to 2.2.2's capacity row where the allocation carries no click
    forecast. Nothing is invented: where neither has a figure the answer is
    None, and the candidate becomes `not_yet` rather than being sized against
    a made-up baseline.
    """
    conversions = _float(line.get("est_conv"))
    clicks = _float(line.get("est_clicks"))
    if clicks is None:
        clicks = _float(capacity.get("forecast_clicks_30d"))
    if conversions is None:
        conversions = _float(capacity.get("forecast_conv_30d"))

    baseline: float | None = None
    if conversions is not None and clicks is not None and clicks > 0 and conversions > 0:
        baseline = round(conversions / clicks * 100, 4)
        if baseline >= 100:
            # More conversions than clicks is a forecast artefact, not a 100%
            # landing page. Refusing it here keeps `power.sample_size_v1` from
            # having to decide what a >=100% baseline means.
            baseline = None

    per_day: float | None = None
    if clicks is not None and clicks > 0:
        per_day = round(clicks / FORECAST_DAYS, 4)

    cpc = _float(line.get("avg_cpc_usd"))
    if cpc is None and clicks and clicks > 0:
        spend = _float(line.get("usd"))
        if spend is not None and spend > 0:
            cpc = round(spend / clicks, 4)

    return {"baseline_cvr_pct": baseline, "clicks_per_day": per_day, "avg_cpc_usd": cpc}


# ---------------------------------------------------------------------------
# small readers
# ---------------------------------------------------------------------------


def _campaigns(structure: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for row in structure.get("campaigns") or []:
        if not isinstance(row, dict):
            continue
        ref = str(row.get("campaign_ref") or row.get("name") or "").strip()
        if ref:
            found[ref] = row
    return found


def _by_ref(rows: Iterable[Any], key: str) -> dict[str, dict[str, Any]]:
    """Index rows by one key, keeping the first of a duplicate.

    First rather than last because 2.2.4's allocation may carry one line per
    campaign per funnel stage; the largest is written first by `allocation.
    split_v1`, and a test measured against the smallest slice of a campaign
    would be sized for traffic the test will not see.
    """
    found: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        ref = str(row.get(key) or "").strip()
        if ref and ref not in found:
            found[ref] = row
    return found


def _waves(slate: Mapping[str, Any]) -> dict[str, int]:
    """Earliest launch wave per campaign ref, from the slate agreed at G4."""
    found: dict[str, int] = {}
    for entry in slate.get("slate") or []:
        if not isinstance(entry, dict):
            continue
        wave = entry.get("launch_wave")
        if not isinstance(wave, int):
            continue
        for ref in entry.get("campaign_refs") or []:
            key = str(ref)
            if key not in found or wave < found[key]:
                found[key] = wave
    return found


def _has_remarketing(slate: Mapping[str, Any]) -> bool:
    return any(
        str((entry or {}).get("campaign_type") or "") in REMARKETING_TYPES
        for entry in slate.get("slate") or []
        if isinstance(entry, dict)
    )


def _first_landing_url(campaign: Mapping[str, Any]) -> str | None:
    for group in campaign.get("ad_groups") or []:
        if isinstance(group, dict) and group.get("landing_url"):
            return str(group["landing_url"])
    return None


def _float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _flag(value: Any) -> bool:
    return bool(value) and value not in ("false", "False", "0", 0)
