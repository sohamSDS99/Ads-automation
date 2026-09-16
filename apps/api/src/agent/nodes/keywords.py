"""Every number stage 1.4 reports, computed in Python.

PRD §18 law 3 for the demand half. A keyword universe is set arithmetic, a
demand table is a join, a seasonality index is a ratio and a relevance score is
a token overlap — so none of them are asked of a model. What the model is asked
for is the one thing this module cannot do: what a human meant when they typed
the words.

Two guards here exist because their absence is expensive rather than untidy:

* **A profitable term can never become a negative.** `blocklist` refuses to
  block a term node 1.2.2 measured as converting, whatever the other sources
  say, and reports the conflict instead of resolving it silently. The failure it
  prevents — a keyword that pays for itself, blocked because it also looks like
  a lost-deal pattern — costs money and is invisible once shipped.
* **A term nobody could price is named, not dropped.** `join_demand` returns the
  unpriced terms separately, so "we have 2,000 keywords" and "we have volume for
  2,000 keywords" cannot be confused for each other.
"""

from __future__ import annotations

import re
import uuid
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)

Intent = Literal[
    "transactional", "commercial_investigation", "informational", "navigational", "irrelevant"
]
FunnelStage = Literal["awareness", "consideration", "decision", "none"]
MatchType = Literal["exact", "phrase", "broad"]
NegativeSource = Literal["lost_reasons", "wasteful_terms", "intent_irrelevant"]
Verdict = Literal["good_fit", "weak_fit", "gap"]

#: The universe is capped so one runaway vendor response cannot turn into a
#: 200,000-row output document. PRD §10 1.4.1 targets >= 2,000 seeds; this is
#: well clear of it, and anything dropped is reported.
MAX_TERMS = 25_000

#: Words that carry no topical signal. Deliberately short — a paid-search
#: keyword list is mostly nouns, and an aggressive stoplist would merge
#: "software for chemists" into "software".
STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "best",
        "by",
        "can",
        "do",
        "does",
        "for",
        "from",
        "how",
        "i",
        "in",
        "is",
        "it",
        "my",
        "near",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "their",
        "to",
        "top",
        "vs",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "with",
        "without",
        "you",
        "your",
    }
)

#: Below this a cluster is not a theme, it is a coincidence.
MIN_CLUSTER_SIZE = 3

#: A token has to appear in this share of a cluster's terms before it counts as
#: part of what the cluster is *about*. Without a floor, the long tail poisons
#: the page map: a 200-term cluster carries 200 one-off qualifiers, and "the
#: twelve most common tokens" quietly becomes eleven of those plus the one word
#: that matters.
CLUSTER_TOKEN_SHARE = 0.2

#: Relevance at or above this is a page that already answers the cluster; at or
#: above the second, a page that could with work. Below it, there is no page.
GOOD_FIT = 0.5
WEAK_FIT = 0.2

#: How much each part of a page counts toward relevance. A term in the <title>
#: is a stronger claim about what the page is for than the same term buried in
#: the body.
FIELD_WEIGHTS: tuple[tuple[str, float], ...] = (
    ("title", 3.0),
    ("h1", 3.0),
    ("url", 2.0),
    ("meta_description", 2.0),
    ("h2", 2.0),
    ("text_excerpt", 1.0),
)

_TOKEN = re.compile(r"[a-z0-9]+")
_WHITESPACE = re.compile(r"\s+")


def normalise(term: Any) -> str:
    """One canonical spelling of a search term, for deduplication."""
    text = _WHITESPACE.sub(" ", str(term or "").strip().lower())
    return text.strip(" -_,.;:!?\"'()[]")


def tokens(text: Any) -> list[str]:
    """Topical tokens of a phrase, stopwords removed."""
    return [word for word in _TOKEN.findall(str(text or "").lower()) if word not in STOPWORDS]


def phrases(text: Any, *, min_tokens: int = 2, max_tokens: int = 4, limit: int = 12) -> list[str]:
    """Candidate search phrases inside a short piece of ad or page copy.

    Contiguous token windows, not n-grams of the whole document: this is fed
    headlines and offers, where the words a competitor chose to buy attention
    with are usually adjacent. Every phrase produced here is a *hypothesis* and
    is priced by node 1.4.3 before anything is built on it — an unpriced term is
    reported as unpriced, never as demand.
    """
    words = tokens(text)
    found: list[str] = []
    for size in range(min_tokens, max_tokens + 1):
        for start in range(len(words) - size + 1):
            phrase = " ".join(words[start : start + size])
            if phrase not in found:
                found.append(phrase)
            if len(found) >= limit:
                return found
    return found


# ---------------------------------------------------------------------------
# 1.4.1 — the universe
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Seed:
    """One term as one source offered it."""

    term: str
    source: str
    market: str = ""
    evidence_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class UniverseTerm:
    """One deduped term, and every source and market that produced it."""

    term: str
    sources: tuple[str, ...]
    markets: tuple[str, ...]
    evidence_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "source": list(self.sources),
            "market": list(self.markets),
            "evidence_ids": [str(item) for item in self.evidence_ids],
        }


def assemble(seeds: Iterable[Seed], *, cap: int = MAX_TERMS) -> tuple[list[UniverseTerm], int]:
    """Dedupe seeds into a universe, keeping every source that offered a term.

    Order is by how many independent sources named a term, then alphabetically:
    a phrase our own search-term report *and* a competitor's ad *and* the
    keyword vendor all produced is more likely to matter than one only a vendor
    guessed at, and that ordering is what survives the cap.
    """
    merged: dict[str, tuple[list[str], list[str], list[uuid.UUID]]] = {}
    for seed in seeds:
        term = normalise(seed.term)
        if not term or len(term) > 200:
            continue
        sources, markets, ids = merged.setdefault(term, ([], [], []))
        if seed.source not in sources:
            sources.append(seed.source)
        if seed.market and seed.market not in markets:
            markets.append(seed.market)
        if seed.evidence_id is not None and seed.evidence_id not in ids:
            ids.append(seed.evidence_id)

    rows = [
        UniverseTerm(
            term=term,
            sources=tuple(sorted(sources)),
            markets=tuple(sorted(markets)),
            evidence_ids=tuple(ids[:6]),
        )
        for term, (sources, markets, ids) in merged.items()
    ]
    rows.sort(key=lambda row: (-len(row.sources), row.term))
    dropped = max(0, len(rows) - cap)
    if dropped:
        log.info("keywords.universe_truncated", kept=cap, dropped=dropped)
    return rows[:cap], dropped


def source_counts(rows: Sequence[UniverseTerm]) -> dict[str, int]:
    """How many terms each source contributed, for the coverage line."""
    counter: Counter[str] = Counter()
    for row in rows:
        counter.update(row.sources)
    return dict(counter.most_common())


# ---------------------------------------------------------------------------
# 1.4.2 — clustering, so 2,000 terms can be acted on as themes
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cluster:
    """A group of terms sharing their strongest token."""

    key: str
    terms: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {"key": self.key, "size": len(self.terms), "terms": list(self.terms[:25])}


def cluster_terms(
    terms: Sequence[str], *, min_size: int = MIN_CLUSTER_SIZE, max_clusters: int = 60
) -> list[Cluster]:
    """Group terms by the longest phrase enough of them share.

    Greedy and deterministic: take the most common two-word phrase, claim every
    unassigned term containing it, repeat; fall back to single tokens only when
    no phrase is shared by `min_size` terms. It is not a semantic clustering and
    does not pretend to be — it is a stable grouping a human can check by
    reading the key, which is what the page map and the report need.

    Phrases first is the whole difference between a useful grouping and a
    useless one. On a real universe the commonest *token* is the category word,
    so a single-token pass returns one cluster holding "sds software" and "free
    sds template" together — two opposite intents, one page recommendation, and
    a negative-keyword list that cannot tell them apart.
    """
    unassigned = {normalise(term) for term in terms if normalise(term)}
    clusters: list[Cluster] = []

    while unassigned and len(clusters) < max_clusters:
        key = _best_key(unassigned, min_size)
        if key is None:
            break
        members = sorted(term for term in unassigned if _contains(term, key))
        clusters.append(Cluster(key=key, terms=tuple(members)))
        unassigned.difference_update(members)

    if unassigned:
        clusters.append(Cluster(key="unclustered", terms=tuple(sorted(unassigned))))
    return clusters


def _best_key(terms: set[str], min_size: int) -> str | None:
    """The longest n-gram shared by at least `min_size` of these terms."""
    for size in (2, 1):
        counter: Counter[str] = Counter()
        for term in terms:
            counter.update(_ngrams(term, size))
        if not counter:
            continue
        # Ties broken alphabetically so the same input always clusters the same
        # way — a report that reorders itself between runs is unreviewable.
        key, count = min(counter.most_common(), key=lambda item: (-item[1], item[0]))
        if count >= min_size:
            return key
    return None


def _ngrams(term: str, size: int) -> set[str]:
    words = tokens(term)
    return {" ".join(words[start : start + size]) for start in range(len(words) - size + 1)}


def _contains(term: str, key: str) -> bool:
    parts = key.split(" ")
    words = tokens(term)
    return any(
        words[start : start + len(parts)] == parts for start in range(len(words) - len(parts) + 1)
    )


# ---------------------------------------------------------------------------
# 1.4.3 — the demand join
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DemandRow:
    """One term priced from the vendor's numbers. Nothing here is generated."""

    term: str
    volume: int
    cpc_low: float | None
    cpc_high: float | None
    competition: str | None
    competition_index: float | None
    seasonality_index: tuple[float, ...]
    trend_yoy: float | None
    months_observed: int
    evidence_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "volume": self.volume,
            "cpc_low": self.cpc_low,
            "cpc_high": self.cpc_high,
            "competition": self.competition,
            "competition_index": self.competition_index,
            "seasonality_index": list(self.seasonality_index),
            "trend_yoy": self.trend_yoy,
            "months_observed": self.months_observed,
        }


def join_demand(
    terms: Iterable[str],
    payloads: list[dict[str, Any]],
    evidence_ids: list[uuid.UUID],
) -> tuple[list[DemandRow], list[str]]:
    """Attach vendor metrics to the universe. Returns the priced rows and the rest.

    PRD §10 1.4.3: "joined from DataForSEO, not generated". A term the vendor
    did not return comes back in the second list rather than as a row of zeros —
    zero volume and unknown volume are different findings, and only one of them
    is a reason not to bid.
    """
    if len(payloads) != len(evidence_ids):
        raise ValueError("payloads and evidence ids must be parallel")

    grouped: dict[str, list[tuple[dict[str, Any], uuid.UUID]]] = {}
    for payload, evidence_id in zip(payloads, evidence_ids, strict=True):
        term = normalise(payload.get("keyword"))
        if term:
            grouped.setdefault(term, []).append((payload, evidence_id))

    rows: list[DemandRow] = []
    unpriced: list[str] = []
    for raw in terms:
        term = normalise(raw)
        found = grouped.get(term)
        if not found:
            unpriced.append(term)
            continue
        # The richest row wins when a term arrives from two endpoints: the one
        # that carries monthly history, then the one with a volume at all.
        payload, evidence_id = max(
            found,
            key=lambda item: (
                len(item[0].get("monthly_searches") or []),
                _int(item[0].get("search_volume")),
            ),
        )
        monthly = [
            item for item in (payload.get("monthly_searches") or []) if isinstance(item, dict)
        ]
        rows.append(
            DemandRow(
                term=term,
                volume=_int(payload.get("search_volume")),
                cpc_low=_money(payload.get("low_top_of_page_bid") or payload.get("cpc")),
                cpc_high=_money(payload.get("high_top_of_page_bid") or payload.get("cpc")),
                competition=_competition(payload.get("competition")),
                competition_index=_number(payload.get("competition_index")),
                seasonality_index=seasonality(monthly),
                trend_yoy=trend_yoy(monthly),
                months_observed=len(monthly),
                evidence_ids=(evidence_id,),
            )
        )
    rows.sort(key=lambda row: (-row.volume, row.term))
    return rows, unpriced


def seasonality(monthly: list[dict[str, Any]]) -> tuple[float, ...]:
    """12 values, January first, each month's volume as a percent of the mean.

    Empty when there is no monthly history. An all-100 array would assert that
    demand is flat, which is a claim; saying nothing is not.
    """
    buckets: list[list[float]] = [[] for _ in range(12)]
    for row in monthly:
        month = _int(row.get("month"))
        volume = _number(row.get("search_volume"))
        if 1 <= month <= 12 and volume is not None:
            buckets[month - 1].append(volume)
    observed = [sum(values) / len(values) for values in buckets if values]
    if not observed:
        return ()
    mean = sum(observed) / len(observed)
    if mean <= 0:
        return ()
    return tuple(
        round(100 * (sum(values) / len(values)) / mean, 1) if values else 0.0 for values in buckets
    )


def trend_yoy(monthly: list[dict[str, Any]]) -> float | None:
    """Percent change of the last 12 months against the 12 before them.

    None when there is less than two years of history — which is the usual case
    for this vendor, and is why the field is nullable rather than zero.
    """
    ordered = sorted(
        (row for row in monthly if _int(row.get("year")) and _int(row.get("month"))),
        key=lambda row: (_int(row.get("year")), _int(row.get("month"))),
    )
    if len(ordered) < 24:
        return None
    recent = sum(_number(row.get("search_volume")) or 0.0 for row in ordered[-12:])
    prior = sum(_number(row.get("search_volume")) or 0.0 for row in ordered[-24:-12])
    if prior <= 0:
        return None
    return round(100 * (recent - prior) / prior, 1)


# ---------------------------------------------------------------------------
# 1.4.4 — the negative blocklist
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Negative:
    """One term not to pay for, and which evidence said so."""

    term: str
    match_type: MatchType
    reason: str
    source: NegativeSource

    def as_dict(self) -> dict[str, Any]:
        return {
            "term": self.term,
            "match_type": self.match_type,
            "reason": self.reason,
            "source": self.source,
        }


#: 1.2.2's `recommended_action` vocabulary, mapped to a match type. Actions that
#: are not "block this" are absent on purpose — `lower_bid` is a bidding
#: decision and must not silently become a negative keyword.
_ACTION_MATCH: dict[str, MatchType] = {
    "negative_exact": "exact",
    "negative_phrase": "phrase",
    "negative_broad": "broad",
}


def blocklist(
    *,
    lost_reason_terms: Iterable[tuple[str, str]] = (),
    wasteful: Iterable[dict[str, Any]] = (),
    irrelevant: Iterable[tuple[str, str]] = (),
    protect: Iterable[str] = (),
) -> tuple[list[Negative], list[str]]:
    """Merge the three negative sources, and refuse to block what converts.

    Returns the blocklist and the terms that were *withheld* because node 1.2.2
    measured them converting. The second list is not a diagnostic — it is a
    finding, and the node puts it in its output: a term that both wins deals and
    looks like a lost-deal pattern is exactly the thing a human should look at.
    """
    protected = {normalise(term) for term in protect if normalise(term)}
    withheld: list[str] = []
    collected: dict[str, Negative] = {}

    def add(term: str, match_type: MatchType, reason: str, source: NegativeSource) -> None:
        cleaned = normalise(term)
        if not cleaned:
            return
        if cleaned in protected:
            if cleaned not in withheld:
                withheld.append(cleaned)
            return
        # First source wins: the three are added in descending order of how
        # directly they observed the waste, so a term our own money proved
        # wasteful keeps that attribution rather than a classifier's.
        collected.setdefault(cleaned, Negative(cleaned, match_type, reason, source))

    for row in wasteful:
        action = str(row.get("recommended_action") or "")
        match_type = _ACTION_MATCH.get(action)
        if match_type is None:
            continue
        cost = row.get("cost")
        add(
            str(row.get("term") or ""),
            match_type,
            str(row.get("reason") or "") or f"Spent {cost} and converted nobody.",
            "wasteful_terms",
        )

    for term, reason in lost_reason_terms:
        add(term, "phrase", reason, "lost_reasons")

    for term, reason in irrelevant:
        add(term, "exact", reason, "intent_irrelevant")

    rows = sorted(collected.values(), key=lambda item: (item.source, item.term))
    if withheld:
        log.info("keywords.negatives_withheld", count=len(withheld))
    return rows, sorted(withheld)


# ---------------------------------------------------------------------------
# 1.4.5 — which page answers which cluster
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageMatch:
    """The best page for one cluster, and how good "best" actually is."""

    cluster: str
    best_url: str | None
    relevance_score: float
    verdict: Verdict
    runner_up_url: str | None
    matched_tokens: tuple[str, ...]
    missing_tokens: tuple[str, ...]
    cluster_size: int
    evidence_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "term_cluster": self.cluster,
            "best_url": self.best_url,
            "relevance_score": self.relevance_score,
            "verdict": self.verdict,
            "runner_up_url": self.runner_up_url,
            "matched_tokens": list(self.matched_tokens),
            "missing_tokens": list(self.missing_tokens),
            "cluster_size": self.cluster_size,
        }


def map_clusters_to_pages(
    clusters: Sequence[Cluster],
    pages: list[dict[str, Any]],
    page_ids: list[uuid.UUID],
) -> list[PageMatch]:
    """Score every page against every cluster and keep the best two.

    The score is weighted token coverage: of the topical tokens the cluster's
    terms are built from, how many does this page use, and where. It is a
    lexical measure and is reported as one — `matched_tokens` and
    `missing_tokens` are returned so a reader can see exactly what earned the
    number rather than taking it on trust.
    """
    if len(pages) != len(page_ids):
        raise ValueError("pages and evidence ids must be parallel")

    indexed = [
        (_page_tokens(page), page, page_id) for page, page_id in zip(pages, page_ids, strict=True)
    ]
    matches: list[PageMatch] = []

    for cluster in clusters:
        wanted = _cluster_tokens(cluster)
        if not wanted:
            matches.append(
                PageMatch(
                    cluster=cluster.key,
                    best_url=None,
                    relevance_score=0.0,
                    verdict="gap",
                    runner_up_url=None,
                    matched_tokens=(),
                    missing_tokens=(),
                    cluster_size=len(cluster.terms),
                    evidence_ids=(),
                )
            )
            continue

        scored = sorted(
            (
                (_score_page(wanted, weighted), weighted, page, page_id)
                for weighted, page, page_id in indexed
            ),
            # Ties break on the URL rather than on input order, so two equally
            # relevant pages pick the same winner on every run.
            key=lambda item: (-item[0], str(item[2].get("url") or "")),
        )
        best = scored[0] if scored else None
        runner_up = scored[1] if len(scored) > 1 else None
        score = best[0] if best else 0.0
        present = tuple(word for word in wanted if best is not None and word in best[1])
        matches.append(
            PageMatch(
                cluster=cluster.key,
                best_url=str(best[2].get("url")) if best and score > 0 else None,
                relevance_score=score,
                verdict=verdict_for(score),
                runner_up_url=(
                    str(runner_up[2].get("url")) if runner_up and runner_up[0] > 0 else None
                ),
                matched_tokens=present,
                missing_tokens=tuple(sorted(set(wanted) - set(present))),
                cluster_size=len(cluster.terms),
                evidence_ids=(best[3],) if best and score > 0 else (),
            )
        )
    return matches


def verdict_for(score: float) -> Verdict:
    """The thresholds, in one place, so the report and the node cannot disagree."""
    if score >= GOOD_FIT:
        return "good_fit"
    if score >= WEAK_FIT:
        return "weak_fit"
    return "gap"


def _cluster_tokens(cluster: Cluster) -> tuple[str, ...]:
    """The tokens a cluster is *about*: shared by enough of it to be the theme.

    The share floor matters more than the cap. A cluster of 800 long-tail terms
    has 800 one-off qualifiers in it, and taking the top twelve by raw count
    admits a dozen of them alongside the two words that actually name the theme
    — which drags every relevance score toward zero and turns real matches into
    gaps.
    """
    counter: Counter[str] = Counter()
    for term in cluster.terms:
        counter.update(set(tokens(term)))
    floor = max(1, int(len(cluster.terms) * CLUSTER_TOKEN_SHARE))
    shared = [word for word, count in counter.most_common() if count >= floor]
    if not shared:
        shared = [word for word, _ in counter.most_common(12)]
    return tuple(shared[:12])


def _page_tokens(page: dict[str, Any]) -> dict[str, float]:
    """Every token on a page, carrying the weight of the strongest field it is in."""
    weighted: dict[str, float] = {}
    for field_name, weight in FIELD_WEIGHTS:
        value = page.get(field_name)
        if field_name == "h2" and isinstance(value, list):
            value = " ".join(str(item) for item in value)
        for word in tokens(value):
            if weight > weighted.get(word, 0.0):
                weighted[word] = weight
    return weighted


def _score_page(wanted: tuple[str, ...], weighted: dict[str, float]) -> float:
    """Weighted coverage of the cluster's tokens, normalised to 0-1."""
    top = max(weight for _, weight in FIELD_WEIGHTS)
    earned = sum(weighted.get(word, 0.0) for word in wanted)
    return round(earned / (top * len(wanted)), 3) if wanted else 0.0


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _number(value: Any) -> float | None:
    try:
        return round(float(value), 4)
    except (TypeError, ValueError):
        return None


def _money(value: Any) -> float | None:
    number = _number(value)
    return round(number, 2) if number is not None else None


def _competition(value: Any) -> str | None:
    """The vendor sends either a label or a 0-1 float. One vocabulary comes out."""
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip().upper() or None
    number = _number(value)
    if number is None:
        return None
    if number >= 0.66:
        return "HIGH"
    return "MEDIUM" if number >= 0.33 else "LOW"
