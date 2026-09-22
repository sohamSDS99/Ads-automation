"""Regenerate the five golden `PlanInput` fixtures.

    uv run python -m tests.eval.build_plan_fixtures

Checked-in JSON rather than objects built at import time, for the same reason
Stage 01's fixtures are files: a fixture that is code changes silently when the
code around it does, and the point of a golden case is that somebody has to
look at a diff. The builder stays so the five can be regenerated when the
`PlanInput` contract moves, and so the *deltas between them* are readable in
one place — which they are not, spread across five documents.

The five are chosen to span the input conditions §18 says change the plan:

| fixture | what it holds |
|---|---|
| `baseline_single_market` | the happy path — one market, a brand, clean research |
| `multi_market_consent_blocked` | gate 1.5.3 refused a market (PC1) |
| `launch_blockers_carried` | research left blockers that must survive the crossing |
| `degraded_forecast_override` | a degraded source **and** an accepted `no_go` |
| `eu_only_no_brand` | EU consent signal required; no brand; no priced keywords |

Every one of them has to produce a plan with **zero blocking critique issues**.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.api.schemas_projects import Market
from agent.export.contract import ResearchReport
from agent.schemas.plan_input import PlanInput

FIXTURES = Path(__file__).parent / "plan"

#: Fixed, so regenerating produces the same bytes. A `uuid4()` here would make
#: every regeneration a diff and `PlanInput.content_hash` a moving target.
PROJECT_ID = uuid.UUID("0eaa9000-0000-4000-8000-000000000001")
ACCEPTED_BY = uuid.UUID("0eaa9000-0000-4000-8000-000000000002")
EVIDENCE_ID = uuid.UUID("0eaa4444-4444-4444-8444-444444444444")
ACCEPTED_AT = datetime(2026, 9, 1, 9, 0, tzinfo=UTC)


def _market(country: str, language: str = "en", currency: str = "USD") -> Market:
    return Market(country=country, language=language, currency=currency)


def _keyword(term: str, market: str, *, volume: int = 900, cpc: float = 4.2) -> dict[str, Any]:
    return {
        "term": term,
        "market": market,
        "intent": "transactional",
        "volume": volume,
        "cpc_low": cpc - 1.0,
        "cpc_high": cpc,
        "match_type": "phrase",
        "verdict": "good_fit",
    }


def _consent_list(
    name: str, markets: list[str], *, usable: bool, basis: str | None, blocker: str | None = None
) -> dict[str, Any]:
    return {
        "name": name,
        "markets_allowed": markets,
        "usable": usable,
        "consent_basis": basis,
        "blocker": blocker,
        "size": 4_200,
        "evidence_ids": [str(EVIDENCE_ID)],
    }


def _claim(statement: str) -> dict[str, Any]:
    return {
        "statement": statement,
        "evidence_ids": [str(EVIDENCE_ID)],
        "confidence": "high",
    }


def _report(**sections: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "project_id": PROJECT_ID,
        "run_id": uuid.UUID("0eaa9000-0000-4000-8000-000000000003"),
        "generated_at": ACCEPTED_AT,
        "executive_summary": "Ready to plan.",
        "launch_readiness": "go",
    }
    payload.update(sections)
    return json.loads(ResearchReport.model_validate(payload).model_dump_json())


def _input(
    *,
    run_seed: int,
    report: dict[str, Any],
    markets: list[Market],
    product_context: dict[str, Any],
    keywords: list[dict[str, Any]],
    degraded: list[str] | None = None,
    override_reason: str | None = None,
) -> PlanInput:
    research = ResearchReport.model_validate(report)
    return PlanInput(
        project_id=PROJECT_ID,
        research_run_id=uuid.UUID(f"0eaa9000-0000-4000-8000-0000000{run_seed:05d}"),
        research_report_id=uuid.UUID(f"0eaa9001-0000-4000-8000-0000000{run_seed:05d}"),
        research_schema_version="1.0",
        accepted_by=ACCEPTED_BY,
        accepted_at=ACCEPTED_AT,
        override_reason=override_reason,
        launch_readiness=research.launch_readiness,
        launch_blockers=research.launch_blockers,
        business_context=research.business_context,
        account_learnings=research.account_learnings,
        competitive_landscape=research.competitive_landscape,
        demand_map=research.demand_map,
        readiness=research.readiness,
        priced_keyword_list=[item.model_copy() for item in research.priced_keyword_list],
        degraded_sources=degraded or [],
        markets=markets,
        product_context=product_context,
    )


def _priced(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {"priced_keyword_list": rows}


# ---------------------------------------------------------------------------
# the five
# ---------------------------------------------------------------------------


def cases() -> dict[str, tuple[str, PlanInput]]:
    """`{filename stem: (why, PlanInput)}`."""
    return {
        "baseline_single_market": (
            "The happy path: one market, a brand to isolate, priced demand and "
            "nothing outstanding. Every other fixture is this one with a "
            "condition added, so a failure here is a failure of the harness.",
            _input(
                run_seed=10,
                report=_report(
                    **_priced([_keyword("safety data sheet software", "US")]),
                    readiness={
                        "lists": [
                            _consent_list(
                                "US customers",
                                ["US"],
                                usable=True,
                                basis="Contract, with a marketing opt-in on file.",
                            )
                        ]
                    },
                ),
                markets=[_market("US")],
                product_context={"brand_terms": ["sds manager"]},
                keywords=[],
            ),
        ),
        "multi_market_consent_blocked": (
            "Gate 1.5.3 refused Germany, so PC1 says no audience channel, no "
            "audience test and no offline upload may name DE — while DE still "
            "gets a search campaign, because consent governs audience lists "
            "and not keywords. The distinction a hand-written fixture blurs.",
            _input(
                run_seed=20,
                report=_report(
                    **_priced(
                        [
                            _keyword("safety data sheet software", "US"),
                            _keyword("sicherheitsdatenblatt software", "DE", cpc=3.1),
                            _keyword("logiciel fds", "FR", cpc=2.8),
                        ]
                    ),
                    readiness={
                        "lists": [
                            _consent_list(
                                "US and FR customers",
                                ["US", "FR"],
                                usable=True,
                                basis="Contract, with a marketing opt-in on file.",
                            ),
                            _consent_list(
                                "DE customers",
                                ["DE"],
                                usable=False,
                                basis=None,
                                blocker=(
                                    "No lawful basis recorded for ad-platform audience "
                                    "upload in Germany."
                                ),
                            ),
                        ]
                    },
                ),
                markets=[_market("US"), _market("DE", "de", "EUR"), _market("FR", "fr", "EUR")],
                product_context={"brand_terms": ["sds manager"]},
                keywords=[],
            ),
        ),
        "launch_blockers_carried": (
            "Research finished with three things unresolved. Assertion 10 says "
            "every one of them is resolved in the plan or listed in "
            "`open_dependencies`; a blocker that is neither has been lost "
            "between the stages, which is the failure this fixture exists for.",
            _input(
                run_seed=30,
                report=_report(
                    launch_blockers=[
                        _claim("The conversion tag is not live on the pricing page."),
                        _claim("No GB pricing page exists for the terms we would bid on."),
                        _claim("The CRM does not record which leads came from paid."),
                    ],
                    **_priced(
                        [
                            _keyword("safety data sheet software", "US"),
                            _keyword("coshh software", "GB", cpc=3.6),
                        ]
                    ),
                    readiness={
                        "lists": [
                            _consent_list(
                                "US and GB customers",
                                ["US", "GB"],
                                usable=True,
                                basis="Contract, with a marketing opt-in on file.",
                            )
                        ]
                    },
                ),
                markets=[_market("US"), _market("GB", "en", "GBP")],
                product_context={"brand_terms": ["sds manager"]},
                keywords=[],
            ),
        ),
        "degraded_forecast_override": (
            "Two §18 rows at once: the Google Ads forecast service was "
            "unavailable, so the plan is built on derived arithmetic and must "
            "carry `degraded_sources`; and an admin accepted a `no_go` verdict "
            "with a written reason, which travels on the input and is printed "
            "on the plan rather than being looked up again later.",
            _input(
                run_seed=40,
                report=_report(
                    launch_readiness="no_go",
                    **_priced([_keyword("safety data sheet software", "US")]),
                    readiness={
                        "lists": [
                            _consent_list(
                                "US customers",
                                ["US"],
                                usable=True,
                                basis="Contract, with a marketing opt-in on file.",
                            )
                        ]
                    },
                ),
                markets=[_market("US")],
                product_context={"brand_terms": ["sds manager"]},
                keywords=[],
                degraded=["google_ads_forecast"],
                override_reason="The board accepted the risk in writing on 2026-08-30.",
            ),
        ),
        "eu_only_no_brand": (
            "EU-only scope with no brand and no priced demand. Three things "
            "are being held: §13's consent-signal mechanism must be named when "
            "EU markets are in scope; a plan with no brand terms has no "
            "isolation section at all, and assertion 5 must pass by returning "
            "early rather than by finding nothing; and an empty keyword list "
            "must produce an account the assertions pass over vacuously "
            "instead of crashing on.",
            _input(
                run_seed=50,
                report=_report(
                    readiness={
                        "lists": [
                            _consent_list(
                                "EU customers",
                                ["DE", "NL"],
                                usable=True,
                                basis="Consent captured at sign-up, logged per market.",
                            )
                        ]
                    },
                ),
                markets=[_market("DE", "de", "EUR"), _market("NL", "nl", "EUR")],
                product_context={},
                keywords=[],
            ),
        ),
    }


def main() -> None:
    FIXTURES.mkdir(parents=True, exist_ok=True)
    for stem, (why, source) in cases().items():
        document = {
            "name": stem,
            "why": why,
            "plan_input": json.loads(source.model_dump_json()),
        }
        path = FIXTURES / f"{stem}.json"
        path.write_text(json.dumps(document, indent=2, sort_keys=False) + "\n")
        print(f"wrote {path.relative_to(Path.cwd())}")


if __name__ == "__main__":
    main()
