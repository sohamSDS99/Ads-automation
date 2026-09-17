"""The arithmetic behind stage 1.5 (PRD §10, "Check we are actually ready").

PRD §18 law 3 again: "All arithmetic (CPA, ROAS, P&L, sizing) happens in pandas/
Python. The model writes labels and prose only." Stage 1.5 is where that law is
easiest to break, because three of its four nodes look like judgement calls —
*is this page too slow, is this tracking stale, can we afford this budget* — and
each of them is a threshold, a date subtraction and a division.

So every number stage 1.5 reports is produced here, and every threshold is named
as a constant with the reason it has that value. Four things are worth stating
plainly because they are the ones a reader will want to argue with:

* **A page audit reports issues against published thresholds**, not against a
  model's taste. LCP and CLS use Google's own "poor" boundaries, so a severity
  here means the same thing it means in PageSpeed Insights.
* **Staleness is measured from the last day a conversion action actually
  recorded something**, which is why the connector pulls conversion actions per
  day. A lifetime total cannot answer "has this stopped working".
* **The synthetic check never claims a round trip it did not observe.** Google's
  conversion reporting lags by hours; a probe fired now cannot be confirmed in
  the API now. What it *can* confirm is that the beacon fired and that it points
  at a conversion action the API knows and is receiving conversions on. That is
  what `observed_in_ads_api` means here, and `latency_min` stays `None` unless a
  conversion genuinely landed after the probe fired.
* **Sizing refuses to invent a conversion rate.** With no measured CVR there is
  no honest scenario, so `size()` returns nothing and says why, rather than
  producing a forecast built on a number somebody made up.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import structlog

from agent.nodes.frames import numeric, with_ids

log = structlog.get_logger(__name__)

Severity = str  # one of SEVERITIES

#: Worst first. `_worst()` orders on this, and the report prints it.
SEVERITIES: tuple[Severity, ...] = ("critical", "major", "minor", "none")

# --- landing page thresholds ------------------------------------------------

#: Google's Core Web Vitals boundaries, unchanged. Using our own numbers here
#: would mean a "major" in this report disagreeing with the PageSpeed report
#: someone opens thirty seconds later.
LCP_GOOD_MS = 2_500.0
LCP_POOR_MS = 4_000.0
CLS_GOOD = 0.1
CLS_POOR = 0.25

#: Fields on a paid landing-page form before it is called out. Not a law of
#: nature — it is the point past which every published test of form length we
#: could act on shows measurable drop-off, and it is reported as an issue to
#: look at rather than as a defect.
FORM_FIELD_LIMIT = 6

#: Words below which a page is unlikely to be a landing page at all — usually a
#: redirect stub or a JS shell the crawler could not read. Reported as an issue
#: about *our measurement*, not about the page.
THIN_PAGE_WORDS = 120

# --- tracking thresholds ----------------------------------------------------

#: Days without a recorded conversion before an enabled action is called stale.
#: Thirty, not seven: a low-volume B2B action can legitimately go a fortnight
#: quiet, and an alert that cries wolf gets filtered into a folder.
STALE_DAYS = 30

# --- sizing -----------------------------------------------------------------

#: Nobody buys 100% impression share. Sizing caps reachable clicks at this
#: fraction of the demand the keyword set actually carries, so a large budget
#: stops converting into imaginary clicks.
MAX_IMPRESSION_SHARE = 0.65

#: Multipliers on the account's current monthly spend.
SPEND_MULTIPLES = (0.5, 1.0, 2.0)

#: Used when the account has never spent: fractions of the budget that would
#: capture the whole reachable market.
DEMAND_FRACTIONS = (0.25, 0.5, 1.0)


# ---------------------------------------------------------------------------
# 1.5.1 — landing page audit
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PageRow:
    """One audited page. Every field is measured; none is inferred."""

    url: str
    lcp_ms: float | None
    cls: float | None
    mobile_ok: bool | None
    form_fields_count: int | None
    trust_signals: tuple[str, ...]
    issues: tuple[str, ...]
    severity: Severity
    status: int | None
    mapped_clusters: tuple[str, ...]
    evidence_ids: tuple[uuid.UUID, ...]
    #: The model's read on whether the page keeps the cluster's promise. Empty
    #: until `apply_message_match` runs, and empty for a page nobody mapped.
    message_match: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "lcp_ms": self.lcp_ms,
            "cls": self.cls,
            "mobile_ok": self.mobile_ok,
            "form_fields_count": self.form_fields_count,
            "trust_signals": list(self.trust_signals),
            "issues": list(self.issues),
            "severity": self.severity,
            "status": self.status,
            "mapped_clusters": list(self.mapped_clusters),
            "message_match": self.message_match or None,
            "evidence_ids": [str(value) for value in self.evidence_ids],
        }


def audit_pages(
    pages: list[dict[str, Any]],
    page_ids: list[uuid.UUID],
    *,
    vitals: list[dict[str, Any]] | None = None,
    vitals_ids: list[uuid.UUID] | None = None,
    wanted: dict[str, list[str]] | None = None,
) -> tuple[list[PageRow], list[str]]:
    """Audit the crawled pages. Returns (rows, urls that were never crawled).

    `wanted` maps a URL to the keyword clusters node 1.4.5 pointed at it. A URL
    in `wanted` that never appears in `pages` is returned in the second element:
    a landing page the crawler could not reach is a launch blocker, and dropping
    it silently would leave the report claiming every mapped page is healthy.
    """
    measured = _vitals_by_url(vitals or [], vitals_ids or [])
    #: key -> (original url, clusters). The key normalises trailing slashes and
    #: case so a crawl of `/demo/` satisfies a mapping to `/demo`; the original
    #: spelling is kept because that is what a person has to go and fix.
    targets = {_key(url): (url, list(clusters)) for url, clusters in (wanted or {}).items()}
    rows: list[PageRow] = []
    seen: set[str] = set()

    for payload, evidence_id in zip(pages, page_ids, strict=False):
        url = str(payload.get("url") or "")
        if not url:
            continue
        key = _key(url)
        if key in seen:
            continue
        seen.add(key)
        vital = measured.get(key, {})
        rows.append(
            _audit_one(
                payload,
                vital,
                clusters=tuple(targets.get(key, (url, []))[1]),
                evidence_ids=tuple(
                    value for value in (evidence_id, vital.get("evidence_id")) if value
                ),
            )
        )

    unreachable = sorted(original for key, (original, _) in targets.items() if key not in seen)
    rows.sort(key=lambda row: (SEVERITIES.index(row.severity), row.url))
    return rows, unreachable


def _audit_one(
    payload: dict[str, Any],
    vital: dict[str, Any],
    *,
    clusters: tuple[str, ...],
    evidence_ids: tuple[uuid.UUID, ...],
) -> PageRow:
    url = str(payload.get("url") or "")
    lcp = _float(vital.get("lcp_ms"))
    cls_value = _float(vital.get("cls"))
    fields = payload.get("form_fields")
    field_count = len(fields) if isinstance(fields, list) else None
    trust = tuple(str(item) for item in (payload.get("trust_markers") or []))
    status = _int(payload.get("status"))
    mobile_ok = payload.get("mobile_viewport")
    issues: list[tuple[Severity, str]] = []

    if status is not None and status >= 400:
        issues.append(("critical", f"the page returns HTTP {status}"))
    if payload.get("https") is False:
        issues.append(("critical", "the page is served over plain HTTP"))
    if mobile_ok is False:
        issues.append(("critical", "no mobile viewport is declared"))

    if lcp is not None:
        if lcp > LCP_POOR_MS:
            issues.append(
                ("critical", f"LCP is {lcp / 1000:.1f}s (poor above {LCP_POOR_MS / 1000:.1f}s)")
            )
        elif lcp > LCP_GOOD_MS:
            issues.append(
                ("major", f"LCP is {lcp / 1000:.1f}s (good is under {LCP_GOOD_MS / 1000:.1f}s)")
            )
    if cls_value is not None:
        if cls_value > CLS_POOR:
            issues.append(("major", f"CLS is {cls_value:.2f} (poor above {CLS_POOR})"))
        elif cls_value > CLS_GOOD:
            issues.append(("minor", f"CLS is {cls_value:.2f} (good is under {CLS_GOOD})"))

    if field_count is not None and field_count > FORM_FIELD_LIMIT:
        issues.append(("major", f"the form asks for {field_count} fields"))
    if field_count == 0 and clusters:
        issues.append(("major", "no form: a mapped landing page with no way to convert"))
    if not payload.get("primary_cta"):
        issues.append(("major", "no primary call to action was found"))
    if not trust:
        issues.append(("minor", "no visible trust markers"))
    words = _int(payload.get("word_count"))
    if words is not None and words < THIN_PAGE_WORDS:
        issues.append(("minor", f"only {words} words of readable copy were retrieved"))
    if lcp is None and cls_value is None:
        issues.append(("minor", "Core Web Vitals were not measured for this page"))

    return PageRow(
        url=url,
        lcp_ms=lcp,
        cls=cls_value,
        mobile_ok=bool(mobile_ok) if mobile_ok is not None else None,
        form_fields_count=field_count,
        trust_signals=trust,
        issues=tuple(text for _, text in issues),
        severity=_worst(severity for severity, _ in issues),
        status=status,
        mapped_clusters=clusters,
        evidence_ids=evidence_ids,
    )


#: What the model is asked about a page it did not measure: does the page keep
#: the promise the keyword cluster makes? A mismatch is the one landing-page
#: problem no threshold can see — the page is fast, mobile, tagged, and about
#: something else.
MESSAGE_MATCH_SEVERITY: dict[str, Severity] = {"mismatch": "major", "partial": "minor"}


def apply_message_match(rows: list[PageRow], verdicts: dict[str, tuple[str, str]]) -> list[PageRow]:
    """Fold the model's message-match verdict into the computed audit.

    Severity is recomputed rather than patched: a page whose only problem is a
    mismatch has to come out `major`, and a page that was already `critical`
    must not be downgraded by a merge that overwrites instead of combining.
    """
    merged: list[PageRow] = []
    for row in rows:
        verdict, note = verdicts.get(_key(row.url), ("", ""))
        severity = MESSAGE_MATCH_SEVERITY.get(verdict)
        if severity is None:
            merged.append(replace(row, message_match=verdict) if verdict else row)
            continue
        issue = note.strip() or f"the page's message is a {verdict} for the keywords mapped to it"
        merged.append(
            replace(
                row,
                issues=(*row.issues, issue),
                severity=_worst([row.severity, severity]),
                message_match=verdict,
            )
        )
    merged.sort(key=lambda row: (SEVERITIES.index(row.severity), row.url))
    return merged


def _vitals_by_url(
    vitals: list[dict[str, Any]], vitals_ids: list[uuid.UUID]
) -> dict[str, dict[str, Any]]:
    measured: dict[str, dict[str, Any]] = {}
    for payload, evidence_id in zip(vitals, vitals_ids, strict=False):
        url = str(payload.get("url") or "")
        if url:
            measured[_key(url)] = {**payload, "evidence_id": evidence_id}
    return measured


def _worst(severities: Any) -> Severity:
    """The most serious of several severities. `none` only wins if it is alone."""
    found = list(severities)
    for severity in SEVERITIES:
        if severity in found:
            return severity
    return "none"


# ---------------------------------------------------------------------------
# 1.5.2 — tracking probe
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ActionRow:
    """One conversion action, folded over every dated row that mentions it."""

    name: str
    action_id: str | None
    status: str | None
    category: str | None
    primary_for_goal: bool
    send_to: str | None
    conversions: float
    last_conversion_at: datetime | None
    staleness_days: int | None
    evidence_ids: tuple[uuid.UUID, ...]

    @property
    def enabled(self) -> bool:
        return (self.status or "").upper() == "ENABLED"

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "conversion_action_id": self.action_id,
            "status": self.status,
            "category": self.category,
            "primary_for_goal": self.primary_for_goal,
            "send_to": self.send_to,
            "conversions": round(self.conversions, 2),
            "last_conversion_at": (
                self.last_conversion_at.isoformat() if self.last_conversion_at else None
            ),
            "staleness_days": self.staleness_days,
            "evidence_ids": [str(value) for value in self.evidence_ids],
        }


def fold_conversion_actions(
    payloads: list[dict[str, Any]],
    evidence_ids: list[uuid.UUID],
    *,
    today: date | None = None,
) -> list[ActionRow]:
    """Dated conversion-action rows folded into one row per action.

    `last_conversion_at` is the most recent **day that recorded a conversion**,
    not the most recent day the API returned. An action that has been enabled
    and silent for a month returns rows for every day of that month; reading the
    newest row as "last conversion" would report it as healthy.
    """
    if not payloads:
        return []
    today = today or datetime.now(UTC).date()
    frame = with_ids(payloads, evidence_ids)
    if frame.empty:
        return []
    # A connector that changed shape, or a hand-seeded row, must degrade to
    # "nothing recorded" rather than take the node down with a KeyError.
    frame["conversions"] = (
        numeric(frame, "conversions").fillna(0.0) if "conversions" in frame else 0.0
    )
    frame["day"] = (
        pd.to_datetime(frame["date"], errors="coerce", utc=True)
        if "date" in frame
        else pd.Series(pd.NaT, index=frame.index)
    )
    if "name" not in frame:
        frame["name"] = "(unnamed)"

    rows: list[ActionRow] = []
    for name, group in frame.groupby(frame["name"].fillna("(unnamed)"), sort=True):
        converting = group[group["conversions"] > 0]
        last = converting["day"].max() if not converting.empty else pd.NaT
        last_at = last.to_pydatetime() if pd.notna(last) else None
        rows.append(
            ActionRow(
                name=str(name),
                action_id=_first(group, "conversion_action_id"),
                status=_first(group, "status"),
                category=_first(group, "category"),
                primary_for_goal=bool(group.get("primary_for_goal", pd.Series(dtype=bool)).any()),
                send_to=_first(group, "send_to"),
                conversions=float(group["conversions"].sum()),
                last_conversion_at=last_at,
                staleness_days=(today - last_at.date()).days if last_at else None,
                evidence_ids=tuple(group["evidence_id"].dropna().unique()[:12]),
            )
        )
    rows.sort(key=lambda row: (not row.primary_for_goal, row.name))
    return rows


def tracking_alerts(rows: list[ActionRow], *, window_days: int) -> list[str]:
    """What a person has to fix before spending money against this tracking."""
    alerts: list[str] = []
    enabled = [row for row in rows if row.enabled]
    if not rows:
        alerts.append(
            "No conversion actions were read from the account, so no conversion can be "
            "attributed to a click."
        )
        return alerts
    if not enabled:
        alerts.append(
            f"None of the {len(rows)} conversion actions in the account is enabled — "
            "every conversion is going unrecorded."
        )
    if enabled and not any(row.primary_for_goal for row in enabled):
        alerts.append(
            "No enabled conversion action is marked primary, so Smart Bidding has nothing "
            "to optimise towards."
        )
    for row in enabled:
        if row.conversions <= 0:
            alerts.append(
                f"'{row.name}' is enabled but recorded no conversions in the last "
                f"{window_days} days."
            )
        elif row.staleness_days is not None and row.staleness_days > STALE_DAYS:
            alerts.append(f"'{row.name}' last recorded a conversion {row.staleness_days} days ago.")
    return alerts


def synthetic_check(
    probe: dict[str, Any] | None, rows: list[ActionRow]
) -> tuple[dict[str, Any], list[str]]:
    """The synthetic conversion probe's verdict. Returns (check, alerts).

    Read the module docstring before changing any of this: the one thing this
    function must never do is report a round trip it did not see. Google's
    conversion reporting lags by hours, so `latency_min` is filled in only when
    a conversion genuinely post-dates the probe.
    """
    alerts: list[str] = []
    if not probe:
        return (
            {
                "fired_at": None,
                "observed_in_ads_api": None,
                "latency_min": None,
                "verdict": "inconclusive",
                "detail": (
                    "No conversion page is configured for this project, so the conversion "
                    "event itself was never fired."
                ),
            },
            [
                "Set a conversion page URL for this project: without one the tracking check "
                "can only read the account, never test it."
            ],
        )

    fired_at = _parse_dt(probe.get("fired_at"))
    check: dict[str, Any] = {
        "fired_at": fired_at.isoformat() if fired_at else None,
        "probe_url": probe.get("url"),
        "tag_ids": list(probe.get("tag_ids") or []),
        "observed_in_ads_api": None,
        "latency_min": None,
        "verdict": "inconclusive",
        "detail": "",
    }

    if not probe.get("loaded"):
        reason = probe.get("error") or "no response"
        check["detail"] = f"The conversion page could not be loaded: {reason}"
        alerts.append(f"The conversion page {probe.get('url')} could not be loaded.")
        return check, alerts

    fired = [str(value) for value in (probe.get("send_to") or [])]
    if not probe.get("conversion_fired"):
        check["verdict"] = "fail"
        check["observed_in_ads_api"] = False
        loaded_tags = check["tag_ids"]
        check["detail"] = "The page loaded and no Google Ads conversion beacon fired." + (
            f" Tag containers present: {', '.join(loaded_tags)}." if loaded_tags else ""
        )
        alerts.append(
            "Loading the conversion page fired no Google Ads conversion beacon — "
            "conversions on this page are not being recorded."
        )
        return check, alerts

    known = {row.send_to: row for row in rows if row.send_to}
    matched = next((known[value] for value in fired if value in known), None)
    if matched is None:
        check["verdict"] = "fail"
        check["observed_in_ads_api"] = False
        check["detail"] = (
            f"A conversion beacon fired for {', '.join(fired) or 'an unknown target'}, "
            "which does not match any conversion action in the account."
        )
        alerts.append(
            "The conversion tag on the page points at a conversion action this account does "
            "not have. The conversion is being sent nowhere."
        )
        return check, alerts

    check["conversion_action"] = matched.name
    check["observed_in_ads_api"] = matched.conversions > 0
    if matched.conversions > 0:
        check["verdict"] = "pass"
        check["detail"] = (
            f"The beacon fired against '{matched.name}', which the API reports as receiving "
            f"{matched.conversions:.0f} conversions."
        )
    else:
        check["verdict"] = "fail"
        check["detail"] = (
            f"The beacon fired against '{matched.name}', but the API reports no conversions "
            "on that action."
        )
        alerts.append(
            f"The conversion tag fires against '{matched.name}', which has recorded nothing. "
            "The tag and the account disagree."
        )

    if fired_at and matched.last_conversion_at and matched.last_conversion_at > fired_at:
        # Only reachable when a conversion genuinely landed after we fired —
        # normally a later run resolving an earlier probe. Anything else is a
        # measurement of Google's reporting delay we did not make.
        check["latency_min"] = round(
            (matched.last_conversion_at - fired_at).total_seconds() / 60, 1
        )
    elif check["verdict"] == "pass":
        check["detail"] += (
            " Round-trip latency is not measurable in one run: Google reports conversions "
            "with a delay of several hours."
        )
    return check, alerts


# ---------------------------------------------------------------------------
# 1.5.4 — opportunity sizing
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Baseline:
    """What the account measured. Every field is `None` when it was not measured."""

    cpc: float | None = None
    ctr: float | None = None
    cvr: float | None = None
    cvr_low: float | None = None
    cvr_high: float | None = None
    value_per_conversion: float | None = None
    monthly_spend: float | None = None
    months: int = 0
    sources: tuple[str, ...] = ()

    @property
    def sizable(self) -> bool:
        return bool(self.cpc and self.cvr)

    def as_dict(self) -> dict[str, Any]:
        return {
            "cpc": self.cpc,
            "ctr": self.ctr,
            "cvr": self.cvr,
            "cvr_low": self.cvr_low,
            "cvr_high": self.cvr_high,
            "value_per_conversion": self.value_per_conversion,
            "monthly_spend": self.monthly_spend,
            "months_measured": self.months,
            "sources": list(self.sources),
        }


@dataclass(frozen=True, slots=True)
class ScenarioRow:
    budget_usd_month: float
    est_clicks: float
    est_conv: float
    est_cpa: float | None
    est_revenue: float | None
    confidence_interval: str
    demand_capped: bool
    inputs: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "budget_usd_month": self.budget_usd_month,
            "est_clicks": self.est_clicks,
            "est_conv": self.est_conv,
            "est_cpa": self.est_cpa,
            "est_revenue": self.est_revenue,
            "confidence_interval": self.confidence_interval,
            "demand_capped": self.demand_capped,
            "inputs": self.inputs,
        }


def account_baseline(
    campaign_rows: list[dict[str, Any]], *, economics: dict[str, Any] | None = None
) -> Baseline:
    """The measured rates a forecast may be built on.

    Nothing here is a default. A rate that the account never measured comes back
    `None`, and `size()` refuses to forecast without one — which is the whole
    point: a plausible-looking scenario built on an invented conversion rate is
    the most expensive thing this report could contain.
    """
    if not campaign_rows:
        return Baseline(sources=("no campaign history",))
    frame = pd.DataFrame(campaign_rows)
    for column in ("cost", "clicks", "impressions", "conversions", "conversion_value"):
        # A column the connector did not write is zero, not a KeyError: a
        # partial pull should narrow the baseline, not lose the whole node.
        if column not in frame:
            frame[column] = 0.0
        frame[column] = numeric(frame, column).fillna(0.0)

    cost = float(frame["cost"].sum())
    clicks = float(frame["clicks"].sum())
    impressions = float(frame["impressions"].sum())
    conversions = float(frame["conversions"].sum())
    value = float(frame["conversion_value"].sum())

    months = frame["month"].nunique() if "month" in frame else 0
    monthly = _monthly_rates(frame)

    cvr = conversions / clicks if clicks else None
    baseline = Baseline(
        cpc=round(cost / clicks, 4) if clicks else None,
        ctr=round(clicks / impressions, 5) if impressions else None,
        cvr=round(cvr, 5) if cvr else None,
        cvr_low=monthly[0],
        cvr_high=monthly[1],
        # `value` is zero for a lead-gen account that counts form fills without
        # a value, which is not the same as "each conversion is worth nothing".
        # Returning 0.0 here would put `est_revenue: 0` in every scenario and
        # suppress the CRM fallback below, silently.
        value_per_conversion=round(value / conversions, 2) if conversions and value else None,
        monthly_spend=round(cost / months, 2) if months else None,
        months=int(months),
        sources=("google_ads campaign history",),
    )
    if baseline.value_per_conversion is None and economics:
        # Falling back to the CRM's ACV is a *stated* substitution, not a silent
        # one: it lands in `sources`, the node passes `sources` to the model, and
        # the model writes the assumption the report prints.
        acv = _float((economics or {}).get("ltv_estimate")) or _float((economics or {}).get("acv"))
        if acv:
            baseline = replace(
                baseline,
                value_per_conversion=acv,
                sources=(
                    *baseline.sources,
                    "CRM lifetime value (the account records no conversion value)",
                ),
            )
    return baseline


def size(
    baseline: Baseline,
    priced_keywords: list[dict[str, Any]],
    *,
    budgets: list[float] | None = None,
) -> tuple[list[ScenarioRow], list[str]]:
    """Budget scenarios. Returns (scenarios, reasons it could not size).

    The cap is the part worth reading: clicks are limited by the demand the
    keyword set actually carries, so doubling a budget past the reachable market
    stops buying clicks and starts buying nothing. A forecast that scales
    linearly for ever is the one people act on and then miss.
    """
    blockers: list[str] = []
    if not baseline.cpc:
        blockers.append("the account has no measured cost per click")
    if not baseline.cvr:
        blockers.append("the account has no measured conversion rate")
    if blockers:
        return [], blockers

    cpc = float(baseline.cpc or 0.0)
    cvr = float(baseline.cvr or 0.0)
    reachable = _reachable_clicks(priced_keywords, baseline)
    budgets = budgets or _budget_ladder(baseline, cpc=cpc, reachable=reachable)

    scenarios: list[ScenarioRow] = []
    for budget in budgets:
        uncapped = budget / cpc
        clicks = min(uncapped, reachable) if reachable else uncapped
        capped = bool(reachable and uncapped > reachable)
        conversions = clicks * cvr
        spend = clicks * cpc
        low = clicks * (baseline.cvr_low or cvr)
        high = clicks * (baseline.cvr_high or cvr)
        scenarios.append(
            ScenarioRow(
                budget_usd_month=round(budget, 2),
                est_clicks=round(clicks, 1),
                est_conv=round(conversions, 1),
                est_cpa=round(spend / conversions, 2) if conversions else None,
                est_revenue=(
                    round(conversions * baseline.value_per_conversion, 2)
                    if baseline.value_per_conversion
                    else None
                ),
                confidence_interval=f"{low:.0f}–{high:.0f} conversions/month",
                demand_capped=capped,
                inputs={
                    "cpc": round(cpc, 2),
                    "cvr": round(cvr, 5),
                    "reachable_clicks": round(reachable, 1) if reachable else None,
                    "spend_at_cap": round(spend, 2) if capped else None,
                },
            )
        )
    return scenarios, []


def _reachable_clicks(priced_keywords: list[dict[str, Any]], baseline: Baseline) -> float:
    """Clicks the keyword set can actually deliver in a month."""
    if not priced_keywords or not baseline.ctr:
        return 0.0
    volume = 0.0
    for row in priced_keywords:
        value = _float(row.get("volume"))
        if value:
            volume += value
    return volume * baseline.ctr * MAX_IMPRESSION_SHARE


def _budget_ladder(baseline: Baseline, *, cpc: float, reachable: float) -> list[float]:
    """Three budgets worth asking about, anchored on something measured."""
    if baseline.monthly_spend:
        return [round(baseline.monthly_spend * multiple, 2) for multiple in SPEND_MULTIPLES]
    full = reachable * cpc
    if full <= 0:
        return []
    return [round(full * fraction, 2) for fraction in DEMAND_FRACTIONS]


def _monthly_rates(frame: pd.DataFrame) -> tuple[float | None, float | None]:
    """The spread of monthly conversion rates — the honest error bar on a forecast."""
    if "month" not in frame:
        return None, None
    grouped = frame.groupby("month")[["clicks", "conversions"]].sum()
    grouped = grouped[grouped["clicks"] > 0]
    if len(grouped) < 2:
        return None, None
    rates = (grouped["conversions"] / grouped["clicks"]).sort_values()
    return round(float(rates.quantile(0.25)), 5), round(float(rates.quantile(0.75)), 5)


# ---------------------------------------------------------------------------
# shared
# ---------------------------------------------------------------------------


def _key(url: str) -> str:
    return url.strip().rstrip("/").lower()


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else number


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _first(group: pd.DataFrame, column: str) -> Any:
    if column not in group:
        return None
    values = group[column].dropna()
    return values.iloc[0] if not values.empty else None


def _parse_dt(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
