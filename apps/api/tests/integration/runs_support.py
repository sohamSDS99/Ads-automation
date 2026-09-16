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
        import json as _json

        body = _json.loads(request.content or b"{}")
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
