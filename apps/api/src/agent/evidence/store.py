"""Drafts in, deduped rows out.

This is the only writer of the `evidence` table, and it is the place PRD Law 1
is actually enforced: nodes cite `evidence_id`s, so every fact a node may cite
has to have come through here first.

Dedupe is the database's job, not a pre-flight SELECT: `UNIQUE(project_id,
hash)` plus `ON CONFLICT DO NOTHING` means two connectors racing on the same
fact produce one row, and the loser learns the winner's id rather than an error.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import Settings, get_settings
from agent.db.models import Evidence, Project
from agent.evidence.embedding import Embedder, get_embedder
from agent.evidence.normalize import EvidenceDraft

log = structlog.get_logger(__name__)


class EvidenceScopeError(PermissionError):
    """The project named does not belong to the caller's workspace."""


@dataclass(slots=True)
class StoreResult:
    """What a write actually did. `evidence_ids` is what a node cites."""

    evidence_ids: list[uuid.UUID] = field(default_factory=list)
    inserted: int = 0
    duplicates: int = 0

    @property
    def total(self) -> int:
        return len(self.evidence_ids)


class EvidenceStore:
    """Writes and reads evidence for one workspace.

    Evidence has no `workspace_id` of its own — it hangs off Project — so this
    deliberately does not subclass `WorkspaceScopedRepo`, which refuses models
    it cannot scope directly. Scoping happens instead by resolving the project
    through the workspace on every call. There is no constructor that lets a
    caller skip that.
    """

    def __init__(
        self,
        session: AsyncSession,
        workspace_id: uuid.UUID,
        *,
        embedder: Embedder | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self._settings = settings or get_settings()
        self._embedder = embedder

    @property
    def embedder(self) -> Embedder:
        # Resolved late: the API process should not pay to load an ONNX model
        # for a request that turns out to write nothing.
        if self._embedder is None:
            self._embedder = get_embedder(self._settings)
        return self._embedder

    async def assert_project(self, project_id: uuid.UUID) -> Project:
        """Resolve a project inside this workspace, or refuse."""
        result = await self.session.execute(
            sa.select(Project).where(
                Project.id == project_id, Project.workspace_id == self.workspace_id
            )
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise EvidenceScopeError(f"project {project_id} is not in this workspace")
        return project

    async def write(
        self,
        drafts: list[EvidenceDraft],
        *,
        project_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        embed: bool = True,
    ) -> StoreResult:
        """Persist `drafts`, skipping anything this project already holds.

        Returns every id — new and pre-existing — because a node that cites a
        fact does not care which run first retrieved it.
        """
        await self.assert_project(project_id)
        result = StoreResult()
        if not drafts:
            return result

        # Collapse duplicates inside the batch first. Without this, one
        # statement would carry two rows with the same key and Postgres would
        # reject the whole INSERT rather than deduping it.
        unique: dict[str, EvidenceDraft] = {}
        for draft in drafts:
            unique.setdefault(draft.hash(), draft)
        result.duplicates += len(drafts) - len(unique)

        known = await self._existing_ids(project_id, list(unique))
        fresh = {digest: draft for digest, draft in unique.items() if digest not in known}
        result.evidence_ids.extend(known.values())
        result.duplicates += len(known)

        if not fresh:
            return result

        texts = [draft.text() for draft in fresh.values()]
        vectors: list[list[float] | None] = (
            list(self.embedder.embed(texts)) if embed else [None] * len(texts)
        )

        rows = [
            {
                "id": uuid.uuid4(),
                "project_id": project_id,
                "run_id": run_id,
                "source": draft.source,
                "source_url": draft.source_url,
                "kind": draft.kind,
                "payload": draft.payload,
                "content_text": text,
                "embedding": vector,
                "hash": digest,
            }
            for (digest, draft), text, vector in zip(fresh.items(), texts, vectors, strict=True)
        ]

        statement = (
            pg_insert(Evidence)
            .values(rows)
            .on_conflict_do_nothing(constraint="uq_evidence_project_hash")
            .returning(Evidence.id, Evidence.hash)
        )
        written = (await self.session.execute(statement)).all()
        result.inserted = len(written)
        result.evidence_ids.extend(row[0] for row in written)

        # Anything the INSERT declined was written by a concurrent caller
        # between our SELECT and now. Read their ids rather than losing the fact.
        raced = set(fresh) - {row[1] for row in written}
        if raced:
            recovered = await self._existing_ids(project_id, list(raced))
            result.duplicates += len(recovered)
            result.evidence_ids.extend(recovered.values())

        log.info(
            "evidence.written",
            project_id=str(project_id),
            run_id=str(run_id) if run_id else None,
            inserted=result.inserted,
            duplicates=result.duplicates,
            embedded=embed,
        )
        return result

    async def _existing_ids(self, project_id: uuid.UUID, hashes: list[str]) -> dict[str, uuid.UUID]:
        if not hashes:
            return {}
        result = await self.session.execute(
            sa.select(Evidence.hash, Evidence.id).where(
                Evidence.project_id == project_id, Evidence.hash.in_(hashes)
            )
        )
        return {row[0]: row[1] for row in result.all()}

    async def backfill_embeddings(self, project_id: uuid.UUID, *, limit: int = 500) -> int:
        """Embed rows written with `embed=False`. Returns how many were filled."""
        await self.assert_project(project_id)
        result = await self.session.execute(
            sa.select(Evidence.id, Evidence.content_text)
            .where(
                Evidence.project_id == project_id,
                Evidence.embedding.is_(None),
                Evidence.content_text.isnot(None),
            )
            .limit(limit)
        )
        pending = result.all()
        if not pending:
            return 0
        vectors = self.embedder.embed([row[1] or "" for row in pending])
        for (row_id, _), vector in zip(pending, vectors, strict=True):
            await self.session.execute(
                sa.update(Evidence).where(Evidence.id == row_id).values(embedding=vector)
            )
        return len(pending)
