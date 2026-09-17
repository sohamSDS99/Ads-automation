"""Business-context documents: the uploaded file, and the evidence it becomes.

Two changes, one feature. Step 1 of the setup wizard now takes a PDF, Word
file, CSV or text file and turns it into passages the research nodes may cite.

**`project_document`** is the record of the upload: filename, size, the
extracted text, and what the extractor could not read. It does not hold the
original bytes — the Volume belongs to `worker` (PRD §5.2) and `api` is what
receives the upload — so the text *is* the artifact, and it is stored where
every reader of it already looks.

**`evidence_source.upload`** is a new enum value rather than a reuse of `csv`.
They arrive through the same kind of form and mean different things: `csv` is a
mapped CRM export with one row per deal, and `upload` is prose a person wrote
about their own business. The Evidence Explorer filters on this column, and
conflating the two would file a pricing PDF under closed-won deals.

`ALTER TYPE … ADD VALUE` is the reason for the autocommit block. Postgres
refuses to add an enum value inside a transaction that then *uses* it, and
Alembic runs a migration in exactly that kind of transaction — the
`CREATE TABLE` below would fail against the value this statement just added.

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE evidence_source ADD VALUE IF NOT EXISTS 'upload'")

    op.create_table(
        "project_document",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "uploaded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("passage_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("unit", sa.Text(), nullable=False, server_default=""),
        sa.Column("unit_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("project_id", "sha256", name="uq_project_document_sha"),
    )

    # Deleting a document deletes its passages, and the link between the two
    # lives in the evidence payload. Without this the delete would sequential
    # scan every evidence row in the workspace to find a handful.
    op.create_index(
        "ix_evidence_document_id",
        "evidence",
        [sa.text("(payload ->> 'document_id')")],
        postgresql_where=sa.text("kind = 'brand_doc'"),
    )


def downgrade() -> None:
    op.drop_index("ix_evidence_document_id", table_name="evidence")
    op.drop_table("project_document")
    # The enum value stays. Removing one from a Postgres enum means rebuilding
    # the type and rewriting every column that uses it, and a downgrade that
    # rewrites the evidence table is more dangerous than an unused label.
