"""Every number stage 1.3 reports, computed in Python.

PRD §18 law 3, for the competitive half of the research: an overlap score, a
spend range, a month histogram and a cluster frequency are all arithmetic, so
none of them are asked of a model. The nodes hand the model the finished table
and ask it for the things a table cannot hold — what a competitor's ad is
*saying*, what angle it takes, which claim nobody is making.

Two things here are judgement calls rather than measurements, and both are
declared rather than hidden:

* **`overlap_score` is a ranking, not a percentage of anything.** It combines
  paid-keyword intersections with SERP presence on our own money terms, each
  normalised against the strongest competitor in the same set. A score of 100
  means "the most overlapping advertiser we found", never "100% overlap".
* **A spend estimate is a model of spend, not spend.** PRD §10 1.3.3 is explicit
  — "must state method; never present as fact" — so every estimate carries the
  method that produced it, the inputs it used, and a confidence that drops to
  `low` the moment the only signal is how many ads we happened to see.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

import structlog

log = structlog.get_logger(__name__)

#: How a competitor turned up. `auction` is declared because PRD §10 names it
#: first, and is currently unreachable: the Google Ads API exposes no auction-
#: insights resource, so nothing can emit it without scraping the UI. Keeping it
#: in the vocabulary means a future source slots in without a schema change; a
#: node that never emits it is the honest state today.
OverlapBasis = Literal["auction", "paid_keywords", "serp"]

#: How a spend range was arrived at. Carried into the output verbatim.
SpendMethod = Literal["paid_traffic_cost", "creative_volume", "insufficient_evidence"]

Confidence = Literal["high", "medium", "low"]

#: Weights over the signals that are actually present. Renormalised, so a
#: project with SERP evidence and no keyword vendor does not silently cap every
#: competitor at 40 out of 100.
WEIGHTS: dict[str, float] = {"paid_keywords": 0.6, "serp": 0.4}

#: Competitors reaching a prompt, ranked by overlap. Anything past this is
#: reported as truncated rather than dropped in silence.
MAX_COMPETITORS = 25

#: Evidence ids kept per aggregated row — enough to check by hand, bounded so
#: one competitor cannot fill the output document.
MAX_IDS = 12

#: The band a vendor's monthly paid-traffic-cost estimate is widened to. It is a
#: modelled number to begin with, and reporting it as a point value would give
#: it a precision it does not have.
TRAFFIC_COST_BAND = (0.6, 1.6)

#: The order-of-magnitude band used when the only thing known about an
#: advertiser is how many live creatives they are running. Deliberately wide,
#: and surfaced in `basis` so nobody reads it as a measurement.
PER_CREATIVE_MONTHLY = (250.0, 2_500.0)

#: A month counts as a peak when it carries at least this share of the busiest
#: month's live-creative count.
PEAK_THRESHOLD = 0.8


# ---------------------------------------------------------------------------
# 1.3.1 — who we are actually up against
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CompetitorRow:
    """One competing advertiser, scored against the others in the same pull."""

    domain: str
    paid_keyword_overlap: int
    paid_keyword_count: int
    est_paid_traffic_cost: float
    avg_position: float | None
    serp_hits: int
    serp_terms: tuple[str, ...]
    overlap_score: float
    overlap_basis: tuple[str, ...]
    evidence_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "domain": self.domain,
            "paid_keyword_overlap": self.paid_keyword_overlap,
            "paid_keyword_count": self.paid_keyword_count,
            "est_paid_traffic_cost": self.est_paid_traffic_cost,
            "avg_position": self.avg_position,
            "serp_hits": self.serp_hits,
            "serp_terms": list(self.serp_terms[:8]),
            "overlap_score": self.overlap_score,
            "overlap_basis": list(self.overlap_basis),
            "evidence_ids": [str(item) for item in self.evidence_ids],
        }


@dataclass(slots=True)
class _Accumulator:
    """Signals for one domain, gathered before anything is scored."""

    domain: str
    intersections: int = 0
    paid_keyword_count: int = 0
    traffic_cost: float = 0.0
    positions: list[float] = field(default_factory=list)
    serp_terms: list[str] = field(default_factory=list)
    evidence_ids: list[uuid.UUID] = field(default_factory=list)


def competitor_rows(
    *,
    domain_rows: list[dict[str, Any]],
    domain_ids: list[uuid.UUID],
    serp_rows: list[dict[str, Any]],
    serp_ids: list[uuid.UUID],
    our_domain: str,
    our_terms: set[str] | None = None,
    top_n: int = MAX_COMPETITORS,
) -> tuple[list[CompetitorRow], int, int]:
    """Rank competing advertisers by how much of our demand they sit on.

    Returns the ranked rows, how many were cut by `top_n`, and how many of our
    own terms had a SERP to check — the denominator behind `serp_hits`, which
    the node reports so "3 hits" cannot be read without "out of how many".
    """
    if len(domain_rows) != len(domain_ids) or len(serp_rows) != len(serp_ids):
        raise ValueError("payloads and evidence ids must be parallel")

    ours = _hostname(our_domain)
    found: dict[str, _Accumulator] = {}

    for payload, evidence_id in zip(domain_rows, domain_ids, strict=True):
        domain = _hostname(payload.get("competitor_domain"))
        if not domain or domain == ours:
            continue
        item = found.setdefault(domain, _Accumulator(domain))
        item.intersections += _int(payload.get("intersections"))
        item.paid_keyword_count += _int(payload.get("paid_keyword_count"))
        item.traffic_cost += _float(payload.get("paid_estimated_traffic_cost"))
        position = payload.get("avg_position")
        if position is not None:
            item.positions.append(_float(position))
        item.evidence_ids.append(evidence_id)

    wanted = {term.strip().lower() for term in (our_terms or set()) if term}
    checked = 0
    for payload, evidence_id in zip(serp_rows, serp_ids, strict=True):
        keyword = str(payload.get("keyword") or "").strip().lower()
        # A SERP for a term we do not bid on tells us nothing about *our*
        # competition, so it is not counted in the denominator either.
        if wanted and keyword not in wanted:
            continue
        checked += 1
        seen: set[str] = set()
        for result in payload.get("results") or []:
            if not isinstance(result, dict):
                continue
            domain = _hostname(result.get("domain") or result.get("url"))
            if not domain or domain == ours or domain in seen:
                continue
            seen.add(domain)
            item = found.setdefault(domain, _Accumulator(domain))
            item.serp_terms.append(keyword)
            item.evidence_ids.append(evidence_id)

    max_intersections = max((item.intersections for item in found.values()), default=0)
    rows = [
        _score(item, max_intersections=max_intersections, serp_checked=checked)
        for item in found.values()
    ]
    rows.sort(key=lambda row: (-row.overlap_score, row.domain))
    if len(rows) > top_n:
        log.info("creatives.competitors_truncated", kept=top_n, dropped=len(rows) - top_n)
    return rows[:top_n], max(0, len(rows) - top_n), checked


def _score(item: _Accumulator, *, max_intersections: int, serp_checked: int) -> CompetitorRow:
    """Normalise whichever signals exist, and name them."""
    components: list[tuple[str, float]] = []
    basis: list[str] = []
    if max_intersections > 0:
        components.append(("paid_keywords", item.intersections / max_intersections))
        if item.intersections > 0:
            basis.append("paid_keywords")
    if serp_checked > 0:
        components.append(("serp", len(set(item.serp_terms)) / serp_checked))
        if item.serp_terms:
            basis.append("serp")

    weight_total = sum(WEIGHTS[name] for name, _ in components)
    score = (
        round(100 * sum(WEIGHTS[name] * value for name, value in components) / weight_total, 1)
        if weight_total > 0
        else 0.0
    )
    return CompetitorRow(
        domain=item.domain,
        paid_keyword_overlap=item.intersections,
        paid_keyword_count=item.paid_keyword_count,
        est_paid_traffic_cost=round(item.traffic_cost, 2),
        avg_position=(
            round(sum(item.positions) / len(item.positions), 2) if item.positions else None
        ),
        serp_hits=len(set(item.serp_terms)),
        serp_terms=tuple(dict.fromkeys(item.serp_terms)),
        overlap_score=score,
        overlap_basis=tuple(basis),
        evidence_ids=tuple(dict.fromkeys(item.evidence_ids))[:MAX_IDS],
    )


# ---------------------------------------------------------------------------
# 1.3.2 — the creative corpus
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CreativeRow:
    """One competitor ad, as scraped. Nothing here was written by a model."""

    key: str
    advertiser: str
    ad_id: str | None
    format: str
    first_shown: str | None
    last_shown: str | None
    creative_text: str
    landing_url: str | None
    screenshot_path: str | None
    evidence_id: uuid.UUID

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "advertiser": self.advertiser,
            "ad_id": self.ad_id,
            "format": self.format,
            "first_shown": self.first_shown,
            "last_shown": self.last_shown,
            "creative_text": self.creative_text,
            "landing_url": self.landing_url,
        }


def creative_rows(
    payloads: list[dict[str, Any]], evidence_ids: list[uuid.UUID], *, max_ads: int
) -> tuple[list[CreativeRow], int]:
    """Deduped ads, newest-looking first, with the count dropped by the cap.

    Evidence is already deduped per project by payload hash, but the same ad
    reached through two advertiser searches differs by `regions` and hashes
    differently. The key here is the creative identity — its ad id, or its text
    when the id did not parse.
    """
    if len(payloads) != len(evidence_ids):
        raise ValueError("payloads and evidence ids must be parallel")

    rows: dict[str, CreativeRow] = {}
    for payload, evidence_id in zip(payloads, evidence_ids, strict=True):
        text = str(payload.get("creative_text") or "").strip()
        ad_id = payload.get("ad_id")
        advertiser = str(payload.get("advertiser") or "").strip() or "unknown"
        key = str(ad_id) if ad_id else f"{advertiser}:{text[:120]}"
        if not text and not ad_id:
            continue
        rows.setdefault(
            key,
            CreativeRow(
                key=key,
                advertiser=advertiser,
                ad_id=str(ad_id) if ad_id else None,
                format=str(payload.get("format") or "text"),
                first_shown=_as_date(payload.get("first_shown")),
                last_shown=_as_date(payload.get("last_shown")),
                creative_text=text[:2000],
                landing_url=payload.get("destination_url"),
                screenshot_path=payload.get("screenshot_path"),
                evidence_id=evidence_id,
            ),
        )

    ordered = sorted(rows.values(), key=lambda row: (row.last_shown or "", row.key), reverse=True)
    dropped = max(0, len(ordered) - max_ads)
    if dropped:
        log.info("creatives.corpus_truncated", kept=max_ads, dropped=dropped)
    return ordered[:max_ads], dropped


def corpus_stats(rows: list[CreativeRow]) -> dict[str, Any]:
    """Counts a model must not be asked to produce, and must not restate."""
    by_advertiser = Counter(row.advertiser for row in rows)
    dates = sorted(value for row in rows for value in (row.first_shown, row.last_shown) if value)
    return {
        "ads": len(rows),
        "advertisers": len(by_advertiser),
        "ads_per_advertiser": dict(by_advertiser.most_common()),
        "formats": dict(Counter(row.format for row in rows).most_common()),
        "with_landing_url": sum(1 for row in rows if row.landing_url),
        "with_screenshot": sum(1 for row in rows if row.screenshot_path),
        "first_seen": dates[0] if dates else None,
        "last_seen": dates[-1] if dates else None,
    }


def cluster_frequency(themes: dict[str, str], rows: list[CreativeRow]) -> list[dict[str, Any]]:
    """Turn the model's `creative key -> theme` assignment into counted clusters.

    The model says which ads belong together; how many there are, and which
    advertisers run them, is counted here. A theme the model invented for a key
    that is not in the corpus is dropped rather than reported as a cluster of
    zero.
    """
    by_key = {row.key: row for row in rows}
    grouped: dict[str, list[CreativeRow]] = {}
    for key, theme in themes.items():
        row = by_key.get(key)
        name = (theme or "").strip()
        if row is None or not name:
            continue
        grouped.setdefault(name, []).append(row)

    # Ordered before the dicts are built, not after: sorting a list of
    # `dict[str, Any]` on one of its values is a cast waiting to happen.
    ordered = sorted(grouped.items(), key=lambda item: (-len(item[1]), item[0]))
    return [
        {
            "theme": theme,
            "frequency": len(members),
            "share_pct": round(100 * len(members) / len(rows), 1) if rows else 0.0,
            "advertisers": sorted({row.advertiser for row in members}),
            "example_keys": [row.key for row in members[:3]],
        }
        for theme, members in ordered
    ]


# ---------------------------------------------------------------------------
# 1.3.3 — what it plausibly costs them
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpendEstimate:
    """A modelled range, the method behind it, and the inputs it used."""

    competitor: str
    est_monthly_spend_low: float | None
    est_monthly_spend_high: float | None
    currency: str
    method: SpendMethod
    confidence: Confidence
    basis: dict[str, Any]
    peak_months: tuple[int, ...]
    creatives: int
    evidence_ids: tuple[uuid.UUID, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "competitor": self.competitor,
            "est_monthly_spend_low": self.est_monthly_spend_low,
            "est_monthly_spend_high": self.est_monthly_spend_high,
            "currency": self.currency,
            "method": self.method,
            "confidence": self.confidence,
            "basis": self.basis,
            "peak_months": list(self.peak_months),
            "creatives": self.creatives,
        }


def spend_estimates(
    competitors: list[CompetitorRow],
    creatives: list[CreativeRow],
    *,
    aliases: dict[str, str] | None = None,
    currency: str = "USD",
) -> list[SpendEstimate]:
    """One range per advertiser we have any signal for.

    Every branch here names its method, and the weakest branch is allowed to
    say `insufficient_evidence` rather than produce a number. PRD §16's rule for
    a missing source — "never hallucinate history" — is the same rule.

    `aliases` maps a scraped advertiser name to the domain the keyword vendor
    knows it by. Without it the two halves of this function never meet: the
    Transparency Center says "Chemwatch" and the vendor says "chemwatch.net", so
    one company would come back as two estimates — a traffic-cost range with no
    ads under it, and a creative-count range with no spend signal.
    """
    by_advertiser = _group_creatives(creatives, aliases)
    traffic_cost = {row.domain: row.est_paid_traffic_cost for row in competitors}
    ids = {row.domain: row.evidence_ids for row in competitors}

    names = sorted(set(traffic_cost) | set(by_advertiser))
    estimates: list[SpendEstimate] = []
    for name in names:
        ads = by_advertiser.get(name, [])
        cost = traffic_cost.get(name, 0.0)
        months = peak_months(ads)
        evidence = list(ids.get(name, ()))
        evidence.extend(row.evidence_id for row in ads)

        if cost > 0:
            low, high = TRAFFIC_COST_BAND
            estimates.append(
                SpendEstimate(
                    competitor=name,
                    est_monthly_spend_low=round(cost * low, 2),
                    est_monthly_spend_high=round(cost * high, 2),
                    currency=currency,
                    method="paid_traffic_cost",
                    confidence="medium",
                    basis={
                        "vendor_monthly_paid_traffic_cost": round(cost, 2),
                        "band": list(TRAFFIC_COST_BAND),
                        "note": (
                            "A keyword vendor's modelled cost of the paid traffic this domain "
                            "receives, widened to a range. It is an estimate of an estimate."
                        ),
                    },
                    peak_months=months,
                    creatives=len(ads),
                    evidence_ids=tuple(dict.fromkeys(evidence))[:MAX_IDS],
                )
            )
        elif ads:
            low_rate, high_rate = PER_CREATIVE_MONTHLY
            estimates.append(
                SpendEstimate(
                    competitor=name,
                    est_monthly_spend_low=round(len(ads) * low_rate, 2),
                    est_monthly_spend_high=round(len(ads) * high_rate, 2),
                    currency=currency,
                    method="creative_volume",
                    confidence="low",
                    basis={
                        "live_creatives": len(ads),
                        "assumed_monthly_spend_per_creative": list(PER_CREATIVE_MONTHLY),
                        "note": (
                            "No spend signal exists for this advertiser. The range is the "
                            "creative count multiplied by an assumed per-creative monthly "
                            "spend, and is an order-of-magnitude guide only."
                        ),
                    },
                    peak_months=months,
                    creatives=len(ads),
                    evidence_ids=tuple(dict.fromkeys(evidence))[:MAX_IDS],
                )
            )
        else:
            estimates.append(
                SpendEstimate(
                    competitor=name,
                    est_monthly_spend_low=None,
                    est_monthly_spend_high=None,
                    currency=currency,
                    method="insufficient_evidence",
                    confidence="low",
                    basis={"note": "Neither a paid-traffic cost nor a live creative was found."},
                    peak_months=(),
                    creatives=0,
                    evidence_ids=tuple(dict.fromkeys(evidence))[:MAX_IDS],
                )
            )
    estimates.sort(key=lambda item: (-(item.est_monthly_spend_high or 0.0), item.competitor))
    return estimates


def months_live(rows: list[CreativeRow]) -> list[int]:
    """Live-creative count per calendar month, January first.

    An ad shown from November to February is live in all four months; counting
    only its start month would put the whole of a winter campaign in November.
    """
    counts = [0] * 12
    for row in rows:
        start = _parse(row.first_shown)
        end = _parse(row.last_shown) or start
        if start is None or end is None:
            continue
        if end < start:
            start, end = end, start
        span = (end.year - start.year) * 12 + (end.month - start.month)
        for offset in range(min(span, 11) + 1):
            counts[(start.month - 1 + offset) % 12] += 1
    return counts


def peak_months(rows: list[CreativeRow]) -> tuple[int, ...]:
    """The months at or above `PEAK_THRESHOLD` of the busiest, as 1-12."""
    counts = months_live(rows)
    top = max(counts, default=0)
    if top <= 0:
        return ()
    return tuple(index + 1 for index, value in enumerate(counts) if value >= top * PEAK_THRESHOLD)


def _group_creatives(
    rows: list[CreativeRow], aliases: dict[str, str] | None
) -> dict[str, list[CreativeRow]]:
    resolved = {str(name).strip().lower(): domain for name, domain in (aliases or {}).items()}
    grouped: dict[str, list[CreativeRow]] = {}
    for row in rows:
        key = resolved.get(row.advertiser.strip().lower(), row.advertiser)
        grouped.setdefault(key, []).append(row)
    return grouped


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _hostname(value: Any) -> str:
    """A bare, comparable hostname. `https://WWW.Acme.com/pricing` -> `acme.com`."""
    text = str(value or "").strip().lower()
    if not text:
        return ""
    if "//" in text:
        text = text.split("//", 1)[1]
    text = text.split("/", 1)[0].split("?", 1)[0].split("@")[-1]
    if text.startswith("www."):
        text = text[4:]
    return text.strip(".")


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_date(value: Any) -> str | None:
    parsed = _parse(value)
    return parsed.isoformat() if parsed else None


def _parse(value: Any) -> date | None:
    if isinstance(value, date):
        return value
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None
