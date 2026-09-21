"""Whether a structure can actually learn, and how keywords group (PRD §9.2).

Two formulas that between them decide whether an account layout is shippable.

`volume_check_v1` answers the question that kills most small accounts: a
campaign forecast to produce nine conversions a month will never leave Smart
Bidding's learning period, so it spends its budget learning nothing. Every
threshold it tests against comes from `planning_constants.yaml`, because Google
moves them and a number baked into a function body becomes a silently wrong plan
six months later (global law 15).

`grouping_v1` assigns keywords to ad groups. It is a **partition**: every kept
keyword lands in exactly one group, asserted before the result is returned,
which is what makes PRD §11 critique assertion 4 ("no keyword appears in two ad
groups") true by construction rather than by review. The page map is
authoritative — an ad group is "the terms that should land on this URL with this
intent", not a cluster of similar strings, because the landing page is what the
ad has to promise.

A group under `min_keywords_per_ad_group` is reported as `thin` and **not
merged**. Every merge available to this formula would be wrong: two groups
sharing an intent and a URL are already one bucket, so the only candidates left
differ in intent or in landing page, and folding those together is exactly the
ad group whose ad cannot honestly promise anything. Thinness is a campaign-level
problem, which is why it surfaces as `volume_check_v1`'s `merge` remedy instead.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any

import pandas as pd

from agent.calc import rows
from agent.calc.registry import CalcDraft, CalcError, formula, money, pct, ratio
from agent.planning.constants import PlanningConstants

VOLUME_COLUMNS = ("campaign_ref", "monthly_budget_usd", "forecast_cpa_usd")
GROUPING_COLUMNS = ("term",)

#: Days a "30 day" threshold is measured over. Not a planning constant — it is
#: what "30d" in `tcpa_min_conv_30d` means, and a budget is monthly.
DAYS_30 = 30.0

#: Dropped before a term is themed or compared. Short on purpose: an aggressive
#: stopword list turns "software for small business" and "software for
#: enterprise" into the same theme, which is the opposite of what grouping is
#: for. These are only the words that carry no intent at all.
STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "best", "by", "do", "does",
        "for", "from", "how", "in", "is", "it", "me", "my", "near", "of", "on",
        "or", "our", "the", "to", "top", "vs", "what", "with", "your",
    }
)

_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(term: str) -> frozenset[str]:
    return frozenset(token for token in _TOKEN.findall(term.lower()) if token not in STOPWORDS)


def _normalise(term: str) -> str:
    return " ".join(term.lower().split())


# ---------------------------------------------------------------------------
# volume_check_v1
# ---------------------------------------------------------------------------


@formula("structure.volume_check_v1", kind="calc_structure")
def volume_check_v1(
    campaigns: pd.DataFrame,
    *,
    constants: PlanningConstants,
    envelope_usd: float | None = None,
) -> CalcDraft:
    """Forecast conversions per campaign per 30 days against the learning thresholds.

    `remedy` uses node 2.2.2's vocabulary. Node 2.4.3 reports an `action` over
    the same rows; the mapping is a lookup, not a second calculation, and is
    deliberately left to that node so this formula does not have to guess at a
    vocabulary two stages away:
    `clears -> ship`, `merge -> merge_into`, `defer_to_wave_2 -> defer`, and
    `broaden` / `switch_strategy` / `raise_budget` -> whatever 2.4.3 decides a
    campaign that needs work before launch should be called.
    """
    tcpa_min = constants.get("learning.tcpa_min_conv_30d").value
    troas_min = constants.get("learning.troas_min_conv_30d").value
    absolute_min = constants.get("learning.absolute_min_conv_30d").value
    min_ad_groups = constants.get("structure.min_ad_groups_per_campaign").as_int()
    min_keywords = constants.get("structure.min_keywords_per_ad_group").as_int()
    if not 0 < absolute_min <= tcpa_min <= troas_min:
        raise CalcError(
            "learning thresholds must satisfy 0 < absolute_min_conv_30d <= "
            f"tcpa_min_conv_30d <= troas_min_conv_30d; got {absolute_min}, "
            f"{tcpa_min}, {troas_min}"
        )

    records = rows.records(campaigns, VOLUME_COLUMNS, what="campaigns")
    checked: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for record in records:
        ref = rows.text(record, "campaign_ref", default="(unnamed)")
        try:
            budget = rows.number(record, "monthly_budget_usd")
            forecast_cpa = rows.number(record, "forecast_cpa_usd")
            cpc = rows.number(record, "avg_cpc_usd", default=-1.0)
            ad_groups = rows.number(record, "ad_group_count", default=-1.0)
            keywords = rows.number(record, "keyword_count", default=-1.0)
        except CalcError as exc:
            excluded.append({"campaign_ref": ref, "reason": str(exc)})
            continue
        if budget <= 0 or forecast_cpa <= 0:
            excluded.append(
                {
                    "campaign_ref": ref,
                    "reason": (
                        f"needs a positive monthly_budget_usd and forecast_cpa_usd; "
                        f"got {budget}, {forecast_cpa}"
                    ),
                }
            )
            continue

        has_values = rows.flag(record, "has_revenue_values")
        conv_30d = budget / forecast_cpa
        desired = "troas" if has_values else "tcpa"
        desired_threshold = troas_min if has_values else tcpa_min

        if conv_30d >= desired_threshold:
            verdict = "clears"
        elif conv_30d >= absolute_min:
            verdict = "marginal"
        else:
            verdict = "below"

        if conv_30d >= troas_min and has_values:
            strategy = "troas"
        elif conv_30d >= tcpa_min:
            strategy = "tcpa"
        elif conv_30d >= absolute_min:
            strategy = "max_conv"
        else:
            strategy = "max_clicks"

        needed_budget = desired_threshold * forecast_cpa
        remedy = _remedy(
            verdict=verdict,
            ad_groups=ad_groups,
            keywords=keywords,
            min_ad_groups=min_ad_groups,
            min_keywords=min_keywords,
            needed_budget=needed_budget,
            envelope_usd=envelope_usd,
        )

        checked.append(
            {
                "campaign_ref": ref,
                "monthly_budget_usd": money(budget),
                "forecast_cpa_usd": money(forecast_cpa),
                "forecast_conv_30d": money(conv_30d),
                "forecast_clicks_30d": money(budget / cpc) if cpc > 0 else None,
                "budget_to_cpc_ratio": ratio(budget / DAYS_30 / cpc) if cpc > 0 else None,
                "ad_group_count": int(ad_groups) if ad_groups >= 0 else None,
                "keyword_count": int(keywords) if keywords >= 0 else None,
                "threshold": ratio(desired_threshold),
                "bid_strategy_desired": desired,
                "bid_strategy_recommended": strategy,
                "verdict": verdict,
                "remedy": remedy,
                "needed_budget_usd": money(needed_budget),
                "budget_shortfall_usd": money(max(0.0, needed_budget - budget)),
            }
        )

    if not checked:
        raise CalcError(
            "no campaign could be volume-checked: "
            + "; ".join(f"{row['campaign_ref']}: {row['reason']}" for row in excluded)
        )

    if any(row["verdict"] == "below" for row in checked):
        structure_verdict = "too_thin"
    elif any(row["remedy"] == "merge" for row in checked):
        structure_verdict = "needs_merge"
    else:
        structure_verdict = "sound"

    clears = sum(1 for row in checked if row["verdict"] == "clears")
    return CalcDraft(
        inputs={
            "campaigns": records,
            "envelope_usd": envelope_usd,
            "tcpa_min_conv_30d": tcpa_min,
            "troas_min_conv_30d": troas_min,
            "absolute_min_conv_30d": absolute_min,
            "min_ad_groups_per_campaign": min_ad_groups,
            "min_keywords_per_ad_group": min_keywords,
        },
        result={
            "campaigns": checked,
            "structure_verdict": structure_verdict,
            "clears_count": clears,
            "campaign_count": len(checked),
        },
        summary=(
            f"{clears} of {len(checked)} campaign(s) clear their learning threshold — "
            f"structure is {structure_verdict.replace('_', ' ')}"
        ),
        constants_version=constants.version,
        excluded=excluded,
    )


def _remedy(
    *,
    verdict: str,
    ad_groups: float,
    keywords: float,
    min_ad_groups: int,
    min_keywords: int,
    needed_budget: float,
    envelope_usd: float | None,
) -> str | None:
    """The one thing to change, in the order that fixes the cause not the symptom.

    Structure first: a campaign with two ad groups does not need more money, it
    needs merging. Only once the structure is adequate is the shortfall
    genuinely a budget question, and `defer_to_wave_2` is reserved for the case
    where clearing the threshold would cost more than the entire envelope —
    which is a decision about this campaign's existence, not its budget.
    """
    if verdict == "clears":
        return None
    if 0 <= ad_groups < min_ad_groups:
        return "merge"
    if ad_groups > 0 and 0 <= keywords < min_keywords * ad_groups:
        return "broaden"
    if verdict == "marginal":
        return "switch_strategy"
    if envelope_usd is not None and needed_budget > envelope_usd:
        return "defer_to_wave_2"
    return "raise_budget"


# ---------------------------------------------------------------------------
# grouping_v1
# ---------------------------------------------------------------------------


@formula("structure.grouping_v1", kind="calc_structure")
def grouping_v1(keywords: pd.DataFrame, *, constants: PlanningConstants) -> CalcDraft:
    """Assign keywords to ad groups, score coherence, and list the orphans."""
    min_size = constants.get("structure.min_keywords_per_ad_group").as_int()
    max_size = constants.get("structure.max_keywords_per_ad_group").as_int()
    if not 0 < min_size <= max_size:
        raise CalcError(
            "structure constants must satisfy 0 < min_keywords_per_ad_group <= "
            f"max_keywords_per_ad_group; got {min_size}, {max_size}"
        )

    records = rows.records(keywords, GROUPING_COLUMNS, what="keywords")
    members: dict[str, dict[str, Any]] = {}
    duplicates: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []

    for record in records:
        term = _normalise(rows.text(record, "term"))
        if not term:
            excluded.append({"term": rows.text(record, "term"), "reason": "term is blank"})
            continue
        try:
            volume = rows.number(record, "search_volume", default=0.0)
            cpc = rows.number(record, "forecast_cpc_usd", default=0.0)
        except CalcError as exc:
            excluded.append({"term": term, "reason": str(exc)})
            continue

        candidate = {
            "term": term,
            "match_type": rows.text(record, "match_type", default="phrase"),
            "search_volume": int(round(max(volume, 0.0))),
            "forecast_cpc_usd": money(max(cpc, 0.0)),
            "intent_label": rows.text(record, "intent_label"),
            "landing_url": rows.text(record, "landing_url"),
            "tokens": _tokens(term),
        }
        existing = members.get(term)
        if existing is not None:
            # Keep the better-evidenced duplicate rather than the last one read:
            # the same term arrives from the demand map and the page map with
            # different volumes, and the larger figure is the one that was
            # actually measured.
            winner, loser = (
                (existing, candidate)
                if candidate["search_volume"] <= existing["search_volume"]
                else (candidate, existing)
            )
            members[term] = winner
            duplicates.append({"term": term, "dropped_search_volume": loser["search_volume"]})
            continue
        members[term] = candidate

    buckets: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for member in members.values():
        if not member["landing_url"]:
            orphans.append({"term": member["term"], "reason": "no landing_url in the page map"})
            continue
        if not member["intent_label"]:
            orphans.append({"term": member["term"], "reason": "no intent_label"})
            continue
        buckets.setdefault((member["intent_label"], member["landing_url"]), []).append(member)

    if not buckets:
        raise CalcError(
            f"none of {len(members)} keyword(s) carried both an intent_label and a "
            f"landing_url, so no ad group can be formed"
        )

    ad_groups: list[dict[str, Any]] = []
    for (intent, url), bucket in buckets.items():
        ordered = sorted(bucket, key=lambda item: (-item["search_volume"], item["term"]))
        chunks = _split(ordered, max_size)
        for index, chunk in enumerate(chunks):
            ad_groups.append(
                {
                    "key": f"{intent}|{url}" + (f"|{index + 1}" if len(chunks) > 1 else ""),
                    "intent_label": intent,
                    "landing_url": url,
                    "theme": _theme(chunk),
                    "split_index": index + 1 if len(chunks) > 1 else None,
                    "split_of": len(chunks) if len(chunks) > 1 else None,
                    "keyword_count": len(chunk),
                    "coherence": ratio(_coherence(chunk)),
                    "thin": len(chunk) < min_size,
                    "search_volume": sum(int(item["search_volume"]) for item in chunk),
                    "avg_forecast_cpc_usd": money(
                        rows.total(item["forecast_cpc_usd"] for item in chunk) / len(chunk)
                    ),
                    "keywords": [
                        {
                            "term": item["term"],
                            "match_type": item["match_type"],
                            "search_volume": item["search_volume"],
                            "forecast_cpc_usd": item["forecast_cpc_usd"],
                        }
                        for item in chunk
                    ],
                }
            )

    _assert_partition(ad_groups, expected=len(members) - len(orphans))

    ad_groups.sort(key=lambda group: (-int(group["search_volume"]), str(group["key"])))
    thin = [group["key"] for group in ad_groups if group["thin"]]
    assigned = sum(int(group["keyword_count"]) for group in ad_groups)
    mean_coherence = (
        rows.total(float(group["coherence"]) for group in ad_groups) / len(ad_groups)
    )

    return CalcDraft(
        inputs={"keywords": records, "min_size": min_size, "max_size": max_size},
        result={
            "ad_groups": ad_groups,
            "orphans": orphans,
            "duplicates_collapsed": duplicates,
            "thin_ad_groups": thin,
            "coherence_scores": [
                {"key": group["key"], "coherence": group["coherence"]} for group in ad_groups
            ],
            "mean_coherence": ratio(mean_coherence),
            "ad_group_count": len(ad_groups),
            "assigned_keyword_count": assigned,
            "orphan_count": len(orphans),
            "orphan_pct": pct(len(orphans) / len(members) * 100) if members else 0.0,
        },
        summary=(
            f"{assigned} keyword(s) into {len(ad_groups)} ad group(s) at "
            f"{ratio(mean_coherence):g} mean coherence; {len(orphans)} orphan(s), "
            f"{len(thin)} thin group(s)"
        ),
        constants_version=constants.version,
        excluded=excluded,
    )


def _split(members: list[dict[str, Any]], max_size: int) -> list[list[dict[str, Any]]]:
    """Break an oversized bucket in two on its most discriminating token.

    Splitting on a shared token keeps an ad group's terms saying the same thing,
    which is the whole point of an ad group — one message, one landing page. When
    no token divides the bucket (every term is equally unlike every other), it is
    chunked by search volume instead, so the highest-value terms at least share a
    group rather than being scattered.
    """
    if len(members) <= max_size:
        return [members]

    counts = Counter(
        token for member in members for token in member["tokens"]
    )
    discriminating = [
        token for token, count in counts.items() if 2 <= count < len(members)
    ]
    if not discriminating:
        return [members[start : start + max_size] for start in range(0, len(members), max_size)]

    # Most frequent, then alphabetical — a tie must not depend on dict order.
    token = min(discriminating, key=lambda item: (-counts[item], item))
    with_token = [member for member in members if token in member["tokens"]]
    without = [member for member in members if token not in member["tokens"]]
    return _split(with_token, max_size) + _split(without, max_size)


def _theme(chunk: list[dict[str, Any]]) -> str:
    """The tokens shared by most of the group, as its working theme.

    Not a name. Naming is node 2.4.1's job — it owns the pattern, the token
    vocabulary and the `validator_regex` every generated name has to pass, and a
    theme invented here would collide with that.
    """
    counts = Counter(token for member in chunk for token in member["tokens"])
    if not counts:
        return chunk[0]["term"]
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return " ".join(token for token, _ in ranked[:3])


def _coherence(chunk: list[dict[str, Any]]) -> float:
    """Mean pairwise Jaccard over the group's token sets, 0..1.

    A single-keyword group scores 1.0: it is trivially coherent, and scoring it
    0 would make `thin` and `incoherent` the same signal when they call for
    opposite remedies.
    """
    if len(chunk) < 2:
        return 1.0
    scores: list[float] = []
    for left in range(len(chunk)):
        for right in range(left + 1, len(chunk)):
            first = chunk[left]["tokens"]
            second = chunk[right]["tokens"]
            union = first | second
            scores.append(len(first & second) / len(union) if union else 0.0)
    return rows.total(scores) / len(scores)


def _assert_partition(ad_groups: list[dict[str, Any]], *, expected: int) -> None:
    """Every non-orphan keyword in exactly one group. PRD §11 critique 4.

    Checked here rather than trusted, because the splitter is recursive and an
    off-by-one in it would hand Google Ads the same keyword in two ad groups —
    which self-competes, inflates CPC, and is invisible in a report.
    """
    seen: set[str] = set()
    for group in ad_groups:
        for keyword in group["keywords"]:
            term = str(keyword["term"])
            if term in seen:
                raise CalcError(f"{term!r} was assigned to more than one ad group")
            seen.add(term)
    if len(seen) != expected:
        raise CalcError(f"grouping kept {len(seen)} keyword(s) but {expected} were assignable")
