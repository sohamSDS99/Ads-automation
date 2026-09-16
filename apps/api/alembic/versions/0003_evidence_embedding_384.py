"""Narrow Evidence.embedding to the local embedder's width, and index the text.

PRD §6 specified `vector(1536)`, a width that assumed a hosted OpenAI-shaped
embedding model. OpenRouter — assumption A2's single LLM surface — serves no
embedding model at all, so the vectors are produced locally by the fallback the
PRD itself named (§20 Q6): `BAAI/bge-small-en-v1.5`, which is 384-wide.

pgvector fixes a column's dimension in DDL and an HNSW index cannot span two
widths, so this is a migration rather than a setting.

The `USING NULL` is safe and deliberate: P2 is the first code that ever writes
an embedding, so every existing row has NULL here. The downgrade widens the
column back and is equally lossy — it does not restore vectors, because at
1536 there is nothing that could have written them.

Also adds the lexical half of hybrid search: a GIN index over
`to_tsvector('english', content_text)`. It is an expression index, so no column
is added and nothing has to stay in sync with the text.

And makes `evidence.run_id` nullable. PRD §6 declared it NOT NULL, but §9.5 and
§14 put a CSV upload on `POST /projects/{id}/sources/csv` — a setup-wizard
action that happens before the project has ever been run. Forcing a synthetic
Run row to exist so an upload can reference it would put a lie in the run
history; NULL says the true thing, which is that this evidence arrived outside
any run.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

# Ids match the file-number convention 0001 set; the descriptive part lives
# in the filename, not in the id.
revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | None = None
depends_on: str | None = None

EMBEDDING_DIM = 384
PREVIOUS_DIM = 1536

HNSW_INDEX = "ix_evidence_embedding_hnsw"
FTS_INDEX = "ix_evidence_content_fts"


def _resize(dim: int) -> None:
    # The index is built on the column, so it has to go first and come back after.
    op.execute(f"DROP INDEX IF EXISTS {HNSW_INDEX}")
    op.execute(f"ALTER TABLE evidence ALTER COLUMN embedding TYPE vector({dim}) USING NULL")
    op.execute(f"CREATE INDEX {HNSW_INDEX} ON evidence USING hnsw (embedding vector_cosine_ops)")


def upgrade() -> None:
    _resize(EMBEDDING_DIM)
    # `evidence.search._document()` must emit this expression character for
    # character, or the planner will not use this index.
    op.execute(
        f"CREATE INDEX {FTS_INDEX} ON evidence "
        f"USING gin (to_tsvector('english', coalesce(content_text, '')))"
    )
    op.execute("ALTER TABLE evidence ALTER COLUMN run_id DROP NOT NULL")


def downgrade() -> None:
    # Uploaded evidence has no run to point at, so re-imposing NOT NULL would
    # fail on exactly the rows this migration exists to allow. Drop them.
    op.execute("DELETE FROM evidence WHERE run_id IS NULL")
    op.execute("ALTER TABLE evidence ALTER COLUMN run_id SET NOT NULL")
    op.execute(f"DROP INDEX IF EXISTS {FTS_INDEX}")
    _resize(PREVIOUS_DIM)
