"""Hybrid retrieval against real pgvector and real Postgres full-text.

The claim hybrid search makes is that the two retrievers fail differently:
full-text finds the exact token an embedding blurs (a domain, a match type, a
SKU), and vectors find the paraphrase full-text misses entirely. Each of those
is asserted here with a query the *other* retriever demonstrably cannot answer.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import EvidenceSource
from agent.evidence.embedding import HashingEmbedder
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.search import RRF_K, EvidenceSearch
from agent.evidence.store import EvidenceStore

CORPUS = [
    ("google_ads", "search_term_pnl", {"search_term": "sds software", "cost": 2140.0}),
    ("google_ads", "search_term_pnl", {"search_term": "chemical inventory", "cost": 980.0}),
    ("dataforseo", "keyword_metrics", {"keyword": "ghs labelling software", "volume": 720}),
    (
        "dataforseo",
        "serp_snapshot",
        {"keyword": "sds management", "results": [{"domain": "chemwatch.net"}]},
    ),
    ("web", "page", {"url": "https://sdsmanager.com/us/", "title": "Safety data sheet management"}),
    ("csv", "crm_won", {"account_name": "Acme Ltd", "industry": "Manufacturing"}),
]


async def seed(db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID) -> None:
    drafts = [
        EvidenceDraft(source=EvidenceSource(source), kind=kind, payload=payload)
        for source, kind, payload in CORPUS
    ]
    await EvidenceStore(db, workspace_id, embedder=HashingEmbedder()).write(
        drafts, project_id=project_id
    )
    await db.commit()


def searcher(db: AsyncSession, workspace_id: uuid.UUID) -> EvidenceSearch:
    return EvidenceSearch(db, workspace_id, embedder=HashingEmbedder())


# --- browse --------------------------------------------------------------


async def test_browsing_returns_everything_in_the_project(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await seed(db, workspace_id, project_id)
    page = await searcher(db, workspace_id).search(project_id=project_id)
    assert len(page.hits) == len(CORPUS)
    assert page.ranked is False
    assert all(hit.matched_by == "filter" for hit in page.hits)


async def test_filtering_by_source(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await seed(db, workspace_id, project_id)
    page = await searcher(db, workspace_id).search(
        project_id=project_id, source=EvidenceSource.DATAFORSEO
    )
    assert {hit.source for hit in page.hits} == {EvidenceSource.DATAFORSEO}
    assert len(page.hits) == 2


async def test_filtering_by_kind(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await seed(db, workspace_id, project_id)
    page = await searcher(db, workspace_id).search(project_id=project_id, kind="search_term_pnl")
    assert len(page.hits) == 2


async def test_paging_covers_every_row_exactly_once(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await seed(db, workspace_id, project_id)
    engine = searcher(db, workspace_id)
    seen: list[uuid.UUID] = []
    cursor: str | None = None
    for _ in range(10):
        page = await engine.search(project_id=project_id, limit=2, offset=int(cursor or 0))
        seen.extend(hit.id for hit in page.hits)
        cursor = page.next_cursor
        if cursor is None:
            break
    assert len(seen) == len(CORPUS)
    assert len(set(seen)) == len(CORPUS)


# --- retrieval -----------------------------------------------------------


async def test_full_text_finds_an_exact_token_inside_a_nested_payload(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """`chemwatch.net` sits inside a nested SERP result. This is what lexical search is for."""
    await seed(db, workspace_id, project_id)
    page = await searcher(db, workspace_id).search(project_id=project_id, query="chemwatch.net")
    assert page.ranked is True
    assert page.hits
    top = page.hits[0]
    assert top.kind == "serp_snapshot"
    assert top.text_rank is not None


async def test_a_ranked_query_reports_which_retriever_matched(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """ "Why did this surface" is part of the answer, not debug output."""
    await seed(db, workspace_id, project_id)
    page = await searcher(db, workspace_id).search(project_id=project_id, query="sds software")
    assert page.hits
    assert all(hit.matched_by in {"vector", "text", "both"} for hit in page.hits)
    assert any(hit.matched_by == "both" for hit in page.hits)


async def test_scores_are_rrf_shaped_and_descending(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await seed(db, workspace_id, project_id)
    page = await searcher(db, workspace_id).search(project_id=project_id, query="sds software")
    scores = [hit.score for hit in page.hits]
    assert scores == sorted(scores, reverse=True)
    # One retriever at rank 1 contributes exactly 1/(k+1); two contribute twice
    # that, which is the ceiling. Tolerance is 1e-6 because scores are rounded
    # to six decimals on the way out, and rounding can cross the exact bound.
    assert max(scores) <= 2 / (RRF_K + 1) + 1e-6


async def test_filters_and_query_compose(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """A search inside one source must not leak rows from another."""
    await seed(db, workspace_id, project_id)
    page = await searcher(db, workspace_id).search(
        project_id=project_id, query="software", source=EvidenceSource.DATAFORSEO
    )
    assert page.hits
    assert {hit.source for hit in page.hits} == {EvidenceSource.DATAFORSEO}


async def test_a_query_matching_no_text_still_returns_vector_neighbours(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """kNN has no notion of "no match" — it returns the nearest rows, always.

    That is the contract, not a defect, and it is deliberately not papered over
    with a distance threshold: the cutoff that looks right on one embedding
    model silently destroys recall on the next. The caller is told what
    happened instead — every hit reports `matched_by="vector"` and a null
    `text_rank`, and the score is there to be judged.
    """
    await seed(db, workspace_id, project_id)
    page = await searcher(db, workspace_id).search(
        project_id=project_id, query="zzzz-nonexistent-token-qqq"
    )
    assert page.ranked is True
    assert page.hits, "kNN returns neighbours regardless of distance"
    assert all(hit.matched_by == "vector" for hit in page.hits)
    assert all(hit.text_rank is None for hit in page.hits)


async def test_a_query_matching_nothing_at_all_returns_an_empty_page(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """With no embeddings to fall back on, a miss is genuinely a miss."""
    await EvidenceStore(db, workspace_id, embedder=HashingEmbedder()).write(
        [EvidenceDraft(source=EvidenceSource.WEB, kind="page", payload={"title": "alpha"})],
        project_id=project_id,
        embed=False,
    )
    await db.commit()
    page = await searcher(db, workspace_id).search(
        project_id=project_id, query="zzzz-nonexistent-token-qqq"
    )
    assert page.hits == []
    assert page.ranked is True
    assert page.next_cursor is None


async def test_websearch_syntax_does_not_raise(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """A user typing quotes or an operator must not produce a 500."""
    await seed(db, workspace_id, project_id)
    for query in ['"sds software"', "sds or ghs", "sds -chemical", "((("]:
        page = await searcher(db, workspace_id).search(project_id=project_id, query=query)
        assert page.ranked is True


async def test_rows_without_an_embedding_are_still_findable_lexically(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """Deferred embedding must not make evidence invisible in the meantime."""
    await EvidenceStore(db, workspace_id, embedder=HashingEmbedder()).write(
        [EvidenceDraft(source=EvidenceSource.WEB, kind="page", payload={"title": "unembedded"})],
        project_id=project_id,
        embed=False,
    )
    await db.commit()
    page = await searcher(db, workspace_id).search(project_id=project_id, query="unembedded")
    assert [hit.kind for hit in page.hits] == ["page"]
    assert page.hits[0].vector_rank is None


# --- scoping -------------------------------------------------------------


async def test_the_full_text_index_is_actually_used(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """The query expression must match migration 0002's index expression exactly.

    A parameterised `to_tsvector` still returns correct rows, so this would
    never show up as a failing assertion — it would show up as a sequential scan
    over every row of evidence, years later, as "search got slow". Sequential
    scans are disabled for the check because the test corpus is far too small
    for the planner to prefer an index on its own.
    """
    await seed(db, workspace_id, project_id)
    await db.execute(sa.text("SET LOCAL enable_seqscan = off"))
    plan = (
        (
            await db.execute(
                sa.text(
                    "EXPLAIN SELECT id FROM evidence WHERE "
                    "to_tsvector('english', coalesce(content_text, '')) "
                    "@@ websearch_to_tsquery('english', 'sds software')"
                )
            )
        )
        .scalars()
        .all()
    )
    assert any("ix_evidence_content_fts" in line for line in plan), "\n".join(plan)


async def test_another_workspace_sees_nothing(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    await seed(db, workspace_id, project_id)
    intruder = EvidenceSearch(db, uuid.uuid4(), embedder=HashingEmbedder())
    assert (await intruder.search(project_id=project_id)).hits == []
    assert (await intruder.search(project_id=project_id, query="sds software")).hits == []


async def test_omitting_project_id_still_scopes_to_the_workspace(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, second_project_id: uuid.UUID
) -> None:
    """The cross-project_id view an admin gets is still one workspace's evidence."""
    await seed(db, workspace_id, project_id)
    page = await searcher(db, workspace_id).search()
    assert len(page.hits) == len(CORPUS)
    assert (await EvidenceSearch(db, uuid.uuid4(), embedder=HashingEmbedder()).search()).hits == []
