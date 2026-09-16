"""Give `export` a lifecycle, and the CSV format the PRD asks for.

Two corrections to the P0 schema, both surfaced by building the export pipeline
against PRD §12.

**`csv` was missing from `export_format`.** §12's format table lists five rows
and §14 spells the query parameter `format=pdf|docx|md|json|csv`; the initial
enum has four. The CSV *is* the deliverable for the keyword list — someone
pastes it into Google Ads Editor — so this is a missing product surface, not a
tidy-up.

**An export row could not describe an export in flight.** §12 puts generation on
the worker: the API returns `202 {job_id}` and the client polls
`GET /exports/{job_id}` for `queued|running|ready|failed`. The P0 table can only
record an export that already exists — `path` and `bytes` are NOT NULL — so
there was nowhere to put the job between accepting it and finishing it.

The row is now created at enqueue time and its id *is* the job id. Job state
lives in Postgres rather than in arq's Redis keys on purpose: `GET
/exports/{id}/download` has to answer after a redeploy, and a job record that
evaporates with a Redis flush would turn a finished export into a 404.

Removing an enum value is not something Postgres supports, so the downgrade
leaves `csv` in place and says so rather than pretending to be symmetric.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

EXPORT_STATUS = ("queued", "running", "ready", "failed")


def upgrade() -> None:
    # PG 12+ permits ADD VALUE inside a transaction; the value simply cannot be
    # *used* until this one commits. Nothing below uses it.
    op.execute("ALTER TYPE export_format ADD VALUE IF NOT EXISTS 'csv'")

    postgresql.ENUM(*EXPORT_STATUS, name="export_status").create(op.get_bind(), checkfirst=True)
    status = postgresql.ENUM(*EXPORT_STATUS, name="export_status", create_type=False)

    op.add_column(
        "export",
        sa.Column("status", status, nullable=False, server_default="queued"),
    )
    op.add_column("export", sa.Column("error", sa.Text(), nullable=True))
    op.add_column(
        "export",
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "export",
        sa.Column(
            "requested_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # A queued export has no file yet. Both columns stay NOT NULL in spirit —
    # `status='ready'` implies both are set, which `tests/test_models.py` asserts
    # — but the database cannot express that without a CHECK that would also have
    # to know about `failed`, so the invariant is held by the job, not the DDL.
    op.alter_column("export", "path", existing_type=sa.Text(), nullable=True)
    op.alter_column("export", "bytes", existing_type=sa.Integer(), nullable=True)

    # Existing rows predate the lifecycle and all describe finished files.
    op.execute("UPDATE export SET status = 'ready', ready_at = created_at WHERE path IS NOT NULL")

    op.create_index("ix_export_report_id", "export", ["report_id"])
    op.create_index("ix_export_status", "export", ["status"])


def downgrade() -> None:
    op.drop_index("ix_export_status", table_name="export")
    op.drop_index("ix_export_report_id", table_name="export")

    # Rows that never produced a file cannot satisfy the old NOT NULL columns.
    # Dropping them is the honest reversal: they describe jobs, and the old
    # schema had no concept of one.
    op.execute("DELETE FROM export WHERE path IS NULL OR bytes IS NULL")
    op.alter_column("export", "bytes", existing_type=sa.Integer(), nullable=False)
    op.alter_column("export", "path", existing_type=sa.Text(), nullable=False)

    op.drop_column("export", "requested_by")
    op.drop_column("export", "ready_at")
    op.drop_column("export", "error")
    op.drop_column("export", "status")

    postgresql.ENUM(name="export_status").drop(op.get_bind(), checkfirst=True)

    # `csv` stays in `export_format`. Postgres cannot drop an enum label, and
    # recreating the type would require rewriting every dependent column for no
    # gain — an unused label is inert.
