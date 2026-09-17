"""P8 hardening: reminder bookkeeping, and the indexes the cron jobs poll on.

Three changes, each paying for a job that now runs every minute.

**`approval.reminders_sent`.** PRD §16 wants a reminder at 50% and at 100% of a
gate's SLA, and each of those must fire exactly once for the lifetime of the
question. The record has to be durable and has to travel with the approval — a
counter in Redis would replay every milestone after a cache flush, and the
recipient reads a duplicate nudge as the system being broken rather than as
infrastructure having been restarted.

**`ix_schedule_due`.** The poller asks "which enabled schedules are due?" sixty
times an hour. Partial on `enabled`, because a disabled row is never an answer
and there is no reason to carry it in the index.

**`ix_run_status`.** The reaper asks "which runs claim to be running?" just as
often. `ix_run_project_started` cannot serve it: that index leads on
`project_id`, and this query has no project.

`approval.due_at` needs no index of its own — the reaper's and the reminder
job's candidate sets are both bounded by `status = 'pending'`, which
`ix_approval_status_role` already leads on.

Revision ID: 0006
Revises: 0005
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "approval",
        sa.Column(
            "reminders_sent",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )

    op.create_index(
        "ix_schedule_due",
        "schedule",
        ["next_at"],
        postgresql_where=sa.text("enabled"),
    )
    op.create_index("ix_run_status", "run", ["status"])


def downgrade() -> None:
    op.drop_index("ix_run_status", table_name="run")
    op.drop_index("ix_schedule_due", table_name="schedule")
    op.drop_column("approval", "reminders_sent")
