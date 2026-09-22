"""Stage 03's four new enum labels, and nothing else.

`ALTER TYPE ... ADD VALUE` is its own revision on purpose. Postgres will not
let a transaction add an enum label and then write a row using it, so an
`ALTER TYPE` living in the same revision as the tables that default to the new
value fails at `upgrade head` on an empty database — which is the worst place
to find out, because it is also Railway's `preDeployCommand`.

Two revisions, strict order, and the block below runs outside the migration's
transaction so the labels are durable before 0016 begins.

`IF NOT EXISTS` makes each statement idempotent: a half-applied upgrade that is
retried must not fail on the label it already added.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

from alembic import op

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | None = None
depends_on: str | None = None

#: (type, label). Order within a type is the order the labels were conceived;
#: Postgres appends, and nothing reads an enum positionally.
NEW_LABELS: tuple[tuple[str, str], ...] = (
    ("run_stage", "guideline"),
    ("run_status", "awaiting_human_task"),
    ("export_artifact_type", "content_guideline"),
    ("export_format", "ruleset_json"),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for type_name, label in NEW_LABELS:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    raise NotImplementedError(
        "Postgres cannot drop an enum label, so this revision is irreversible. "
        "Undoing it means recreating each of "
        f"{', '.join(sorted({t for t, _ in NEW_LABELS}))} without the Stage 03 "
        "labels, rewriting every column that uses them, and dropping the old "
        "types — a data migration, not a downgrade. Downgrade 0016 instead: it "
        "removes everything that reads these labels, which leaves them unused "
        "and harmless."
    )
