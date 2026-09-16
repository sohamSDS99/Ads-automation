"""At most one pending Approval per gate per run.

P3 turns on the gate machinery of PRD §7.2 item 5, and the invariant it rests on
is that a gate has exactly one open question at a time. Without it, a retried
run, a redelivered arq job or two workers racing on the same run could each
write an `Approval` for node 1.1.5, and the approver's inbox would show two
identical cards whose decisions could disagree.

It is a **partial** unique index rather than a plain unique constraint, and the
predicate is what makes it usable: a gate that was rejected and then re-run must
be able to hold its old `rejected` row and a new `pending` one at the same time.
Uniqueness is over open questions, not over history.

Nothing else changes. The `approval` table itself was created in 0001.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | None = None
depends_on: str | None = None

INDEX = "uq_approval_pending_per_gate"


def upgrade() -> None:
    op.create_index(
        INDEX,
        "approval",
        ["run_id", "node_id"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX, table_name="approval")
