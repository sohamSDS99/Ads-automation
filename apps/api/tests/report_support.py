"""The golden report, and the shapes the renderer tests need around it.

One loader, so every format test renders the *same* document. That is what makes
`test_report_parity.py` meaningful: if each test built its own report, "the PDF
and the markdown contain the same sections" would be a statement about two
fixtures rather than about two renderers.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agent.export.contract import PricedKeyword, ResearchReport

GOLDEN = Path(__file__).parent / "fixtures" / "report_golden.json"

PROJECT_NAME = "Northwind Safety"


def golden_payload() -> dict[str, Any]:
    """The raw fixture, freshly parsed so a mutating test cannot poison another."""
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def golden_report() -> ResearchReport:
    return ResearchReport.model_validate(golden_payload())


def minimal_report(**overrides: Any) -> ResearchReport:
    """The smallest report the contract accepts.

    Every section empty. Renderers are asked to survive this, because a run that
    degrades badly still has to produce a document — a report that only renders
    when it is complete is a report that vanishes exactly when it is needed.
    """
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "project_id": uuid.UUID("11111111-1111-4111-8111-111111111111"),
        "run_id": uuid.UUID("22222222-2222-4222-8222-222222222222"),
        "generated_at": datetime(2026, 3, 4, 9, 30, tzinfo=UTC),
        "executive_summary": "Nothing usable was gathered.",
        "launch_readiness": "no_go",
    }
    payload.update(overrides)
    return ResearchReport.model_validate(payload)


def many_keywords(count: int) -> list[PricedKeyword]:
    """`count` distinct priced keywords with descending volume.

    Used to push past the preview cap so the truncation notice can be asserted
    rather than assumed.
    """
    return [
        PricedKeyword(
            term=f"keyword {index:04d}",
            market="GB",
            intent="transactional",
            volume=10_000 - index,
            cpc_low=1.0,
            cpc_high=2.0,
            competition=0.5,
            seasonality_index=[1.0] * 12,
            best_url="https://example.test/page",
            verdict="good_fit",
        )
        for index in range(count)
    ]
