"""A workspace switches a source on; the deployment supplies the key.

`source_connection` is the whole of what a workspace now owns about a source:
one row means "use this", no row means "do not". The credential itself is read
from the environment (`credential_kinds.KindSpec.from_env`), so there is no
longer anywhere in this product that a person types a key and nowhere that a
copy of one is stored per workspace.

**The `credential` table is deliberately left in place.** Its rows are
AES-256-GCM ciphertext that no endpoint ever returned, so dropping it here
would destroy the only copy of any secret an operator had not also written into
their environment — a Google Ads refresh token above all, which the retired
consent flow minted straight into that table and showed nobody.
`scripts/vault-to-env.py` prints those rows as the `.env` lines that replace
them. Drop the table in its own migration once that has been run.

The seed below is why an upgrade is not a regression: every workspace/kind pair
that had a stored credential is switched on, so a deployment that was working
before this change is working after it as soon as its keys are in the
environment — no clicking through the Connections screen to restore a state
somebody already chose.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "source_connection",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "kind",
            postgresql.ENUM(name="credential_kind", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "connected_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "connected_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("last_tested_at", sa.DateTime(timezone=True)),
        sa.Column("last_test_ok", sa.Boolean()),
        sa.Column("last_test_detail", sa.Text()),
        sa.Column(
            "meta",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.UniqueConstraint("workspace_id", "kind", name="uq_source_connection_workspace_kind"),
    )

    # Carry every existing decision forward. `DISTINCT ON` collapses the three
    # scopes a kind could be stored at — a personal OpenRouter key and the
    # workspace one were two rows and are one decision — and takes the oldest,
    # so the connection is attributed to whoever first connected the source
    # rather than to whoever most recently replaced its key.
    #
    # `meta` is not carried across. The vault's hints describe the vault's key —
    # a last-4 of a string that is no longer the one being used — and a hint
    # that names the wrong key is worse than no hint. The next test fills it in
    # from the environment's own values.
    op.execute(
        """
        INSERT INTO source_connection
            (id, workspace_id, kind, connected_by, connected_at,
             last_tested_at, last_test_ok)
        SELECT DISTINCT ON (workspace_id, kind)
            gen_random_uuid(), workspace_id, kind, created_by, created_at,
            last_tested_at, last_test_ok
        FROM credential
        WHERE kind <> 'smtp'
        ORDER BY workspace_id, kind, created_at ASC
        """
    )


def downgrade() -> None:
    op.drop_table("source_connection")
