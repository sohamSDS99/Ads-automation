"""A connection can now carry the consent a person gave it.

Google Ads is the source that never fitted the "every key is the deployment's"
model. Three of its five values are the deployment's — the developer token and
the OAuth client — but the refresh token and the customer id are a *person's*,
minted by their consent against the account they can actually see. There was
nowhere to put them, so the only way to connect Google Ads was for an operator
to run a script on a laptop and paste its output into Railway.

These four columns are that missing place. The refresh token is AES-256-GCM
sealed with the same key the retired vault used, bound to the connection's id;
the showable half of a grant (the chosen account, the manager it is reached
through, every account the consent can see) goes in the `meta` column that is
already there.

All four are nullable, and every existing row is a source whose credential is
entirely the deployment's. Nothing is backfilled and nothing changes meaning: a
row with no grant resolves from the environment exactly as it did before.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "source_connection", sa.Column("grant_ciphertext", sa.LargeBinary(), nullable=True)
    )
    op.add_column("source_connection", sa.Column("grant_nonce", sa.LargeBinary(), nullable=True))
    op.add_column(
        "source_connection",
        sa.Column("granted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "source_connection",
        sa.Column("granted_by", sa.UUID(as_uuid=True), nullable=True),
    )
    # SET NULL, not RESTRICT: a grant outlives the person who gave it, and
    # removing a colleague from the workspace must not be blocked by a
    # connection they happened to sign in to.
    op.create_foreign_key(
        "fk_source_connection_granted_by_user",
        "source_connection",
        "user",
        ["granted_by"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_source_connection_granted_by_user", "source_connection", type_="foreignkey"
    )
    for column in ("granted_by", "granted_at", "grant_nonce", "grant_ciphertext"):
        op.drop_column("source_connection", column)
