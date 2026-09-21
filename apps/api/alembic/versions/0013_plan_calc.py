"""`plan_calc` — the audit trail behind every number in a campaign plan.

Stage 02 global law 14: the LLM never does arithmetic. Every figure comes from a
registered `@formula` in `agent/calc/`, is persisted here with the inputs it
consumed, and is cited by `calc_evidence_ids` on the node output. A number that
cannot be resolved back to a row in this table is a number the plan may not
contain.

**Why this table arrives on its own, ahead of the rest of Stage 02.** PRD §7.2
lists `plan_calc` alongside `research_acceptance` and `campaign_plan`, and §23
assigns all three to S2-P0's `stage02_handshake` revision. But §21 also
specifies that S2-P1 — the calculation engine — is buildable in parallel with
the handshake, and `calc/derived.py` is the sole writer of this table: its
dedupe behaviour is one of S2-P1's three exit criteria and cannot be proven
against a table that does not exist.

`plan_calc` references only `run` and `evidence`, both of which predate Stage
02, so it carries no dependency on the handshake's own tables and splitting it
out is safe. **S2-P0's revision must omit `plan_calc`** — creating it twice
fails the upgrade.

`UNIQUE(plan_run_id, formula_id, inputs_hash)` is the dedupe key. The same
formula over the same inputs inside one plan run is the same answer, so the
second call reuses the row instead of writing another `derived` Evidence row
saying the same thing. It is scoped per run rather than per project on purpose:
a re-run under a new constants version must be able to record its own figure
without colliding with the plan that came before it.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "plan_calc",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("formula_id", sa.Text(), nullable=False),
        sa.Column("calc_version", sa.Text(), nullable=False),
        sa.Column(
            "inputs",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("inputs_hash", sa.Text(), nullable=False),
        sa.Column(
            "result",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "plan_run_id", "formula_id", "inputs_hash", name="uq_plan_calc_inputs"
        ),
    )
    op.create_index("ix_plan_calc_run_node", "plan_calc", ["plan_run_id", "node_id"])


def downgrade() -> None:
    op.drop_index("ix_plan_calc_run_node", table_name="plan_calc")
    op.drop_table("plan_calc")
