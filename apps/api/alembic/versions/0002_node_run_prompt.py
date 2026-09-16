"""node_run.prompt — the prompt as sent, for GET /runs/{id}/nodes/{node_id}.

PRD §14 specifies that endpoint returns "output, evidence, prompt, metrics", but
the §6 `NodeRun` column list has no prompt. Re-rendering it on read would be a
lie the moment a repair pass or a model substitution changed what was actually
sent — and those are exactly the runs somebody is inspecting. So it is stored.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("node_run", sa.Column("prompt", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("node_run", "prompt")
