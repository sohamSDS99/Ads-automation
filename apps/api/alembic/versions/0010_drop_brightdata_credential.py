"""Remove the `brightdata` credential kind with the SERP source it configured.

The SERP source is gone — `connectors/serp.py` and the four evidence kinds it
wrote — so the credential that pointed at a Bright Data account has nothing
left to authenticate. `credential_kind` is a Postgres enum, and a label with no
writer is an invitation to store a row nothing can read.

**`evidence_source.serp` deliberately stays.** It is the other enum 0007 added,
and dropping it would mean deleting every evidence row already gathered from a
live result page — rows that reports cite by id. A source that can no longer
be *written* is a different thing from one that was never *true*, and this
product's whole claim is that a citation resolves. So the label survives with a
comment in `db.models.EvidenceSource` saying why it has no writer.

Postgres cannot drop an enum label, so the type is rebuilt. The DELETE comes
first because a row holding the dropped label could not survive the cast in any
case; it is a no-op on any deployment that never stored a Bright Data key.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | None = None
depends_on: str | None = None

#: Every label `credential_kind` carries *after* this migration, in the order
#: `db.models` declares them.
CREDENTIAL_KIND = ("openrouter", "google_ads", "dataforseo", "webshare", "smtp")


def upgrade() -> None:
    op.execute("DELETE FROM credential WHERE kind = 'brightdata'")
    _rebuild(CREDENTIAL_KIND)


def downgrade() -> None:
    # Restored where 0007 put it: before `smtp`, which is last in `db.models`.
    op.execute("ALTER TYPE credential_kind ADD VALUE IF NOT EXISTS 'brightdata' BEFORE 'webshare'")


def _rebuild(labels: tuple[str, ...]) -> None:
    """Recreate `credential_kind` with exactly `labels`, recasting the column."""
    kept = ", ".join(f"'{label}'" for label in labels)
    op.execute("ALTER TYPE credential_kind RENAME TO credential_kind_old")
    op.execute(f"CREATE TYPE credential_kind AS ENUM ({kept})")
    op.execute(
        "ALTER TABLE credential ALTER COLUMN kind "
        "TYPE credential_kind USING kind::text::credential_kind"
    )
    op.execute("DROP TYPE credential_kind_old")
