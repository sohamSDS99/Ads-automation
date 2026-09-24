"""A creative run keeps the `CreativeInput` it was started from.

Stage 04 PRD §4.3 rule 1: `CreativeInput` is "assembled once at run start …
hashed into `Run.input_hash`, passed read-only to every node". S4-P0 assembled
and hashed it in the api, then discarded it: the worker that executes the run
had nothing to pass, and could not rebuild it — the scope and the media-model
choices are request parameters, and the offer snapshot, the references and the
sign-off matrix are live rows that move after start.

`creative_input` is that missing place, written in the same INSERT as the run
row. The executor re-hashes it against `input_hash` before the first node, so
a row edited after start fails the run instead of feeding it.

Nullable, and only a creative run may carry one (`ck_run_creative_input_only_
creative`); it must be a JSON object, not the jsonb scalar `null`, for the
reason `Run.bindings` gives. Creative runs started before this revision keep
NULL and fail at execution with `creative_input_missing` — there is no input to
backfill them from.

Revision ID: 0021
Revises: 0020
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0021"
down_revision: str | None = "0020"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "run",
        sa.Column("creative_input", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.create_check_constraint(
        "ck_run_creative_input_only_creative",
        "run",
        "creative_input IS NULL OR (stage = 'creative' AND jsonb_typeof(creative_input) = 'object')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_run_creative_input_only_creative", "run", type_="check")
    op.drop_column("run", "creative_input")
