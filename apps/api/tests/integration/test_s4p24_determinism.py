"""S4-P24 — PRD §17 CC9: identical `CreativeInput` + cassettes ⇒ identical runs.

A run is a function of its pinned input, the rows that input points at, and
the recorded model outputs — never of the random UUIDs rows happen to get.
Evidence written in one transaction shares its `fetched_at` (Postgres `now()`
is the transaction's start), so a query ordered `(fetched_at, id)` lists those
rows in UUID order: a different prompt for the same input on every run, which
a cassette keyed by what was asked cannot replay. The tie-break is the row's
content hash, unique per project.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Evidence, EvidenceSource
from agent.nodes.creative import n4_3_3_lead_form_asset
from agent.nodes.creative.n4_3_1_sitelinks_callouts_snippets import SITELINKS_CALLOUTS_SNIPPETS

pytestmark = pytest.mark.asyncio

FETCHED = datetime(2026, 9, 26, 9, 0, tzinfo=UTC)


async def _pages_in_one_transaction(
    db: AsyncSession, project_id: uuid.UUID, kind: str, source: EvidenceSource
) -> list[str]:
    """Three rows sharing `fetched_at`, their ids in the REVERSE of their hash order."""
    hashes = [f"{kind}-alpha", f"{kind}-mid", f"{kind}-zeta"]
    ids = sorted((uuid.uuid4() for _ in hashes), reverse=True)
    for digest, row_id in zip(hashes, ids, strict=True):
        db.add(
            Evidence(
                id=row_id,
                project_id=project_id,
                source=source,
                kind=kind,
                source_url=f"https://sdsmanager.com/{digest}",
                payload={"url": f"https://sdsmanager.com/{digest}"},
                hash=digest,
                fetched_at=FETCHED,
            )
        )
    await db.commit()
    return hashes


def _ctx(db: AsyncSession, project_id: uuid.UUID) -> Any:
    return SimpleNamespace(db=db, project=SimpleNamespace(id=project_id))


async def test_4_3_1_lists_pages_crawled_together_in_content_order_not_uuid_order(
    db: AsyncSession, project_id: uuid.UUID
) -> None:
    expected = await _pages_in_one_transaction(db, project_id, "page", EvidenceSource.WEB)
    rows = await SITELINKS_CALLOUTS_SNIPPETS.gather(_ctx(db, project_id))
    assert [row.hash for row in rows] == expected


async def test_4_3_3_reads_evidence_written_together_in_content_order_not_uuid_order(
    db: AsyncSession, project_id: uuid.UUID
) -> None:
    expected = await _pages_in_one_transaction(db, project_id, "crm_won", EvidenceSource.CSV)
    rows = await n4_3_3_lead_form_asset._evidence(
        _ctx(db, project_id), ["crm_won"], EvidenceSource.CSV
    )
    assert [row.hash for row in rows] == expected
