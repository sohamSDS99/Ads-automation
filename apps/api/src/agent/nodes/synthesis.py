"""Assembling the `ResearchReport` from what the DAG produced (PRD §11).

The single most important sentence in PRD §11 is this one: "`markdown` is
rendered from this object by a deterministic Jinja2 template — the LLM does not
write the final markdown." This module is the other half of that guarantee. If
the model does not write the document, something has to build the object, and
the honest answer is that **almost all of it is a projection of node outputs**.
Nineteen nodes already produced validated, cited findings; asking a model to
restate two thousand priced keywords would cost money, take minutes and
introduce a way for the report to disagree with the run that produced it.

So the division of labour is:

* **Python assembles every section** — `business_context` through `readiness`,
  the priced keyword list, the evidence citations and the cost. Nothing here
  invents a value; every field is copied or joined from a node output.
* **Python decides `launch_readiness`**, from the rules in `VERDICT_RULES`
  below. A launch verdict is a rubric applied to measurements, and a rubric in
  code can be read, argued with and unit-tested. See the note on that decision
  in `verdict()`.
* **The model writes what only prose can carry** — the executive summary, the
  blockers and next actions as cited `Claim`s, and the open questions. That is
  node 1.6.1's completion, and it is handed the computed verdict rather than
  asked to guess one.

Node 1.6.2 then reviews the finished object with a different model family, and
one blocking issue buys exactly one re-synthesis (PRD §10).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from pydantic import BaseModel, ValidationError

from agent.db.models import Project, Run
from agent.export.contract import (
    SCHEMA_VERSION,
    AccountLearnings,
    AudienceList,
    BusinessContext,
    Claim,
    CompetitiveLandscape,
    Competitor,
    CompetitorAd,
    ComplianceGuardrails,
    Confidence,
    ContentGap,
    ConversionAction,
    DemandMap,
    FailedExperiment,
    IcpExclusion,
    IcpSegment,
    KeywordPageMapping,
    LaunchReadiness,
    MarketCoverage,
    MessageCluster,
    NegativeKeyword,
    PageAudit,
    PerformanceFinding,
    PricedKeyword,
    Product,
    ProfitableTerm,
    Readiness,
    RegulatedTerm,
    ResearchReport,
    Scenario,
    SpendEstimate,
    SyntheticCheck,
    WastefulTerm,
    Whitespace,
)

log = structlog.get_logger(__name__)

Outputs = Mapping[str, Mapping[str, Any]]

#: Called with a one-line reason each time a row does not fit the contract. See
#: `_safe`. `assemble` defaults it to a no-op so callers that do not care — the
#: test suite, mostly — do not have to pass one.
Drop = Callable[[str], None]

#: Keywords carried into the report. The CSV export is the deliverable a person
#: pastes into Google Ads Editor, so the cut is generous; `priced_keyword_list`
#: is also the biggest thing in the payload, and an unbounded join of a runaway
#: 1.4.1 would put a hundred megabytes in a JSONB column.
MAX_PRICED_KEYWORDS = 5_000

#: Evidence ids the model is offered to cite blockers and actions from. The
#: report as a whole cites everything the nodes cited; this bounds only what one
#: prompt holds.
MAX_CITABLE = 60


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Fact:
    """One computed finding, with the evidence that establishes it.

    Blockers are `Claim`s in the report, and a `Claim` needs at least one
    citation. So a rule does not just decide *that* something blocks a launch —
    it has to hand over the rows that prove it, or say it cannot.
    """

    statement: str
    evidence_ids: tuple[uuid.UUID, ...] = ()

    def as_claim(self, confidence: Confidence = "high") -> Claim | None:
        if not self.evidence_ids:
            return None
        return Claim(
            statement=self.statement,
            evidence_ids=list(self.evidence_ids),
            confidence=confidence,
        )


def verdict(outputs: Outputs) -> tuple[LaunchReadiness, list[Fact], list[Fact]]:
    """`(launch_readiness, blocking facts, fixable facts)`.

    **Why this is not the model's call.** PRD §10 gives node 1.6.1 the whole
    report, and 1.6.2 is asked to check "launch-readiness verdict consistency" —
    which presumes the verdict can disagree with the findings. It cannot, if it
    is derived from them. A model asked for a go/no-go over twenty pages of
    input will occasionally return `go` under a critical blocker it summarised
    correctly two paragraphs earlier, and that is the single worst thing this
    report could get wrong. So the rubric is code, the model is shown the answer
    it has to write prose consistent with, and 1.6.2 checks that consistency —
    which is the failure that actually happens.
    """
    audit = outputs.get("1.5.1") or {}
    tracking = outputs.get("1.5.2") or {}
    consent = outputs.get("1.5.3") or {}
    sizing = outputs.get("1.5.4") or {}

    blocking: list[Fact] = []
    fixable: list[Fact] = []

    pages = _rows(audit, "pages")
    critical = [page for page in pages if page.get("severity") == "critical"]
    for page in critical:
        blocking.append(
            Fact(
                f"The landing page {page.get('url')} is not fit to receive paid traffic: "
                + "; ".join(str(issue) for issue in (page.get("issues") or [])[:3]),
                _row_ids(page),
            )
        )
    mapping = _rows(outputs.get("1.4.5") or {}, "mapping")
    for url in audit.get("unreachable") or []:
        # An unreachable page has no evidence row of its own — that is what
        # unreachable means. The citation is 1.4.5's mapping row, which is the
        # evidence that we decided to send traffic there in the first place.
        blocking.append(
            Fact(
                f"The landing page {url} could not be fetched, and keywords are mapped to it.",
                tuple(
                    value
                    for row in mapping
                    if str(row.get("best_url") or "") == str(url)
                    for value in _row_ids(row)
                )[:6],
            )
        )
    major = [page for page in pages if page.get("severity") == "major"]
    if major:
        fixable.append(
            Fact(
                f"{len(major)} landing page(s) have major issues to fix before launch: "
                + ", ".join(str(page.get("url")) for page in major[:3]),
                tuple(value for page in major[:3] for value in _row_ids(page))[:6],
            )
        )

    actions = _rows(tracking, "conversion_actions")
    live = [
        row
        for row in actions
        if str(row.get("status") or "").upper() == "ENABLED" and float(row.get("conversions") or 0)
    ]
    if actions and not live:
        blocking.append(
            Fact(
                "No enabled conversion action has recorded a conversion, so no spend can be "
                "attributed to an outcome.",
                tuple(value for row in actions[:4] for value in _row_ids(row))[:6],
            )
        )
    elif not actions:
        fixable.append(Fact("Conversion tracking could not be read from the account."))

    check = tracking.get("synthetic_check") or {}
    if check.get("verdict") == "fail":
        blocking.append(
            Fact(
                str(check.get("detail") or "The conversion page fires no conversion beacon."),
                tuple(value for row in actions[:4] for value in _row_ids(row))[:6],
            )
        )
    elif check.get("verdict") == "inconclusive":
        fixable.append(Fact("The conversion tag could not be tested end to end."))
    for alert in tracking.get("alerts") or []:
        fixable.append(Fact(str(alert)))

    lists = _rows(consent, "lists")
    if lists and not any(row.get("usable") for row in lists):
        fixable.append(
            Fact(
                "No audience list is cleared for use in the markets in scope.",
                tuple(value for row in lists[:4] for value in _row_ids(row))[:6],
            )
        )

    for reason in sizing.get("blockers") or []:
        fixable.append(Fact(f"The opportunity could not be sized: {reason}"))

    if blocking:
        return "no_go", blocking, fixable
    if fixable:
        return "go_with_fixes", [], fixable
    return "go", [], []


# ---------------------------------------------------------------------------
# evidence
# ---------------------------------------------------------------------------


def cited_evidence_ids(outputs: Outputs) -> list[uuid.UUID]:
    """Every evidence id any upstream node cited, deduped, in a stable order.

    Node 1.6.1 has to `gather()` exactly these: the executor fails a node that
    cites evidence it did not gather, and the report is nothing but a fold of
    what the other nodes cited.
    """
    found: dict[uuid.UUID, None] = {}
    for node_id in sorted(outputs):
        for value in _walk_ids(outputs[node_id]):
            found.setdefault(value, None)
    return list(found)


def _walk_ids(node: Any) -> Iterable[uuid.UUID]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "evidence_ids" and isinstance(value, list | tuple):
                for item in value:
                    parsed = _as_uuid(item)
                    if parsed is not None:
                        yield parsed
            else:
                yield from _walk_ids(value)
    elif isinstance(node, list | tuple):
        for item in node:
            yield from _walk_ids(item)


def _as_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def degraded_sources(outputs: Outputs) -> list[str]:
    """Which sources came back partial, gathered from every node's `coverage`."""
    found: set[str] = set()
    for payload in outputs.values():
        for note in payload.get("coverage") or []:
            found.add(str(note))
    return sorted(found)


# ---------------------------------------------------------------------------
# the sections
# ---------------------------------------------------------------------------


def business_context(outputs: Outputs, drop: Drop = lambda _: None) -> BusinessContext:
    economics = outputs.get("1.1.1") or {}
    icp = outputs.get("1.1.2") or {}
    negative = outputs.get("1.1.3") or {}
    markets = outputs.get("1.1.4") or {}
    compliance = outputs.get("1.1.5") or {}
    return BusinessContext(
        products=_safe(Product, [_ids(row) for row in _rows(economics, "products")], drop),
        ltv_estimate=_float(economics.get("ltv_estimate")),
        target_cac=_float(economics.get("target_cac")),
        payback_months=_float(economics.get("payback_months")),
        segments=_safe(IcpSegment, [_ids(row) for row in _rows(icp, "segments")], drop),
        exclusions=_safe(IcpExclusion, [_ids(row) for row in _rows(negative, "exclusions")], drop),
        markets=_safe(MarketCoverage, [_ids(row) for row in _rows(markets, "markets")], drop),
        compliance=(
            ComplianceGuardrails(
                prohibited_claims=[str(x) for x in compliance.get("prohibited_claims") or []],
                required_disclaimers=[str(x) for x in compliance.get("required_disclaimers") or []],
                regulated_terms=_safe(
                    RegulatedTerm, [_ids(row) for row in _rows(compliance, "regulated_terms")], drop
                ),
                confidence=_confidence(compliance.get("confidence")),
            )
            if compliance
            else None
        ),
    )


def account_learnings(outputs: Outputs, drop: Drop = lambda _: None) -> AccountLearnings:
    history = outputs.get("1.2.1") or {}
    pnl = outputs.get("1.2.2") or {}
    failures = outputs.get("1.2.3") or {}
    return AccountLearnings(
        winners=_safe(PerformanceFinding, [_delta(row) for row in _rows(history, "winners")], drop),
        losers=_safe(PerformanceFinding, [_delta(row) for row in _rows(history, "losers")], drop),
        structural_findings=[str(x) for x in history.get("structural_findings") or []],
        profitable_terms=_safe(
            ProfitableTerm, [_ids(_conv(row)) for row in _rows(pnl, "profitable_terms")], drop
        ),
        wasteful_terms=_safe(
            WastefulTerm, [_ids(_conv(row)) for row in _rows(pnl, "wasteful_terms")], drop
        ),
        tried_and_failed=_safe(
            FailedExperiment, [_ids(row) for row in _rows(failures, "tried_and_failed")], drop
        ),
    )


def competitive_landscape(outputs: Outputs, drop: Drop = lambda _: None) -> CompetitiveLandscape:
    competitors = outputs.get("1.3.1") or {}
    corpus = outputs.get("1.3.2") or {}
    spend = outputs.get("1.3.3") or {}
    claim = outputs.get("1.3.4") or {}
    return CompetitiveLandscape(
        competitors=_safe(Competitor, _overlap(_rows(competitors, "competitors")), drop),
        ads=_safe(CompetitorAd, [_ids(row) for row in _rows(corpus, "ads")], drop),
        message_clusters=_safe(
            MessageCluster, [_ids(row) for row in _rows(corpus, "message_clusters")], drop
        ),
        spend_estimates=_safe(
            SpendEstimate, [_ids(_spend_range(row)) for row in _rows(spend, "estimates")], drop
        ),
        whitespace=_safe(Whitespace, [_ids(row) for row in _rows(claim, "whitespace")], drop),
        recommended_claim=claim.get("recommended_claim") or None,
        substantiation_required=[str(x) for x in claim.get("substantiation_required") or []],
    )


def demand_map(outputs: Outputs, drop: Drop = lambda _: None) -> DemandMap:
    metrics = outputs.get("1.4.3") or {}
    blocklist = outputs.get("1.4.4") or {}
    mapping = outputs.get("1.4.5") or {}
    totals = metrics.get("totals") or {}
    counted = int(totals.get("terms_priced") or 0) + int(totals.get("terms_unpriced") or 0)
    return DemandMap(
        total_keywords=counted,
        negatives=_safe(
            NegativeKeyword, [_ids(row) for row in _rows(blocklist, "negatives")], drop
        ),
        mapping=_safe(KeywordPageMapping, [_ids(row) for row in _rows(mapping, "mapping")], drop),
        content_gaps=_safe(ContentGap, [_ids(row) for row in _rows(mapping, "content_gaps")], drop),
    )


def readiness(outputs: Outputs, drop: Drop = lambda _: None) -> Readiness:
    audit = outputs.get("1.5.1") or {}
    tracking = outputs.get("1.5.2") or {}
    consent = outputs.get("1.5.3") or {}
    sizing = outputs.get("1.5.4") or {}
    check = tracking.get("synthetic_check") or {}
    return Readiness(
        pages=_safe(PageAudit, [_ids(row) for row in _rows(audit, "pages")], drop),
        conversion_actions=_safe(
            ConversionAction, [_ids(row) for row in _rows(tracking, "conversion_actions")], drop
        ),
        synthetic_check=SyntheticCheck.model_validate(check) if check else None,
        alerts=[str(x) for x in tracking.get("alerts") or []],
        lists=_safe(AudienceList, [_ids(row) for row in _rows(consent, "lists")], drop),
        scenarios=_safe(Scenario, [_ids(row) for row in _rows(sizing, "scenarios")], drop),
    )


def priced_keywords(outputs: Outputs, drop: Drop = lambda _: None) -> list[PricedKeyword]:
    """The CSV export, joined from four nodes.

    `PricedKeyword` forbids extra fields on purpose (§12: someone pastes this
    into Google Ads Editor), so this join is explicit about every column rather
    than splatting a node's payload into it.
    """
    universe = {str(row.get("term")): row for row in _rows(outputs.get("1.4.1") or {}, "keywords")}
    classified = {
        str(row.get("term")): row for row in _rows(outputs.get("1.4.2") or {}, "classified")
    }
    mapped = {
        str(row.get("term_cluster")): row for row in _rows(outputs.get("1.4.5") or {}, "mapping")
    }
    negatives = {
        str(row.get("term")): row for row in _rows(outputs.get("1.4.4") or {}, "negatives")
    }

    rows: list[dict[str, Any]] = []
    for metric in _rows(outputs.get("1.4.3") or {}, "metrics")[:MAX_PRICED_KEYWORDS]:
        term = str(metric.get("term") or "")
        if not term:
            continue
        label = classified.get(term) or {}
        cluster = str(label.get("cluster") or "")
        page = mapped.get(cluster) or {}
        markets = (universe.get(term) or {}).get("market") or []
        candidate = {
            "term": term,
            "market": str(markets[0]) if markets else None,
            "intent": _intent(label.get("intent")),
            "funnel_stage": label.get("funnel_stage") or None,
            "volume": _int(metric.get("volume")),
            "cpc_low": _float(metric.get("cpc_low")),
            "cpc_high": _float(metric.get("cpc_high")),
            "competition": _float(metric.get("competition")),
            "seasonality_index": [float(value) for value in metric.get("seasonality_index") or []],
            "trend_yoy": _float(metric.get("trend_yoy")),
            "best_url": page.get("best_url") or None,
            "verdict": _verdict_label(page.get("verdict")),
            "match_type": _match_type((negatives.get(term) or {}).get("match_type")),
        }
        rows.append(candidate)
    # Validated as a batch, through the same guard as every other section: a
    # keyword whose vendor payload carried eleven seasonality values should cost
    # the run one row of a CSV, not the report.
    return _safe(PricedKeyword, rows, drop)


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------


def assemble(
    *,
    project: Project,
    run: Run,
    outputs: Outputs,
    summary: str,
    launch_readiness: LaunchReadiness,
    launch_blockers: list[Claim],
    next_actions: list[Claim],
    open_questions: list[str],
    cost_usd: float,
    generated_at: datetime | None = None,
    drop: Drop | None = None,
) -> ResearchReport:
    """One validated `ResearchReport`. Same outputs in, same report out.

    `drop` is called once per row that did not fit the contract; see `_safe`.
    """
    drop = drop or (lambda _: None)
    report = ResearchReport(
        schema_version=SCHEMA_VERSION,
        project_id=project.id,
        run_id=run.id,
        generated_at=generated_at or datetime.now(UTC),
        executive_summary=summary,
        launch_readiness=launch_readiness,
        launch_blockers=launch_blockers,
        business_context=business_context(outputs, drop),
        account_learnings=account_learnings(outputs, drop),
        competitive_landscape=competitive_landscape(outputs, drop),
        demand_map=demand_map(outputs, drop),
        readiness=readiness(outputs, drop),
        priced_keyword_list=priced_keywords(outputs, drop),
        recommended_next_actions=next_actions,
        open_questions=open_questions,
        degraded_sources=degraded_sources(outputs),
        cost_usd=cost_usd,
    )
    log.info(
        "synthesis.assembled",
        run_id=str(run.id),
        verdict=report.launch_readiness,
        keywords=len(report.priced_keyword_list),
        evidence=len(report.evidence_ids()),
    )
    return report


def citable(outputs: Outputs, evidence: Mapping[uuid.UUID, str]) -> list[dict[str, str]]:
    """The evidence the model may cite, newest and most-cited first.

    A prompt cannot hold three thousand rows, so this is a ranked slice: an id
    several nodes already cited is the one a blocker is most likely to need.
    """
    counts: dict[uuid.UUID, int] = {}
    for node_id in sorted(outputs):
        for value in _walk_ids(outputs[node_id]):
            counts[value] = counts.get(value, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))
    return [
        {"id": str(value), "summary": evidence.get(value, "")}
        for value, _ in ranked[:MAX_CITABLE]
        if value in evidence
    ]


# ---------------------------------------------------------------------------
# small conversions
# ---------------------------------------------------------------------------


def _safe[T: BaseModel](model: type[T], rows: list[dict[str, Any]], drop: Drop) -> list[T]:
    """Validate rows, dropping the ones that do not fit rather than raising.

    This is the module docstring's principle made operational. Nineteen nodes
    feed this assembly and their output models are not the contract's; the two
    will disagree again (they already did once, over whether `firmographics` is
    a sentence or a map). When they do, the choice is between losing one row of
    one section and losing a forty-minute run at its very last node. It is not a
    close call — but the drop is *reported*, never silent: `dropped_rows` on
    node 1.6.1's output is how it surfaces.
    """
    kept: list[T] = []
    for row in rows:
        try:
            kept.append(model.model_validate(row))
        except ValidationError as exc:
            drop(f"{model.__name__}: {exc.errors()[0].get('msg', 'invalid')}")
            log.warning("synthesis.row_dropped", model=model.__name__, error=str(exc)[:200])
    return kept


def _delta(row: dict[str, Any]) -> dict[str, Any]:
    """1.2.1 reports `metric_delta` as a percentage *number*; §11's field is the
    label a reader sees. Formatting it here keeps both honest — the alternative
    was dropping every winner and loser out of the report, which is exactly what
    the guard caught the first time this assembly ran against real node output.
    """
    value = row.get("metric_delta")
    if isinstance(value, int | float):
        return {**_ids(row), "metric_delta": f"{value:+.1f}% CPA"}
    return _ids(row)


def _overlap(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rescale 1.3.1's overlap score into the 0–1 the report renders as a percent.

    Node 1.3.1 says so itself: the score "ranks these domains against each
    other, it is not a percentage of anything". The report prints it through
    `ratio_pct`. Dividing by the strongest overlap in the set preserves 1.3.1's
    ranking exactly and puts it on the scale the column claims — and the raw
    figure rides along so nothing is actually lost.
    """
    scores = [
        value
        for value in (_float(row.get("overlap_score")) for row in rows)
        if value is not None and value > 0
    ]
    largest = max(scores, default=0.0)
    if largest <= 1.0:
        return [_ids(row) for row in rows]
    return [
        {
            **_ids(row),
            "overlap_score": round((_float(row.get("overlap_score")) or 0.0) / largest, 4),
            "overlap_score_raw": row.get("overlap_score"),
        }
        for row in rows
    ]


def _row_ids(row: Mapping[str, Any]) -> tuple[uuid.UUID, ...]:
    """The evidence ids on one record, parsed. Empty when it carries none."""
    parsed = (_as_uuid(value) for value in (row.get("evidence_ids") or []))
    return tuple(value for value in parsed if value is not None)


def _rows(payload: Mapping[str, Any], key: str) -> list[dict[str, Any]]:
    return [row for row in (payload.get(key) or []) if isinstance(row, dict)]


def _ids(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce a node's string evidence ids into the UUIDs the contract wants."""
    values = row.get("evidence_ids")
    if not isinstance(values, list):
        return row
    parsed = [_as_uuid(value) for value in values]
    return {**row, "evidence_ids": [value for value in parsed if value is not None]}


def _conv(row: dict[str, Any]) -> dict[str, Any]:
    """Nodes 1.2.2 writes `conversions`; the contract's field is `conv`."""
    if "conversions" in row and "conv" not in row:
        return {**row, "conv": row["conversions"]}
    return row


def _spend_range(row: dict[str, Any]) -> dict[str, Any]:
    """1.3.3 reports a low/high pair; §11's field is one human-readable range."""
    if row.get("est_monthly_spend_range"):
        return row
    low, high = row.get("est_monthly_spend_low"), row.get("est_monthly_spend_high")
    currency = row.get("currency") or ""
    if low is None and high is None:
        return {**row, "est_monthly_spend_range": "not estimated"}
    return {
        **row,
        "est_monthly_spend_range": f"{currency}{low:,.0f}–{currency}{high:,.0f}".strip(),
    }


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value: Any) -> int | None:
    number = _float(value)
    return int(number) if number is not None else None


def _confidence(value: Any) -> Any:
    return value if value in {"high", "medium", "low"} else None


def _intent(value: Any) -> Any:
    allowed = {
        "transactional",
        "commercial_investigation",
        "informational",
        "navigational",
        "irrelevant",
    }
    return value if value in allowed else None


def _verdict_label(value: Any) -> Any:
    return value if value in {"good_fit", "weak_fit", "gap"} else None


def _match_type(value: Any) -> str:
    return value if value in {"broad", "phrase", "exact"} else "phrase"
