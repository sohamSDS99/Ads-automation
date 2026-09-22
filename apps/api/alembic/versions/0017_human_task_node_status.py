"""A node can now stop for a named person, not only for a role.

`node_run_status` gains `awaiting_human_task`. Its own revision, and outside the
migration's transaction, for the reason 0015 spells out: Postgres will not let a
transaction add an enum label and then write a row using it.

`run_status.awaiting_human_task` already exists — 0015 added it, because S3-P0
wrote the run-level status before anything used it. This is the node-level twin.

Revision ID: 0017
Revises: 0016
"""

from __future__ import annotations

from alembic import op

revision: str = "0017"
down_revision: str | None = "0016"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE node_run_status ADD VALUE IF NOT EXISTS 'awaiting_human_task'")


def downgrade() -> None:
    raise NotImplementedError(
        "Postgres cannot drop an enum label, so this revision is irreversible. The "
        "label is inert once nothing writes it; downgrade the revision that added "
        "the person-task nodes instead."
    )
