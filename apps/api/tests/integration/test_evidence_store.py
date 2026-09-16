"""Dedupe, scoping and embedding, against a real Postgres.

`UNIQUE(project_id, hash)` is the mechanism PRD §6 specifies for "dedupe across
runs", and it only means something if the writer actually relies on it rather
than on a pre-flight SELECT. These tests exercise it the way a run does:
repeatedly, concurrently, and across projects.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import EMBEDDING_DIM, Evidence, EvidenceSource
from agent.evidence.embedding import HashingEmbedder
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.store import EvidenceScopeError, EvidenceStore


def draft(term: str, *, kind: str = "search_term_pnl", cost: float = 10.0) -> EvidenceDraft:
    return EvidenceDraft(
        source=EvidenceSource.GOOGLE_ADS,
        kind=kind,
        payload={"search_term": term, "cost": cost},
    )


def store(db: AsyncSession, workspace_id: uuid.UUID) -> EvidenceStore:
    return EvidenceStore(db, workspace_id, embedder=HashingEmbedder())


async def test_drafts_become_rows(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    result = await store(db, workspace_id).write(
        [draft("sds software"), draft("ghs labels")], project_id=project_id
    )
    await db.commit()
    assert result.inserted == 2
    assert result.total == 2
    assert len(set(result.evidence_ids)) == 2


async def test_rewriting_the_same_evidence_inserts_nothing(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """The point of dedupe: a re-run must not double the corpus."""
    writer = store(db, workspace_id)
    first = await writer.write([draft("sds software")], project_id=project_id)
    await db.commit()
    second = await writer.write([draft("sds software")], project_id=project_id)
    await db.commit()

    assert first.inserted == 1
    assert second.inserted == 0
    assert second.duplicates == 1
    # Crucially it still returns the id — a node citing this fact does not care
    # which run first retrieved it.
    assert second.evidence_ids == first.evidence_ids


async def test_duplicates_inside_one_batch_collapse(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """Without this, one INSERT carries two identical keys and Postgres rejects the lot."""
    result = await store(db, workspace_id).write(
        [draft("sds software"), draft("sds software"), draft("ghs labels")], project_id=project_id
    )
    await db.commit()
    assert result.inserted == 2
    assert result.duplicates == 1


async def test_a_changed_metric_is_a_new_fact(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    writer = store(db, workspace_id)
    await writer.write([draft("sds software", cost=10.0)], project_id=project_id)
    await db.commit()
    second = await writer.write([draft("sds software", cost=11.0)], project_id=project_id)
    await db.commit()
    assert second.inserted == 1


async def test_the_same_fact_in_two_projects_is_two_rows(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, second_project_id: uuid.UUID
) -> None:
    """Dedupe is scoped per project_id, so two products can hold the same keyword."""
    writer = store(db, workspace_id)
    first = await writer.write([draft("sds software")], project_id=project_id)
    second = await writer.write([draft("sds software")], project_id=second_project_id)
    await db.commit()
    assert first.inserted == 1
    assert second.inserted == 1
    assert first.evidence_ids != second.evidence_ids


async def test_concurrent_writers_produce_one_row_and_both_learn_its_id(
    workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """The race `ON CONFLICT DO NOTHING` plus the recovery read exists to survive."""
    from agent.db.session import get_sessionmaker

    async def write_once() -> list[uuid.UUID]:
        async with get_sessionmaker()() as session:
            result = await EvidenceStore(session, workspace_id, embedder=HashingEmbedder()).write(
                [draft("contested term")], project_id=project_id
            )
            await session.commit()
            return result.evidence_ids

    results = await asyncio.gather(*(write_once() for _ in range(5)))

    async with get_sessionmaker()() as session:
        count = await session.execute(
            sa.select(sa.func.count())
            .select_from(Evidence)
            .where(Evidence.project_id == project_id)
        )
        assert count.scalar_one() == 1
    # Every writer must come away with the id, not just the one that won.
    assert all(len(ids) == 1 for ids in results)
    assert len({ids[0] for ids in results}) == 1


async def test_embeddings_are_written_at_the_column_width(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await store(db, workspace_id).write([draft("sds software")], project_id=project_id)
    await db.commit()
    row = (await db.execute(sa.select(Evidence))).scalar_one()
    assert row.embedding is not None
    assert len(row.embedding) == EMBEDDING_DIM


async def test_content_text_is_populated_for_every_row(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """A row with no content_text can never be retrieved by either retriever."""
    await store(db, workspace_id).write([draft("sds software")], project_id=project_id)
    await db.commit()
    row = (await db.execute(sa.select(Evidence))).scalar_one()
    assert row.content_text and "sds software" in row.content_text


async def test_embedding_can_be_deferred_and_backfilled(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """A bulk connector can write first and pay for vectors once, in the worker."""
    writer = store(db, workspace_id)
    await writer.write([draft("sds software")], project_id=project_id, embed=False)
    await db.commit()
    row = (await db.execute(sa.select(Evidence))).scalar_one()
    assert row.embedding is None

    filled = await writer.backfill_embeddings(project_id)
    await db.commit()
    assert filled == 1
    refreshed = (await db.execute(sa.select(Evidence))).scalar_one()
    assert refreshed.embedding is not None


async def test_backfill_is_idempotent(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    writer = store(db, workspace_id)
    await writer.write([draft("sds software")], project_id=project_id, embed=False)
    await db.commit()
    await writer.backfill_embeddings(project_id)
    await db.commit()
    assert await writer.backfill_embeddings(project_id) == 0


async def test_evidence_can_be_written_without_a_run(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """A CSV uploaded in the setup wizard has no run to belong to (migration 0002)."""
    await store(db, workspace_id).write([draft("sds software")], project_id=project_id, run_id=None)
    await db.commit()
    row = (await db.execute(sa.select(Evidence))).scalar_one()
    assert row.run_id is None


async def test_a_project_in_another_workspace_is_refused(
    db: AsyncSession, project_id: uuid.UUID
) -> None:
    """Evidence has no workspace_id of its own; the project_id join is the boundary."""
    intruder = EvidenceStore(db, uuid.uuid4(), embedder=HashingEmbedder())
    with pytest.raises(EvidenceScopeError):
        await intruder.write([draft("sds software")], project_id=project_id)


async def test_an_unknown_project_is_refused(db: AsyncSession, workspace_id: uuid.UUID) -> None:
    with pytest.raises(EvidenceScopeError):
        await store(db, workspace_id).write([draft("x")], project_id=uuid.uuid4())


async def test_writing_nothing_is_not_an_error(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """A connector that legitimately found nothing must not raise."""
    result = await store(db, workspace_id).write([], project_id=project_id)
    assert result.total == 0
    assert result.inserted == 0
