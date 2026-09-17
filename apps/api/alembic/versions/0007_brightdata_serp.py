"""A SERP source: the `brightdata` credential kind and the `serp` evidence source.

Two enum values, and the reason they are two rather than one.

**`credential_kind.brightdata`.** The Bright Data SERP proxy is a secret the
workspace owns, so it belongs in the same vault as the OpenRouter key and the
Google Ads refresh token rather than in the environment. PRD §18 law 9 keeps
deployment differences in env vars; an account anyone on the team can rotate
from `/settings` is not a deployment difference.

**`evidence_source.serp`.** A live Google result page is not the keyword
vendor's cached one and not the Transparency archive, and the evidence explorer
has to be able to say which of the three a fact came from. Folding it into
`web` would make "we saw this ad on the SERP on Tuesday" indistinguishable from
"we crawled this page", which is the distinction the citation is for.

`ADD VALUE` runs inside alembic's transaction happily on PG12+ as long as the
new label is not *used* in the same transaction. Nothing here uses it.

The downgrade deletes the rows that can only exist because of this migration.
Postgres cannot drop an enum label, so the type is rebuilt — and a row holding
a label the rebuilt type does not have could not survive the cast in any case.

Revision ID: 0007
Revises: 0006
"""

from __future__ import annotations

from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | None = None
depends_on: str | None = None

#: Every label the type carries *after* this migration, in declaration order —
#: which is also the order `db.models` declares them in, so the two files can be
#: read against each other.
CREDENTIAL_KIND = ("openrouter", "google_ads", "dataforseo", "brightdata", "smtp")
EVIDENCE_SOURCE = ("google_ads", "dataforseo", "transparency", "serp", "web", "csv", "derived")


def upgrade() -> None:
    op.execute("ALTER TYPE credential_kind ADD VALUE IF NOT EXISTS 'brightdata' BEFORE 'smtp'")
    op.execute("ALTER TYPE evidence_source ADD VALUE IF NOT EXISTS 'serp' AFTER 'transparency'")


def downgrade() -> None:
    op.execute("DELETE FROM credential WHERE kind = 'brightdata'")
    op.execute("DELETE FROM evidence WHERE source = 'serp'")
    _rebuild("credential_kind", "credential", "kind", CREDENTIAL_KIND, drop="brightdata")
    _rebuild("evidence_source", "evidence", "source", EVIDENCE_SOURCE, drop="serp")


def _rebuild(
    type_name: str, table: str, column: str, labels: tuple[str, ...], *, drop: str
) -> None:
    """Recreate an enum type without one of its labels, recasting the column."""
    kept = ", ".join(f"'{label}'" for label in labels if label != drop)
    op.execute(f"ALTER TYPE {type_name} RENAME TO {type_name}_old")
    op.execute(f"CREATE TYPE {type_name} AS ENUM ({kept})")
    op.execute(
        f"ALTER TABLE {table} ALTER COLUMN {column} "
        f"TYPE {type_name} USING {column}::text::{type_name}"
    )
    op.execute(f"DROP TYPE {type_name}_old")
