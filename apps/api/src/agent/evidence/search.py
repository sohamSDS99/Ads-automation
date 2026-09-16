"""Retrieval over stored evidence: filter, full-text, vector, or all three.

`GET /evidence` is the read side of PRD Law 1 — if a node is going to cite a
fact, a human has to be able to find that same fact and check it. So this
returns not just rows but *why* each row matched.

Hybrid search is Reciprocal Rank Fusion over two independent retrievers. RRF
fuses by rank rather than by score, which matters here because the two scores
are not comparable: cosine distance lives in [0, 2] and `ts_rank_cd` is an
unbounded relevance number. Fusing rank positions needs no tuning constant per
corpus, and it degrades gracefully — if one retriever returns nothing, the
result is simply the other one's ranking.

Lexical matching earns its keep on this corpus specifically: evidence is full
of exact tokens an embedding blurs — campaign names, `[exact match]` keywords,
domains, SKUs. Vector search alone would miss a search for `sds-manager.com`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Evidence, EvidenceSource, Project
from agent.evidence.embedding import Embedder, get_embedder

log = structlog.get_logger(__name__)

#: RRF's damping constant. 60 is the value from the original paper and the one
#: every implementation uses; it flattens the difference between rank 1 and 2 so
#: a single retriever cannot dominate a fused list on its own.
RRF_K = 60

#: How deep each retriever goes before fusion. Wider than the page size, because
#: a row ranked 40th by one retriever and 3rd by the other should still surface.
RETRIEVER_DEPTH = 200

MatchedBy = Literal["vector", "text", "both", "filter"]

#: Emitted as literal SQL, not as bound parameters, and both reasons matter.
#: `to_tsvector` needs its first argument typed `regconfig`; a bound string
#: arrives as `varchar` and Postgres refuses the call outright. And the GIN
#: index in migration 0002 is built on this exact expression — a parameterised
#: one would not match it, so the query would silently fall back to a sequential
#: scan over every row of evidence. Neither value is user input.
TS_CONFIG: sa.ColumnClause[str] = sa.literal_column("'english'")
TS_EMPTY: sa.ColumnClause[str] = sa.literal_column("''")


def _document() -> sa.Function[Any]:
    """The indexed expression, character for character (see migration 0002)."""
    return sa.func.to_tsvector(TS_CONFIG, sa.func.coalesce(Evidence.content_text, TS_EMPTY))


@dataclass(slots=True)
class EvidenceHit:
    """One result, and the evidence trail for why it is a result."""

    id: uuid.UUID
    project_id: uuid.UUID
    run_id: uuid.UUID | None
    source: EvidenceSource
    kind: str
    source_url: str | None
    content_text: str | None
    payload: dict[str, Any]
    fetched_at: datetime
    score: float = 0.0
    matched_by: MatchedBy = "filter"
    text_rank: int | None = None
    vector_rank: int | None = None


@dataclass(slots=True)
class SearchPage:
    hits: list[EvidenceHit] = field(default_factory=list)
    next_cursor: str | None = None
    #: True when the query ran the two retrievers rather than a plain filter.
    ranked: bool = False


class EvidenceSearch:
    """Read-side counterpart to `EvidenceStore`, scoped the same way.

    Every statement joins `project` and filters on `workspace_id`. Evidence
    carries no workspace of its own, so that join *is* the authorization
    boundary — not a convenience.
    """

    def __init__(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        *,
        embedder: Embedder | None = None,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self._embedder = embedder

    @property
    def embedder(self) -> Embedder:
        if self._embedder is None:
            self._embedder = get_embedder()
        return self._embedder

    def _scoped(self) -> sa.Select[Any]:
        return (
            sa.select(
                Evidence.id,
                Evidence.project_id,
                Evidence.run_id,
                Evidence.source,
                Evidence.kind,
                Evidence.source_url,
                Evidence.content_text,
                Evidence.payload,
                Evidence.fetched_at,
            )
            .join(Project, Project.id == Evidence.project_id)
            .where(Project.workspace_id == self.workspace_id)
        )

    def _filtered(
        self,
        *,
        project_id: uuid.UUID | None,
        source: EvidenceSource | None,
        kind: str | None,
        run_id: uuid.UUID | None,
    ) -> sa.Select[Any]:
        statement = self._scoped()
        if project_id is not None:
            statement = statement.where(Evidence.project_id == project_id)
        if source is not None:
            statement = statement.where(Evidence.source == source)
        if kind:
            statement = statement.where(Evidence.kind == kind)
        if run_id is not None:
            statement = statement.where(Evidence.run_id == run_id)
        return statement

    @staticmethod
    def _hit(row: Any) -> EvidenceHit:
        return EvidenceHit(
            id=row.id,
            project_id=row.project_id,
            run_id=row.run_id,
            source=row.source,
            kind=row.kind,
            source_url=row.source_url,
            content_text=row.content_text,
            payload=dict(row.payload or {}),
            fetched_at=row.fetched_at,
        )

    async def search(
        self,
        *,
        project_id: uuid.UUID | None = None,
        source: EvidenceSource | None = None,
        kind: str | None = None,
        run_id: uuid.UUID | None = None,
        query: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> SearchPage:
        """Filter, and — when `query` is given — rank by fused relevance."""
        base = self._filtered(project_id=project_id, source=source, kind=kind, run_id=run_id)
        if not query or not query.strip():
            return await self._browse(base, limit=limit, offset=offset)
        return await self._hybrid(base, query.strip(), limit=limit, offset=offset)

    async def _browse(self, base: sa.Select[Any], *, limit: int, offset: int) -> SearchPage:
        """No query: newest first. Ordered by `(fetched_at, id)` so the tie-break
        is total and a page boundary never repeats or drops a row."""
        statement = (
            base.order_by(Evidence.fetched_at.desc(), Evidence.id.desc())
            .limit(limit + 1)
            .offset(offset)
        )
        rows = (await self.session.execute(statement)).all()
        has_more = len(rows) > limit
        hits = [self._hit(row) for row in rows[:limit]]
        return SearchPage(
            hits=hits,
            next_cursor=str(offset + limit) if has_more else None,
            ranked=False,
        )

    async def _hybrid(
        self, base: sa.Select[Any], query: str, *, limit: int, offset: int
    ) -> SearchPage:
        text_ids = await self._text_ranked(base, query)
        vector_ids = await self._vector_ranked(base, query)

        fused: dict[uuid.UUID, EvidenceHit] = {}
        scores: dict[uuid.UUID, float] = {}

        for rank, row_id in enumerate(text_ids, start=1):
            scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (RRF_K + rank)
        for rank, row_id in enumerate(vector_ids, start=1):
            scores[row_id] = scores.get(row_id, 0.0) + 1.0 / (RRF_K + rank)

        if not scores:
            return SearchPage(hits=[], next_cursor=None, ranked=True)

        ordered = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
        window = ordered[offset : offset + limit + 1]
        has_more = len(window) > limit
        window = window[:limit]

        wanted = [row_id for row_id, _ in window]
        rows = (await self.session.execute(self._scoped().where(Evidence.id.in_(wanted)))).all()
        for row in rows:
            fused[row.id] = self._hit(row)

        text_position = {row_id: rank for rank, row_id in enumerate(text_ids, start=1)}
        vector_position = {row_id: rank for rank, row_id in enumerate(vector_ids, start=1)}

        hits: list[EvidenceHit] = []
        for row_id, score in window:
            hit = fused.get(row_id)
            if hit is None:  # deleted between the two statements
                continue
            hit.score = round(score, 6)
            hit.text_rank = text_position.get(row_id)
            hit.vector_rank = vector_position.get(row_id)
            if hit.text_rank and hit.vector_rank:
                hit.matched_by = "both"
            elif hit.vector_rank:
                hit.matched_by = "vector"
            else:
                hit.matched_by = "text"
            hits.append(hit)

        log.info(
            "evidence.search",
            query_len=len(query),
            text_hits=len(text_ids),
            vector_hits=len(vector_ids),
            returned=len(hits),
        )
        return SearchPage(
            hits=hits,
            next_cursor=str(offset + limit) if has_more else None,
            ranked=True,
        )

    async def _text_ranked(self, base: sa.Select[Any], query: str) -> list[uuid.UUID]:
        """Full-text ranking.

        `websearch_to_tsquery` rather than `plainto_tsquery`: it understands
        quoted phrases and `or`, and — the part that matters for a public
        endpoint — it never raises on syntax a user typed.
        """
        document = _document()
        # The query itself stays a bound parameter — it is user input, and it is
        # not part of the index expression, so nothing is lost by binding it.
        tsquery = sa.func.websearch_to_tsquery(TS_CONFIG, query)
        statement = (
            base.where(document.op("@@")(tsquery))
            .order_by(sa.func.ts_rank_cd(document, tsquery).desc(), Evidence.id.desc())
            .limit(RETRIEVER_DEPTH)
        )
        rows = (await self.session.execute(statement)).all()
        return [row.id for row in rows]

    async def _vector_ranked(self, base: sa.Select[Any], query: str) -> list[uuid.UUID]:
        """Nearest neighbours by cosine distance, HNSW-backed.

        Deliberately unthresholded. kNN always returns *something*, so a query
        matching nothing lexically still comes back with its nearest rows — and
        the distance cutoff that would suppress them is model-specific tuning
        that silently costs recall the moment the embedding model changes. The
        caller gets `matched_by` and `score` and can decide.
        """
        try:
            vector = self.embedder.embed_query(query)
        except Exception as exc:  # noqa: BLE001 — search must survive a cold model
            log.warning("evidence.search_vector_unavailable", error=str(exc))
            return []
        statement = (
            base.where(Evidence.embedding.isnot(None))
            .order_by(Evidence.embedding.cosine_distance(vector))
            .limit(RETRIEVER_DEPTH)
        )
        rows = (await self.session.execute(statement)).all()
        return [row.id for row in rows]
