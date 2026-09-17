"""A crawl transport: the `webshare` credential kind.

One enum value, and no evidence source to go with it — which is the whole point
of the migration being this small.

`credential_kind.webshare` is an API key that resolves to a proxy pair, and a
proxy is not a source of facts. Nothing new arrives in the evidence store
because of it: `web_crawler` already writes `evidence_source.web`, and it goes
on writing exactly that whether the fetch left from this machine or from one of
a thousand rotating exits. A `webshare` evidence source would be a claim about
*how* a page was reached, and a citation is a claim about what the page said.

The kind is placed before `smtp` for the same reason `brightdata` was: `smtp`
is last in `db.models.CredentialKind` and staying last keeps the declaration
order in that file and the label order in this type readable against each other.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | None = None
depends_on: str | None = None

#: Every label `credential_kind` carries *after* this migration, in the order
#: `db.models` declares them.
CREDENTIAL_KIND = (
    "openrouter",
    "google_ads",
    "dataforseo",
    "brightdata",
    "webshare",
    "smtp",
)


def upgrade() -> None:
    op.execute("ALTER TYPE credential_kind ADD VALUE IF NOT EXISTS 'webshare' BEFORE 'smtp'")


def downgrade() -> None:
    op.execute("DELETE FROM credential WHERE kind = 'webshare'")
    # Postgres cannot drop an enum label, so the type is rebuilt without it.
    # Same shape as 0007; a row holding the dropped label could not survive the
    # cast anyway, which is why the DELETE comes first.
    kept = ", ".join(f"'{label}'" for label in CREDENTIAL_KIND if label != "webshare")
    op.execute("ALTER TYPE credential_kind RENAME TO credential_kind_old")
    op.execute(f"CREATE TYPE credential_kind AS ENUM ({kept})")
    op.execute(
        "ALTER TABLE credential ALTER COLUMN kind "
        "TYPE credential_kind USING kind::text::credential_kind"
    )
    op.execute("DROP TYPE credential_kind_old")
