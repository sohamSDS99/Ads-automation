"""Assembling the report from node outputs (PRD §11), and the launch verdict.

The failure this suite is written against is a quiet one. Every node can
succeed, the report can render, the PDF can download — and the verdict can say
`go` over a landing page that returns 404, or the keyword CSV can lose the
column that makes it importable. Nothing raises. So the assertions here are
about *agreement*: the report has to say what the nodes measured.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from agent.export.contract import ResearchReport
from agent.export.markdown import render_markdown
from agent.export.view import SECTION_TITLES
from agent.nodes import synthesis

EV = [uuid.UUID(f"{index:032x}") for index in range(1, 12)]


class FakeProject:
    id = uuid.UUID("11111111-1111-4111-8111-111111111111")
    name = "Northwind Safety"
    domain = "northwind.test"


class FakeRun:
    id = uuid.UUID("22222222-2222-4222-8222-222222222222")


def outputs(**overrides: Any) -> dict[str, dict[str, Any]]:
    """A complete, healthy run: every node succeeded and nothing blocks."""
    base: dict[str, dict[str, Any]] = {
        "1.1.1": {
            "products": [{"name": "SDS Manager", "acv": 4800.0, "gross_margin_pct": 82.0}],
            "ltv_estimate": 14400.0,
            "target_cac": 4800.0,
            "payback_months": 12.0,
            "coverage": [],
        },
        "1.1.2": {
            "segments": [
                {
                    "label": "Mid-market manufacturers",
                    "share_of_revenue_pct": 61.0,
                    "evidence_ids": [str(EV[0])],
                }
            ]
        },
        "1.1.3": {"exclusions": [{"persona": "Sole trader", "disqualifier": "no budget"}]},
        "1.1.4": {"markets": [{"country": "US", "language": "en", "currency": "USD"}]},
        "1.1.5": {
            "prohibited_claims": ["100% compliant"],
            "required_disclaimers": ["Not legal advice"],
            "regulated_terms": [{"term": "OSHA", "rule": "no endorsement implied"}],
            "confidence": "high",
        },
        "1.2.1": {
            "winners": [{"campaign": "Brand", "metric_delta": "-18% CPA", "period": "H2"}],
            "losers": [],
            "structural_findings": ["Brand and generic share one budget"],
        },
        "1.2.2": {
            "profitable_terms": [
                {"term": "sds software", "cost": 900.0, "conversions": 6.0, "cpa": 150.0}
            ],
            "wasteful_terms": [
                {
                    "term": "free sds",
                    "cost": 1880.0,
                    "conversions": 0.0,
                    "recommended_action": "exclude",
                }
            ],
            "totals": {"cost": 2780.0, "wasted_spend": 1880.0},
        },
        "1.2.3": {"tried_and_failed": [{"what": "Broad match test", "outcome": "CPA tripled"}]},
        "1.3.1": {"competitors": [{"domain": "rival.test", "name": "Rival", "overlap_score": 0.4}]},
        "1.3.2": {
            "ads": [{"advertiser": "Rival", "headline": "SDS in minutes"}],
            "message_clusters": [{"theme": "speed", "frequency": 12, "advertisers": ["Rival"]}],
        },
        "1.3.3": {
            "estimates": [
                {
                    "competitor": "Rival",
                    "est_monthly_spend_low": 8000,
                    "est_monthly_spend_high": 14000,
                    "currency": "$",
                    "method": "impression share x CPC",
                    "confidence": "medium",
                }
            ]
        },
        "1.3.4": {
            "whitespace": [{"claim": "Audited by chemists", "our_proof": "staff list"}],
            "recommended_claim": "Audited by chemists, not scanned by software",
            "substantiation_required": ["Publish the audit process"],
        },
        "1.4.1": {"keywords": [{"term": "sds software", "market": ["US"]}]},
        "1.4.2": {
            "classified": [
                {
                    "term": "sds software",
                    "intent": "transactional",
                    "funnel_stage": "bottom",
                    "cluster": "sds software",
                }
            ]
        },
        "1.4.3": {
            "metrics": [
                {
                    "term": "sds software",
                    "volume": 2400,
                    "cpc_low": 4.1,
                    "cpc_high": 9.7,
                    "competition": 0.8,
                    "seasonality_index": [1.0] * 12,
                }
            ],
            "totals": {"terms_priced": 2100, "terms_unpriced": 40},
        },
        "1.4.4": {
            "negatives": [{"term": "free", "match_type": "broad", "source": "wasteful_terms"}]
        },
        "1.4.5": {
            "mapping": [
                {
                    "term_cluster": "sds software",
                    "best_url": "https://northwind.test/software",
                    "relevance_score": 0.8,
                    "verdict": "good_fit",
                    "evidence_ids": [str(EV[1])],
                }
            ],
            "content_gaps": [{"cluster": "sds authoring", "required_page_type": "solution page"}],
        },
        "1.5.1": {
            "pages": [
                {
                    "url": "https://northwind.test/software",
                    "lcp_ms": 1900.0,
                    "cls": 0.02,
                    "mobile_ok": True,
                    "form_fields_count": 3,
                    "trust_signals": ["iso_certification"],
                    "issues": [],
                    "severity": "none",
                    "evidence_ids": [str(EV[2])],
                }
            ],
            "unreachable": [],
        },
        "1.5.2": {
            "conversion_actions": [
                {
                    "name": "Demo request",
                    "status": "ENABLED",
                    "conversions": 23.0,
                    "staleness_days": 2,
                    "evidence_ids": [str(EV[3])],
                }
            ],
            "synthetic_check": {"verdict": "pass", "observed_in_ads_api": True},
            "alerts": [],
        },
        "1.5.3": {
            "lists": [
                {
                    "name": "All converters",
                    "size": 48200,
                    "usable": True,
                    "consent_basis": "consent at form submission",
                    "evidence_ids": [str(EV[4])],
                }
            ]
        },
        "1.5.4": {
            "scenarios": [
                {
                    "budget_usd_month": 1000.0,
                    "est_clicks": 200.0,
                    "est_conv": 10.5,
                    "est_cpa": 95.2,
                    "assumptions": ["CPC holds at $5"],
                }
            ],
            "blockers": [],
        },
    }
    for key, value in overrides.items():
        base[key] = value
    return base


def report(**overrides: Any) -> ResearchReport:
    payload = outputs(**overrides)
    launch, blocking, _ = synthesis.verdict(payload)
    return synthesis.assemble(
        project=FakeProject(),  # type: ignore[arg-type]
        run=FakeRun(),  # type: ignore[arg-type]
        outputs=payload,
        summary="A short summary.",
        launch_readiness=launch,
        launch_blockers=[claim for claim in (f.as_claim() for f in blocking) if claim],
        next_actions=[],
        open_questions=[],
        cost_usd=1.23,
        generated_at=datetime(2026, 9, 17, 9, 0, tzinfo=UTC),
    )


# ---------------------------------------------------------------------------
# the verdict
# ---------------------------------------------------------------------------


def test_a_clean_run_is_a_go() -> None:
    launch, blocking, fixable = synthesis.verdict(outputs())
    assert launch == "go"
    assert blocking == [] and fixable == []


def test_a_broken_landing_page_is_a_no_go_and_cites_the_page() -> None:
    """The verdict that matters. A model summarising this correctly and still
    answering "go" is the single worst thing this report could do."""
    payload = outputs()
    payload["1.5.1"]["pages"][0].update(severity="critical", issues=["the page returns HTTP 404"])
    launch, blocking, _ = synthesis.verdict(payload)
    assert launch == "no_go"
    assert blocking[0].evidence_ids == (EV[2],)
    assert "HTTP 404" in blocking[0].statement


def test_an_unreachable_page_is_cited_from_the_mapping_that_chose_it() -> None:
    """It has no evidence row of its own — that is what unreachable means — so
    the citation is the mapping row that decided to send traffic there."""
    payload = outputs()
    payload["1.5.1"]["unreachable"] = ["https://northwind.test/software"]
    _, blocking, _ = synthesis.verdict(payload)
    assert blocking[0].evidence_ids == (EV[1],)


def test_tracking_that_records_nothing_blocks_a_launch() -> None:
    payload = outputs()
    payload["1.5.2"]["conversion_actions"][0]["conversions"] = 0.0
    launch, blocking, _ = synthesis.verdict(payload)
    assert launch == "no_go"
    assert "no spend can be attributed" in blocking[0].statement


def test_a_failing_synthetic_check_blocks_a_launch() -> None:
    payload = outputs()
    payload["1.5.2"]["synthetic_check"] = {"verdict": "fail", "detail": "no beacon fired"}
    launch, blocking, _ = synthesis.verdict(payload)
    assert launch == "no_go"
    assert any("no beacon fired" in fact.statement for fact in blocking)


def test_an_inconclusive_check_is_a_fix_not_a_block() -> None:
    payload = outputs()
    payload["1.5.2"]["synthetic_check"] = {"verdict": "inconclusive"}
    launch, blocking, fixable = synthesis.verdict(payload)
    assert launch == "go_with_fixes"
    assert blocking == []
    assert any("end to end" in fact.statement for fact in fixable)


def test_a_major_page_issue_downgrades_but_does_not_stop() -> None:
    payload = outputs()
    payload["1.5.1"]["pages"][0].update(severity="major", issues=["LCP is 3.2s"])
    launch, blocking, fixable = synthesis.verdict(payload)
    assert (launch, blocking) == ("go_with_fixes", [])
    assert fixable[0].evidence_ids == (EV[2],)


def test_sizing_that_could_not_run_is_reported_as_a_fix() -> None:
    payload = outputs()
    payload["1.5.4"] = {"scenarios": [], "blockers": ["the account has no measured CPC"]}
    launch, _, fixable = synthesis.verdict(payload)
    assert launch == "go_with_fixes"
    assert any("no measured CPC" in fact.statement for fact in fixable)


# ---------------------------------------------------------------------------
# assembly
# ---------------------------------------------------------------------------


def test_every_section_of_the_prd_contract_is_populated() -> None:
    built = report()
    assert built.business_context.products[0].name == "SDS Manager"
    assert built.account_learnings.wasteful_terms[0].term == "free sds"
    assert built.competitive_landscape.recommended_claim
    assert built.demand_map.total_keywords == 2140
    assert built.readiness.conversion_actions[0].name == "Demo request"
    assert built.readiness.scenarios[0].est_conv == 10.5
    assert built.priced_keyword_list[0].term == "sds software"


def test_the_priced_keyword_row_joins_four_nodes() -> None:
    """The CSV someone pastes into Google Ads Editor. Each column comes from a
    different node, and a lost join is a column of blanks nobody notices."""
    row = report().priced_keyword_list[0]
    assert row.volume == 2400  # 1.4.3
    assert row.intent == "transactional"  # 1.4.2
    assert row.best_url == "https://northwind.test/software"  # 1.4.5
    assert row.market == "US"  # 1.4.1
    assert row.verdict == "good_fit"
    assert len(row.seasonality_index) == 12


def test_a_spend_estimate_keeps_its_method_and_gains_a_range() -> None:
    """PRD §10 1.3.3: an estimate must state its method and is never presented
    as fact. The contract wants one range string; the node emits two numbers."""
    estimate = report().competitive_landscape.spend_estimates[0]
    assert estimate.method == "impression share x CPC"
    assert estimate.est_monthly_spend_range == "$8,000–$14,000"


def test_conversions_survive_the_rename_between_node_and_contract() -> None:
    """1.2.2 writes `conversions`; §11's field is `conv`. A silent mismatch
    leaves the P&L column empty in every export."""
    assert report().account_learnings.profitable_terms[0].conv == 6.0


def test_the_report_cites_what_the_nodes_cited() -> None:
    built = report()
    cited = set(built.evidence_ids())
    assert {EV[0], EV[1], EV[2], EV[3], EV[4]} <= cited


def test_cited_evidence_ids_is_what_node_1_6_1_must_gather() -> None:
    """The executor fails a node citing evidence it did not gather, so these two
    have to be the same set or the last node in the run cannot pass."""
    payload = outputs()
    wanted = set(synthesis.cited_evidence_ids(payload))
    built = report()
    assert set(built.evidence_ids()) <= wanted


def test_degraded_sources_are_collected_from_every_node() -> None:
    payload = outputs()
    payload["1.3.2"] = {**payload["1.3.2"], "coverage": ["competitor_creative: partial — blocked"]}
    payload["1.4.3"] = {**payload["1.4.3"], "coverage": ["keyword_metrics: unavailable"]}
    built = synthesis.assemble(
        project=FakeProject(),  # type: ignore[arg-type]
        run=FakeRun(),  # type: ignore[arg-type]
        outputs=payload,
        summary="s",
        launch_readiness="go",
        launch_blockers=[],
        next_actions=[],
        open_questions=[],
        cost_usd=0.0,
    )
    assert built.degraded_sources == [
        "competitor_creative: partial — blocked",
        "keyword_metrics: unavailable",
    ]


def test_a_run_where_almost_everything_failed_still_produces_a_report() -> None:
    """A report that only assembles when the run went well is a report that
    vanishes exactly when someone needs to know what went wrong."""
    built = synthesis.assemble(
        project=FakeProject(),  # type: ignore[arg-type]
        run=FakeRun(),  # type: ignore[arg-type]
        outputs={"1.5.1": {"pages": [], "unreachable": []}},
        summary="Nothing usable was gathered.",
        launch_readiness="no_go",
        launch_blockers=[],
        next_actions=[],
        open_questions=["Authorise the Google Ads connector."],
        cost_usd=0.0,
    )
    assert built.priced_keyword_list == []
    assert built.launch_readiness == "no_go"


def test_the_assembled_report_renders_every_section() -> None:
    """Assembly and rendering are tested together once: a report that validates
    but renders empty sections is not a deliverable."""
    markdown = render_markdown(report(), project_name="Northwind Safety")
    for title in SECTION_TITLES:
        assert title in markdown, title
    assert "sds software" in markdown


def test_assembly_is_deterministic() -> None:
    first = report().model_dump(mode="json")
    second = report().model_dump(mode="json")
    assert first == second


def test_the_keyword_list_is_capped_rather_than_unbounded() -> None:
    payload = outputs()
    payload["1.4.3"] = {
        "metrics": [
            {"term": f"term {i}", "volume": i} for i in range(synthesis.MAX_PRICED_KEYWORDS + 50)
        ],
        "totals": {"terms_priced": synthesis.MAX_PRICED_KEYWORDS + 50},
    }
    assert len(synthesis.priced_keywords(payload)) == synthesis.MAX_PRICED_KEYWORDS


def test_citable_ranks_the_most_cited_evidence_first() -> None:
    payload = outputs()
    payload["1.1.2"]["segments"][0]["evidence_ids"] = [str(EV[0])]
    payload["1.2.2"]["profitable_terms"][0]["evidence_ids"] = [str(EV[0])]
    ranked = synthesis.citable(payload, {value: f"row {value}" for value in EV})
    assert ranked[0]["id"] == str(EV[0])


def test_citable_never_offers_an_id_with_no_evidence_row() -> None:
    """The model copies these verbatim. Offering an id the run cannot resolve
    guarantees a dropped claim."""
    ranked = synthesis.citable(outputs(), {EV[0]: "row"})
    assert [item["id"] for item in ranked] == [str(EV[0])]


@pytest.mark.parametrize("missing", ["1.5.1", "1.5.2", "1.5.3", "1.5.4"])
def test_a_missing_readiness_node_does_not_break_assembly(missing: str) -> None:
    payload = {key: value for key, value in outputs().items() if key != missing}
    launch, _, _ = synthesis.verdict(payload)
    assert launch in {"go", "go_with_fixes", "no_go"}
    assert synthesis.readiness(payload) is not None


# ---------------------------------------------------------------------------
# where the nodes and the contract disagree
#
# Both of these were found by running the assembly against real node output for
# the first time, and both were silent: the guard dropped the rows, the report
# rendered, and a whole section was simply missing.
# ---------------------------------------------------------------------------


def test_a_numeric_metric_delta_becomes_the_label_the_report_prints() -> None:
    """1.2.1 emits `metric_delta` as a number. §11's field is a string. Left
    alone, every winner and loser disappears from the report."""
    payload = outputs()
    payload["1.2.1"]["winners"] = [{"campaign": "Brand", "metric_delta": -20.0, "period": "H2"}]
    built = report(**{"1.2.1": payload["1.2.1"]})
    assert built.account_learnings.winners[0].metric_delta == "-20.0% CPA"


def test_a_metric_delta_already_written_as_text_is_left_alone() -> None:
    payload = outputs()
    payload["1.2.1"]["winners"] = [{"campaign": "Brand", "metric_delta": "-18% CPA"}]
    built = report(**{"1.2.1": payload["1.2.1"]})
    assert built.account_learnings.winners[0].metric_delta == "-18% CPA"


def test_an_unbounded_overlap_score_is_rescaled_not_dropped() -> None:
    """1.3.1's score "ranks these domains against each other, it is not a
    percentage of anything", and the report renders it as a percentage. Without
    the rescale the contract's `le=1` drops the entire competitor table."""
    payload = outputs()
    payload["1.3.1"]["competitors"] = [
        {"domain": "a.test", "overlap_score": 100.0},
        {"domain": "b.test", "overlap_score": 70.0},
        {"domain": "c.test", "overlap_score": 55.0},
    ]
    built = report(**{"1.3.1": payload["1.3.1"]})
    scores = [competitor.overlap_score for competitor in built.competitive_landscape.competitors]
    assert scores == [1.0, 0.7, 0.55]
    assert len(built.competitive_landscape.competitors) == 3


def test_rescaling_preserves_the_ranking_and_keeps_the_raw_figure() -> None:
    payload = outputs()
    payload["1.3.1"]["competitors"] = [
        {"domain": "a.test", "overlap_score": 40.0},
        {"domain": "b.test", "overlap_score": 80.0},
    ]
    built = report(**{"1.3.1": payload["1.3.1"]})
    ordered = built.competitive_landscape.competitors
    assert ordered[0].overlap_score < ordered[1].overlap_score
    assert ordered[1].model_extra["overlap_score_raw"] == 80.0


def test_scores_already_on_the_right_scale_are_untouched() -> None:
    payload = outputs()
    payload["1.3.1"]["competitors"] = [{"domain": "a.test", "overlap_score": 0.81}]
    built = report(**{"1.3.1": payload["1.3.1"]})
    assert built.competitive_landscape.competitors[0].overlap_score == 0.81
    assert "overlap_score_raw" not in (built.competitive_landscape.competitors[0].model_extra or {})


def test_a_row_that_cannot_be_reconciled_is_dropped_and_reported() -> None:
    """The guard's whole purpose. One bad row costs one row of one section —
    never a forty-minute run at its last node — and the drop is *named*."""
    dropped: list[str] = []
    payload = outputs()
    payload["1.5.4"]["scenarios"] = [
        {"budget_usd_month": 1000.0},
        {"budget_usd_month": -5.0},  # ge=0
    ]
    built = synthesis.readiness(payload, dropped.append)
    assert len(built.scenarios) == 1
    assert dropped and dropped[0].startswith("Scenario:")


def test_a_keyword_with_the_wrong_seasonality_length_loses_only_itself() -> None:
    dropped: list[str] = []
    payload = outputs()
    payload["1.4.3"]["metrics"] = [
        {"term": "good", "volume": 10, "seasonality_index": [1.0] * 12},
        {"term": "bad", "volume": 10, "seasonality_index": [1.0] * 11},
    ]
    rows = synthesis.priced_keywords(payload, dropped.append)
    assert [row.term for row in rows] == ["good"]
    assert dropped
