"""Shared machinery for the run tests: seeded evidence, scripted answers, a worker call.

`execute` is deliberately the same shape as `agent.worker.execute_run` — its own
session, the shared Redis, one HTTP client for the whole run — so what these
tests exercise is the path the worker actually takes.

The chain these tests drive is **1.1.2 → 1.1.4**, the smallest real
dependency in the PRD §10 DAG. P1 used two throwaway nodes for this; P3 deleted
them, and running the executor against nodes that ship is strictly better
coverage — the prompts, the pandas rollups and the merge are all on the path.

It does mean evidence has to exist: `seed_crm` writes the closed-won rows both
nodes read. A node with no evidence returns an empty result *without* calling a
model, which is correct behaviour (PRD §16, never hallucinate history) and also
means an unseeded run would consume none of the scripted completions.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import httpx

from agent.orchestrator.executor import ExecutionResult, RunExecutor
from tests.openrouter_fake import FakeOpenRouter, completion

#: The tail of the two-node chain. The API widens it to `["1.1.2", "1.1.4"]`.
CHAIN = ["1.1.4"]

#: The gate branch: 1.1.1 → 1.1.5, the only gate in stages 1.1 and 1.2.
GATE_CHAIN = ["1.1.5"]

#: Closed-won rows that fall into exactly two firmographic segments, so the
#: rollups produce a stable, hand-checkable table.
WON_DEALS: tuple[dict[str, Any], ...] = (
    {
        "account_name": "Acme Chemicals",
        "industry": "Chemicals",
        "country": "US",
        "employee_count": 120,
        "deal_value": 24000,
        "created_at": "2025-03-04",
        "outcome": "won",
    },
    {
        "account_name": "Borax Supply",
        "industry": "Chemicals",
        "country": "US",
        "employee_count": 180,
        "deal_value": 16000,
        "created_at": "2025-03-19",
        "outcome": "won",
    },
    {
        "account_name": "Rheinwerk GmbH",
        "industry": "Manufacturing",
        "country": "DE",
        "employee_count": 640,
        "deal_value": 40000,
        "created_at": "2025-09-02",
        "outcome": "won",
    },
    {
        "account_name": "Nordmetall AG",
        "industry": "Manufacturing",
        "country": "DE",
        "employee_count": 900,
        "deal_value": 20000,
        "created_at": "2025-10-11",
        "outcome": "won",
    },
)

#: The segment keys `frames.crm_segments` derives from `WON_DEALS`, in the order
#: it emits them (revenue descending).
SEGMENT_KEYS = ("Manufacturing|DE|250-999", "Chemicals|US|50-249")

#: Node 1.1.2's scripted answer — labels only; every number is computed.
ICP_LABELS: dict[str, Any] = {
    "labels": [
        {
            "key": SEGMENT_KEYS[0],
            "label": "German plant operators",
            "firmographics": "Mid-size manufacturers running regulated production lines.",
            "triggers": ["a REACH audit", "a new plant"],
            "jobs_to_be_done": ["prove compliance to an auditor"],
        },
        {
            "key": SEGMENT_KEYS[1],
            "label": "US chemical distributors",
            "firmographics": "Distributors shipping hazardous goods across state lines.",
            "triggers": ["a failed inspection"],
            "jobs_to_be_done": ["keep safety data sheets current"],
        },
    ]
}

#: Node 1.1.4's scripted answer.
MARKET_LABELS: dict[str, Any] = {
    "markets": [
        {
            "country": "DE",
            "language": "de",
            "currency": "EUR",
            "note": "Autumn-weighted, matching the plant-operator segment.",
        },
        {
            "country": "US",
            "language": "en",
            "currency": "USD",
            "note": "Concentrated in March.",
        },
    ]
}

#: Node 1.1.1's scripted answer — assumptions only, never a measured figure.
OFFER_DRAFT: dict[str, Any] = {
    "products": [
        {
            "name": "SDS Manager",
            "price_model": "subscription",
            "delivery_cost_notes": "Hosting and a shared support desk.",
            "evidence_ids": [],
        }
    ],
    "assumptions": {
        "gross_margin_pct": 80,
        "expected_lifetime_months": 36,
        "target_ltv_cac_ratio": 3,
        "basis": "Closed-won deals renew across multiple years in the export.",
    },
}

#: Node 1.1.5's scripted answer — the proposal a human is asked to approve.
GUARDRAILS: dict[str, Any] = {
    "prohibited_claims": ["100% compliance guaranteed"],
    "required_disclaimers": ["Not legal advice."],
    "regulated_terms": [
        {
            "term": "GHS certified",
            "rule": "Only for classifications we actually hold.",
            "evidence_ids": [],
        }
    ],
    "prior_disapprovals": [],
    "confidence": 0.4,
    "reviewer_notes": "Check the GHS wording against our certificates.",
    "coverage": [],
}


async def seed_crm(
    project_id: uuid.UUID,
    *,
    won: tuple[dict[str, Any], ...] = WON_DEALS,
    lost: tuple[dict[str, Any], ...] = (),
) -> list[uuid.UUID]:
    """Write CRM evidence the way `csv_ingest` does, minus the embedding.

    `embed=False` keeps the ONNX model out of the test process: these tests are
    about the executor, and loading a 130MB embedder to prove a run resumes
    would add half a minute to every one of them.

    Idempotent — evidence is deduped on `(project_id, hash)` — so calling it
    once per launch costs nothing on the second call.
    """
    from agent.db.models import Project
    from agent.db.session import get_sessionmaker
    from agent.evidence.normalize import EvidenceDraft
    from agent.evidence.store import EvidenceStore

    drafts = [EvidenceDraft(source="csv", kind="crm_won", payload=dict(row)) for row in won] + [
        EvidenceDraft(source="csv", kind="crm_lost", payload=dict(row)) for row in lost
    ]

    async with get_sessionmaker()() as session:
        project = await session.get(Project, project_id)
        assert project is not None, "seed_crm needs a project that exists"
        store = EvidenceStore(session, project.workspace_id)
        result = await store.write(drafts, project_id=project_id, embed=False)
        await session.commit()
    return result.evidence_ids


def unparseable() -> Any:
    """A response the gateway cannot turn into any output model.

    Not "valid JSON of the wrong shape": every stage-1.1 output model has
    defaults on all but one field, so a stray object would validate and the
    test would prove nothing. Text that is not JSON at all fails the same way
    for every node, which is what a failure fixture needs to be.
    """
    return completion("the model replied with prose instead of JSON")


#: Google Ads evidence for the stage-1.2 nodes: two campaigns over four months,
#: and four search-term rows of which two converted nobody.
CAMPAIGN_ROWS: tuple[dict[str, Any], ...] = (
    {
        "campaign": "Brand",
        "month": "2025-01",
        "cost": 100.0,
        "conversions": 4.0,
        "conversion_value": 2000.0,
        "impressions": 900,
        "clicks": 60,
    },
    {
        "campaign": "Brand",
        "month": "2025-06",
        "cost": 120.0,
        "conversions": 6.0,
        "conversion_value": 3000.0,
        "impressions": 1100,
        "clicks": 80,
    },
    {
        "campaign": "Generic",
        "month": "2025-01",
        "cost": 400.0,
        "conversions": 1.0,
        "conversion_value": 500.0,
        "impressions": 9000,
        "clicks": 300,
    },
    {
        "campaign": "Generic",
        "month": "2025-06",
        "cost": 500.0,
        "conversions": 0.0,
        "conversion_value": 0.0,
        "impressions": 12000,
        "clicks": 380,
    },
)

SEARCH_TERM_ROWS: tuple[dict[str, Any], ...] = (
    {
        "search_term": "sds management software",
        "campaign": "Brand",
        "month": "2025-01",
        "cost": 90.0,
        "conversions": 3.0,
        "conversion_value": 1800.0,
        "clicks": 40,
        "impressions": 500,
    },
    {
        "search_term": "free sds template",
        "campaign": "Generic",
        "month": "2025-01",
        "cost": 260.0,
        "conversions": 0.0,
        "conversion_value": 0.0,
        "clicks": 200,
        "impressions": 7000,
    },
    {
        "search_term": "sds jobs",
        "campaign": "Generic",
        "month": "2025-06",
        "cost": 50.0,
        "conversions": 0.0,
        "conversion_value": 0.0,
        "clicks": 30,
        "impressions": 900,
    },
)

CHANGE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "changed_at": "2025-04-02",
        "resource_type": "CAMPAIGN",
        "operation": "REMOVE",
        "changed_fields": "status",
        "campaign": "Broad Match Test",
    },
)


async def seed_google_ads(project_id: uuid.UUID) -> None:
    """Write the Google Ads evidence a cassette-backed pull would have produced.

    P3 has no live Google Ads credential, and `gather` degrades to an empty
    result without one. Writing the rows directly is what lets stage 1.2 be
    exercised end to end without pretending a connector ran.
    """
    from agent.db.models import Project
    from agent.db.session import get_sessionmaker
    from agent.evidence.normalize import EvidenceDraft
    from agent.evidence.store import EvidenceStore

    drafts = (
        [
            EvidenceDraft(source="google_ads", kind="campaign_perf", payload=dict(row))
            for row in CAMPAIGN_ROWS
        ]
        + [
            EvidenceDraft(source="google_ads", kind="search_term_pnl", payload=dict(row))
            for row in SEARCH_TERM_ROWS
        ]
        + [
            EvidenceDraft(source="google_ads", kind="change_log", payload=dict(row))
            for row in CHANGE_ROWS
        ]
    )
    async with get_sessionmaker()() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        store = EvidenceStore(session, project.workspace_id)
        await store.write(drafts, project_id=project_id, embed=False)
        await session.commit()


#: One scripted answer per output model. Keyed by the schema name the gateway
#: puts in `response_format.json_schema.name`, which is the Pydantic model's
#: title — so a wave that runs six nodes concurrently still gets the right
#: answer to each question.
BY_OUTPUT_MODEL: dict[str, dict[str, Any]] = {
    "OfferEconomicsDraft": OFFER_DRAFT,
    "SegmentLabels": ICP_LABELS,
    "MarketLabels": MARKET_LABELS,
    "ComplianceGuardrails": GUARDRAILS,
    "NegativeIcp": {
        "exclusions": [
            {
                "persona": "Student researcher",
                "disqualifier": "No budget and no purchasing authority.",
                "observable_signal": "Queries containing 'free' or 'template'.",
                "suggested_negative_terms": ["free", "template", "pdf download"],
                "evidence_ids": [],
            }
        ],
        "lost_reason_summary": [],
        "coverage": [],
    },
    "HistoricalReading": {
        "verdicts": [
            {"campaign": "Brand", "verdict": "winner", "why": "CPA improved across the window."},
            {"campaign": "Generic", "verdict": "loser", "why": "Spend rose as conversions fell."},
        ],
        "structural_findings": ["Four fifths of spend sits in one broad campaign."],
    },
    "TermLabelling": {
        "actions": [
            {
                "term": "free sds template",
                "recommended_action": "negative_phrase",
                "reason": "Downloaders, not buyers.",
            },
            {
                "term": "sds jobs",
                "recommended_action": "negative_exact",
                "reason": "Job seekers.",
            },
        ],
        "clusters": [
            {
                "theme": "free templates",
                "verdict": "wasteful",
                "terms": ["free sds template"],
                "note": "",
            }
        ],
    },
    "FailedExperiments": {
        "tried_and_failed": [
            {
                "what": "A broad match test campaign",
                "when": "April 2025",
                "outcome": "Removed within the month.",
                "do_not_repeat_reason": "It was switched off rather than tuned.",
                "evidence_ids": [],
            }
        ],
        "coverage": [],
    },
}


def by_output_model(fake: FakeOpenRouter, answers: dict[str, Any] | None = None) -> None:
    """Answer every completion according to the output model it asked for."""
    table = answers if answers is not None else BY_OUTPUT_MODEL

    def respond(request: Any) -> Any:
        body = json.loads(request.content or b"{}")
        name = (
            body.get("response_format", {}).get("json_schema", {}).get("name")
            or (body.get("tools") or [{}])[0].get("function", {}).get("name")
            or ""
        )
        if name not in table:
            raise AssertionError(f"no scripted answer for output model {name!r}")
        return completion(table[name])

    fake.dispatch(respond)


def script_two_node_run(fake: FakeOpenRouter) -> None:
    """One good answer per node of the 1.1.2 → 1.1.4 chain, in wave order."""
    fake.queue(completion(ICP_LABELS), completion(MARKET_LABELS))


def script_gate_run(fake: FakeOpenRouter) -> None:
    """One good answer per node of the 1.1.1 → 1.1.5 gate branch."""
    fake.queue(completion(OFFER_DRAFT), completion(GUARDRAILS))


async def execute(
    run_id: uuid.UUID,
    fake: FakeOpenRouter,
    *,
    client: httpx.AsyncClient | None = None,
    **kwargs: Any,
) -> ExecutionResult:
    """Run the DAG the way the worker does, against the scripted provider."""
    from agent.db.session import get_sessionmaker
    from agent.redis_client import get_redis

    owned = client or fake.client()
    try:
        async with get_sessionmaker()() as session:
            executor = RunExecutor(
                db=session,
                redis=get_redis(),
                http_client=owned,
                backoff_base=0.0,
                **kwargs,
            )
            return await executor.execute(run_id)
    finally:
        if client is None:
            await owned.aclose()


async def launch(admin: Any, project_id: uuid.UUID, **body: Any) -> dict[str, Any]:
    response = await admin.post(f"/projects/{project_id}/runs", json=body or {})
    assert response.status_code == 201, response.text
    return dict(response.json())


async def launch_chain(admin: Any, project_id: uuid.UUID, **body: Any) -> dict[str, Any]:
    """Seed the CRM rows the chain reads, then launch 1.1.2 → 1.1.4.

    The seeding is part of the helper on purpose: both nodes return an empty
    result *without calling a model* when there is no evidence, so a run
    launched without it would consume none of the scripted completions and the
    test would pass for the wrong reason.
    """
    await seed_crm(project_id)
    return await launch(admin, project_id, mode="partial", node_ids=CHAIN, **body)


async def launch_gate(admin: Any, project_id: uuid.UUID, **body: Any) -> dict[str, Any]:
    """Seed the CRM rows, then launch the 1.1.1 → 1.1.5 gate branch."""
    await seed_crm(project_id)
    return await launch(admin, project_id, mode="partial", node_ids=GATE_CHAIN, **body)


def utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# P4: the competitive and demand evidence stages 1.3 and 1.4 read
#
# Seeded directly rather than pulled, for the same reason `seed_google_ads`
# exists: there is no DataForSEO login and no Chromium in this suite, and a
# connector that cannot reach its source degrades to empty — which would make
# every assertion below pass for the wrong reason.
# ---------------------------------------------------------------------------

#: The domains the keyword vendor reports as competing for our paid clicks, and
#: the names the Transparency Center scrape ran under. Two spellings of three
#: companies, which is the join node 1.3.3 has to get right.
COMPETITORS: tuple[tuple[str, str, int, float], ...] = (
    ("chemwatch.net", "Chemwatch", 240, 18_000.0),
    ("sdsbinder.com", "SDS Binder", 120, 4_500.0),
    ("msdsonline.com", "MSDSonline", 60, 0.0),
)

#: Keyword families, sized so the universe clears PRD §10 1.4.1's 2,000 target
#: on evidence alone rather than on anything a model contributed.
KEYWORD_FAMILIES: tuple[tuple[str, int], ...] = (
    ("sds software", 800),
    ("ghs labeling", 800),
    ("chemical inventory", 600),
    ("free sds template", 200),
    # Nothing on the site mentions respirators, so this family has to come back
    # as a content gap rather than be mapped to the nearest page that exists.
    ("respirator fit testing", 200),
)

#: Our own pages. Two answer a keyword family well, one answers none of them —
#: so the page map has a good fit, a weak fit and a gap to find.
#: The crawler fields node 1.5.1 audits, on a page with nothing wrong with it.
HEALTHY_PAGE: dict[str, Any] = {
    "status": 200,
    "https": True,
    "mobile_viewport": True,
    "primary_cta": "Book a demo",
    "form_fields": ["name", "work_email", "company"],
    "trust_markers": ["iso_certification", "gdpr"],
    "word_count": 940,
}

OUR_PAGES: tuple[dict[str, Any], ...] = (
    {
        **HEALTHY_PAGE,
        "url": "https://sdsmanager.com/sds-software",
        "title": "SDS software for chemical manufacturers",
        "h1": "SDS software that keeps your library current",
        "h2": ["Why SDS software", "SDS software pricing"],
        "meta_description": "Manage safety data sheets in one place.",
        "text_excerpt": "software for managing safety data sheets",
    },
    {
        **HEALTHY_PAGE,
        "url": "https://sdsmanager.com/ghs-labeling",
        "title": "GHS labeling software",
        "h1": "GHS labeling made simple",
        "h2": ["GHS labeling rules"],
        "meta_description": "Print compliant GHS labels.",
        # Eleven fields on a landing page. 1.5.1 reports it as a major issue and
        # the report downgrades to `go_with_fixes` — which is the state this
        # fixture is built to produce.
        "form_fields": [f"field_{index}" for index in range(11)],
        "text_excerpt": "labeling for hazardous chemicals",
    },
    {
        **HEALTHY_PAGE,
        "url": "https://sdsmanager.com/about",
        "title": "About us",
        "h1": "Our story",
        "h2": [],
        "meta_description": "Who we are.",
        "text_excerpt": "a company founded in Norway",
    },
)


def keyword_terms() -> list[str]:
    """Every seeded term, in the order the families declare them."""
    return [f"{family} {index}" for family, count in KEYWORD_FAMILIES for index in range(count)]


def _monthly(volume: int) -> list[dict[str, Any]]:
    """Twelve months with an autumn peak, so `seasonality_index` has something to find."""
    return [
        {"year": 2025, "month": month, "search_volume": volume * (2 if month in (9, 10) else 1)}
        for month in range(1, 13)
    ]


async def seed_competitive(project_id: uuid.UUID, *, ads_per_advertiser: int = 40) -> None:
    """Competitor domains, SERPs, creatives and landing pages for stage 1.3."""
    from agent.db.models import Project
    from agent.db.session import get_sessionmaker
    from agent.evidence.normalize import EvidenceDraft
    from agent.evidence.store import EvidenceStore

    drafts = [
        EvidenceDraft(
            source="dataforseo",
            kind="domain_competitor",
            payload={
                "competitor_domain": domain,
                "for_domain": "sdsmanager.com",
                "avg_position": 2.4,
                "intersections": intersections,
                "paid_keyword_count": intersections * 3,
                "paid_estimated_traffic_cost": cost,
            },
        )
        for domain, _, intersections, cost in COMPETITORS
    ]
    drafts += [
        EvidenceDraft(
            source="dataforseo",
            kind="serp_snapshot",
            payload={
                "keyword": term,
                "results": [
                    {"rank": rank, "domain": domain, "url": f"https://{domain}/{term}"}
                    for rank, (domain, _, _, _) in enumerate(COMPETITORS, start=1)
                ],
            },
        )
        for term in ("sds management software", "free sds template")
    ]
    for _, advertiser, _, _ in COMPETITORS:
        for index in range(ads_per_advertiser):
            drafts.append(
                EvidenceDraft(
                    source="transparency",
                    kind="competitor_creative",
                    payload={
                        "advertiser": advertiser,
                        "ad_id": f"CR-{advertiser.replace(' ', '')}-{index}",
                        "format": "text",
                        "first_shown": "2025-01-05",
                        "last_shown": "2025-10-18",
                        "creative_text": (
                            f"{advertiser}: compliance without the binders. "
                            f"Start a free trial today. Offer {index}."
                        ),
                        "destination_url": f"https://{advertiser.replace(' ', '').lower()}.com/sds",
                        "regions": ["US"],
                        "screenshot_path": f"creatives/seed/{advertiser.replace(' ', '')}.png",
                    },
                )
            )
    drafts += [
        EvidenceDraft(
            source="transparency",
            kind="competitor_landing_page",
            payload={
                "url": f"https://{domain}/sds",
                "title": f"{name} — SDS management",
                "h1": "Compliance without the binders",
                "competitor_domain": domain,
                "text_excerpt": "trusted by 10,000 teams",
            },
        )
        for domain, name, _, _ in COMPETITORS
    ]

    async with get_sessionmaker()() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        store = EvidenceStore(session, project.workspace_id)
        await store.write(drafts, project_id=project_id, embed=False)
        await session.commit()


async def seed_demand(project_id: uuid.UUID) -> None:
    """Priced keywords and our own pages, for stage 1.4."""
    from agent.db.models import Project
    from agent.db.session import get_sessionmaker
    from agent.evidence.normalize import EvidenceDraft
    from agent.evidence.store import EvidenceStore

    drafts: list[EvidenceDraft] = []
    for position, term in enumerate(keyword_terms()):
        volume = 10 + (position % 90)
        drafts.append(
            EvidenceDraft(
                source="dataforseo",
                kind="keyword_metrics",
                payload={
                    "keyword": term,
                    "search_volume": volume,
                    "cpc": 4.5,
                    "low_top_of_page_bid": 3.2,
                    "high_top_of_page_bid": 9.8,
                    "competition": "HIGH" if position % 2 else 0.2,
                    "competition_index": 70,
                    # Monthly history on a slice only: `with_seasonality` has to
                    # be able to differ from `terms_priced`, or the totals prove
                    # nothing about the join.
                    "monthly_searches": _monthly(volume) if position % 4 == 0 else [],
                    "origin": "site",
                    "domain": "sdsmanager.com",
                },
            )
        )
    drafts += [EvidenceDraft(source="web", kind="page", payload=dict(page)) for page in OUR_PAGES]

    async with get_sessionmaker()() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        store = EvidenceStore(session, project.workspace_id)
        await store.write(drafts, project_id=project_id, embed=False)
        await session.commit()


# --- the scripted provider for stages 1.3 and 1.4 --------------------------


def _computed(user: str, fragment: str) -> Any:
    """The JSON of the COMPUTED block whose title contains `fragment`.

    The point of answering this way rather than from a static table: a batched
    node that dropped a term would be handed back exactly the terms it sent, so
    a hole in the fan-out shows up as a missing row instead of being papered
    over by a fixture that always returns the full list.
    """
    marker = "COMPUTED — "
    cursor = 0
    while True:
        found = user.find(marker, cursor)
        if found == -1:
            raise AssertionError(f"no COMPUTED block whose title contains {fragment!r}")
        line_end = user.index("\n", found)
        if fragment in user[found + len(marker) : line_end]:
            value, _ = json.JSONDecoder().raw_decode(user[line_end + 1 :])
            return value
        cursor = line_end


def _intent_of(term: str) -> tuple[str, str]:
    """A deterministic rubric, so the classification is checkable by hand."""
    if "free" in term or "template" in term:
        return "irrelevant", "none"
    if "software" in term:
        return "transactional", "decision"
    if "labeling" in term:
        return "commercial_investigation", "consideration"
    return "informational", "awareness"


P4_STATIC: dict[str, Any] = {
    "SeedTopics": {
        "topics": [
            "safety data sheet management",
            "chemical compliance software",
            "sds authoring tool",
        ]
    },
    "DifferentiationClaim": {
        "whitespace": [
            {
                "claim": "Every sheet is re-checked against the supplier each quarter.",
                "why_unsaid": "Competitors sell storage, not currency.",
                "our_proof": "Our own pages describe the re-check cycle.",
                "risk": "We must be able to evidence the cadence.",
                "evidence_ids": [],
            }
        ],
        "recommended_claim": "Your library is never out of date.",
        "recommended_rationale": "Nobody in the corpus claims currency.",
        "substantiation_required": ["A measured re-check interval"],
        "rejected_claims": ["100% compliance guaranteed — prohibited by 1.1.5"],
        "confidence": 0.55,
        "reviewer_notes": "Check the cadence claim against operations.",
        "coverage": [],
    },
}


def stage_1_3_and_1_4(fake: FakeOpenRouter) -> None:
    """Answer every completion from the batch it was actually sent."""

    def respond(request: Any) -> Any:
        body = json.loads(request.content or b"{}")
        name = (
            body.get("response_format", {}).get("json_schema", {}).get("name")
            or (body.get("tools") or [{}])[0].get("function", {}).get("name")
            or ""
        )
        user = next(
            (item["content"] for item in reversed(body.get("messages", [])) if item["content"]),
            "",
        )

        if name == "CompetitorLabels":
            rows = _computed(user, "measured overlap")
            return completion(
                {
                    "competitors": [
                        {
                            "domain": row["domain"],
                            "name": next(
                                (
                                    label
                                    for domain, label, _, _ in COMPETITORS
                                    if domain == row["domain"]
                                ),
                                row["domain"],
                            ),
                            "positioning": "Sells SDS storage.",
                            "threat": "direct",
                        }
                        for row in rows
                    ]
                }
            )

        if name == "ExtractedAds":
            rows = _computed(user, "ads to read")
            return completion(
                {
                    "ads": [
                        {
                            "key": row["key"],
                            "headline": "Compliance without the binders",
                            "description": "Keep every sheet current.",
                            "offer": "Free trial",
                            "angle": "Reduce audit risk",
                            "proof_type": "free_trial",
                            "cta": "Start free",
                            "theme": "free trial",
                        }
                        for row in rows
                    ]
                }
            )

        if name == "EstimateNotes":
            rows = _computed(user, "estimates (final")
            return completion(
                {
                    "notes": [
                        {
                            "competitor": row["competitor"],
                            "caveat": "The vendor's traffic model may be stale.",
                            "how_to_verify": "Run an auction-insights export by hand.",
                        }
                        for row in rows
                    ]
                }
            )

        if name == "TermIntents":
            terms = _computed(user, "terms to label")
            items = []
            for term in terms:
                intent, stage = _intent_of(term)
                items.append(
                    {
                        "term": term,
                        "intent": intent,
                        "funnel_stage": stage,
                        "confidence": 0.8,
                        "note": "Seeker, not a buyer." if intent == "irrelevant" else "",
                    }
                )
            return completion({"items": items})

        if name == "ContentGaps":
            rows = _computed(user, "themes with no good page")
            return completion(
                {
                    "gaps": [
                        {
                            "cluster": row["cluster"],
                            "required_page_type": "comparison page",
                            "why": "Nothing on the site answers this theme.",
                            "priority": "high",
                        }
                        for row in rows
                    ]
                }
            )

        if name in P4_STATIC:
            return completion(P4_STATIC[name])
        if name in BY_OUTPUT_MODEL:
            return completion(BY_OUTPUT_MODEL[name])
        raise AssertionError(f"no scripted answer for output model {name!r}")

    fake.dispatch(respond)


# ---------------------------------------------------------------------------
# P5b: the readiness evidence stage 1.5 reads
#
# Seeded directly, for the same reason `seed_google_ads` is: there is no live
# Google Ads credential and no browser in the test image, and `gather` degrades
# to an empty result without either. Writing the rows is what lets stage 1.5 be
# exercised end to end without pretending a connector ran.
# ---------------------------------------------------------------------------

#: The `send_to` on both sides of the join node 1.5.2 makes: the tag the browser
#: saw fire, and the conversion action the API reports. They match on purpose —
#: `test_a_broken_tag_blocks_the_launch` breaks it deliberately.
SEND_TO = "AW-987654321/AbC-D_efGhIjKlM"

#: The two pages the keyword map points at. One is fast, one is slow enough to
#: be a major issue but not a critical one.
PAGE_VITALS_ROWS: tuple[dict[str, Any], ...] = (
    {"url": "https://sdsmanager.com/sds-software", "lcp_ms": 1840.0, "cls": 0.02, "tbt_ms": 90.0},
    {"url": "https://sdsmanager.com/ghs-labeling", "lcp_ms": 3300.0, "cls": 0.12, "tbt_ms": 260.0},
)


def conversion_action_rows(
    *, converting_days_ago: int = 3, conversions: float = 11.0
) -> list[dict[str, Any]]:
    """Dated rows for two conversion actions: one live and primary, one removed.

    Dated, because staleness is computed from the last day that *recorded* a
    conversion rather than the last day the API answered for.
    """
    from datetime import timedelta

    today = datetime.now(UTC).date()
    rows = [
        {
            "conversion_action_id": "555000111",
            "name": "Demo request",
            "status": "ENABLED",
            "action_type": "WEBPAGE",
            "category": "SUBMIT_LEAD_FORM",
            "counting_type": "ONE_PER_CLICK",
            "primary_for_goal": True,
            "send_to": SEND_TO,
            "date": (today - timedelta(days=converting_days_ago)).isoformat(),
            "conversions": conversions,
        }
    ]
    rows += [
        {
            "conversion_action_id": "555000111",
            "name": "Demo request",
            "status": "ENABLED",
            "primary_for_goal": True,
            "send_to": None,
            "date": (today - timedelta(days=offset)).isoformat(),
            "conversions": 0.0,
        }
        for offset in range(converting_days_ago)
    ]
    rows.append(
        {
            "conversion_action_id": "555000222",
            "name": "Newsletter signup (legacy)",
            "status": "REMOVED",
            "primary_for_goal": False,
            "send_to": None,
            "date": today.isoformat(),
            "conversions": 0.0,
        }
    )
    return rows


AUDIENCE_ROWS: tuple[dict[str, Any], ...] = (
    {
        "user_list_id": "777000111",
        "name": "All converters — 540 days",
        "description": "Anyone who submitted the demo form",
        "list_type": "REMARKETING",
        "membership_status": "OPEN",
        "membership_life_span_days": "540",
        "size_for_display": "48200",
        "size_for_search": "39100",
        "eligible_for_search": True,
        "eligible_for_display": True,
    },
    {
        "user_list_id": "777000222",
        "name": "CRM upload — EU customers",
        "description": "Customer match list uploaded from the CRM",
        "list_type": "CRM_BASED",
        "membership_status": "OPEN",
        "membership_life_span_days": "10000",
        "size_for_display": "0",
        "size_for_search": "1200",
        "eligible_for_search": True,
        "eligible_for_display": False,
    },
)


def probe_row(*, fired: bool = True, send_to: str = SEND_TO) -> dict[str, Any]:
    """What `browser.probe_conversion_tags` writes after loading the page."""
    return {
        "url": "https://sdsmanager.com/thanks",
        "fired_at": utcnow().isoformat(),
        "loaded": True,
        "status": 200,
        "error": None,
        "tag_ids": ["AW-987654321", "GTM-ABCDE12"],
        "send_to": [send_to] if fired else [],
        "beacons": [{"beacon": True, "send_to": send_to}] if fired else [],
        "conversion_fired": fired,
        "observed_at": utcnow().isoformat(),
    }


async def seed_readiness(
    project_id: uuid.UUID,
    *,
    probe: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    probe_url: str | None = "https://sdsmanager.com/thanks",
) -> None:
    """Vitals, conversion actions, audience lists and one synthetic probe.

    `probe_url` is not decoration: node 1.5.2 only *asks* for probe evidence when
    the project names a conversion page, so a fixture that seeded the row and
    left the setting unset would find the probe ignored and every tag assertion
    passing as "inconclusive".
    """
    from agent.db.models import Project
    from agent.db.session import get_sessionmaker
    from agent.evidence.normalize import EvidenceDraft
    from agent.evidence.store import EvidenceStore
    from agent.nodes.stage_1_5 import PROBE_URL_SETTING

    drafts = (
        [
            EvidenceDraft(source="web", kind="page_vitals", payload=dict(row))
            for row in PAGE_VITALS_ROWS
        ]
        + [
            EvidenceDraft(source="google_ads", kind="conversion_action", payload=dict(row))
            for row in (actions if actions is not None else conversion_action_rows())
        ]
        + [
            EvidenceDraft(source="google_ads", kind="audience_list", payload=dict(row))
            for row in AUDIENCE_ROWS
        ]
        + [
            EvidenceDraft(
                source="web",
                kind="conversion_probe",
                payload=probe if probe is not None else probe_row(),
            )
        ]
    )
    async with get_sessionmaker()() as session:
        project = await session.get(Project, project_id)
        assert project is not None
        if probe_url:
            project.settings = {**(project.settings or {}), PROBE_URL_SETTING: probe_url}
        store = EvidenceStore(session, project.workspace_id)
        await store.write(drafts, project_id=project_id, embed=False)
        await session.commit()


def _citable_ids(user: str) -> list[str]:
    """The evidence ids node 1.6.1 offered the model, read back out of its prompt.

    Answering with ids taken from the prompt is the point: a fixture that
    returned a hard-coded id would pass even if the node offered the model
    nothing, and the executor's provenance check would never be exercised.
    """
    try:
        rows = _computed(user, "evidence you may cite")
    except AssertionError:
        return []
    return [str(row["id"]) for row in rows if isinstance(row, dict) and row.get("id")]


def stage_1_5_and_1_6(fake: FakeOpenRouter, *, critique: dict[str, Any] | None = None) -> None:
    """Answer stages 1.3 through 1.6, each node from the batch it was sent."""
    stage_1_3_and_1_4(fake)
    # The P4 scripter installed itself as the dispatcher; keep it as the
    # fallthrough rather than duplicating nine nodes' worth of answers.
    inner = fake.dispatcher
    assert inner is not None

    def respond(request: Any) -> Any:
        body = json.loads(request.content or b"{}")
        name = (
            body.get("response_format", {}).get("json_schema", {}).get("name")
            or (body.get("tools") or [{}])[0].get("function", {}).get("name")
            or ""
        )
        user = next(
            (item["content"] for item in reversed(body.get("messages", [])) if item["content"]),
            "",
        )

        if name == "PageMatches":
            rows = _computed(user, "pages and the keyword clusters")
            return completion(
                {
                    "pages": [
                        {
                            "url": row["url"],
                            "message_match": "strong",
                            "note": "The page answers the cluster directly.",
                        }
                        for row in rows
                    ]
                }
            )

        if name == "ConsentVerdicts":
            rows = _computed(user, "audience lists in the account")
            return completion(
                {
                    "lists": [
                        {
                            "name": row["name"],
                            "consent_basis": (
                                "Consent captured at form submission"
                                if row["list_type"] == "REMARKETING"
                                else "No recorded consent for advertising use"
                            ),
                            "markets_allowed": ["US"],
                            "usable": row["list_type"] == "REMARKETING",
                            "blocker": (
                                ""
                                if row["list_type"] == "REMARKETING"
                                else "The CRM export records no advertising consent."
                            ),
                            "evidence_ids": [],
                        }
                        for row in rows
                    ],
                    "reviewer_notes": "Confirm the form's consent wording covers advertising.",
                    "open_questions": ["Who owns the consent register?"],
                }
            )

        if name == "SizingAssumptions":
            rows = _computed(user, "scenarios (final")
            return completion(
                {
                    "scenarios": [
                        {
                            "budget_usd_month": row["budget_usd_month"],
                            "assumptions": [
                                "Cost per click holds at the account's measured average.",
                                "The conversion rate holds across new, colder keywords.",
                            ],
                            "risk": "Colder traffic usually converts worse than brand traffic.",
                        }
                        for row in rows
                    ]
                }
            )

        if name == "ReportNarrative":
            citable = _citable_ids(user)[:3]
            cite = citable[:1] or []
            return completion(
                {
                    "executive_summary": (
                        "The account has a working demand base and a measurable conversion "
                        "path. Two landing pages carry the mapped keywords, one of which asks "
                        "for too much information. Fix that, and the plan is fundable."
                    ),
                    "launch_blockers": [],
                    "recommended_next_actions": [
                        {
                            "statement": "Cut the GHS labeling form to three fields.",
                            "evidence_ids": cite,
                            "confidence": "high",
                        },
                        {
                            "statement": "Add the wasteful terms to the shared negative list.",
                            "evidence_ids": citable[1:2] or cite,
                            "confidence": "medium",
                        },
                        {
                            "statement": "Publish a comparison page for the unmapped cluster.",
                            "evidence_ids": citable[2:3] or cite,
                            "confidence": "medium",
                        },
                    ],
                    "open_questions": ["Which market should the first campaign run in?"],
                }
            )

        if name == "ReportCritique":
            return completion(
                critique
                if critique is not None
                else {
                    "issues": [],
                    "verdict_consistent": True,
                    "unsupported_claims": [],
                    "contradictions": [],
                }
            )

        return inner(request)

    fake.dispatch(respond)
