"""Many workspaces, one account per person, and one administrator over the lot.

Three changes, one feature.

**`membership`** splits authorization off the account. `user.workspace_id` and
`user.role` said a person belonged to one workspace and held one role forever;
a company that separates its business functions into a workspace each needs the
same person in several of them, and needs their access revocable in one without
touching the others. The join table carries the role and the status; the
account keeps the identity and the password.

**The workspace singleton is gone.** `uq_workspace_singleton` was a unique
index on the constant `true`, which is the cleanest way to hold a table to one
row and exactly what this feature has to undo. `workspace` gains `archived_at`
(hide it, keep its history) and `created_by`, and a unique index on
`lower(name)` so two workspaces cannot share a name in the switcher.

**`user.is_superadmin`** is the whole-system administrator. Exactly one account
is promoted here — the oldest active admin, which on every existing
installation is the bootstrap admin — because a migration that promoted nobody
would leave an installation with no one able to create the second workspace,
and one that promoted everybody would hand every workspace admin the keys to
every other company's data.

Backfill order matters and is the reason this file is longer than its diff:
every existing `user` row becomes one `membership` row carrying the role and
status it already had, and that has to land *before* the columns it was read
from are dropped.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # --- membership ---------------------------------------------------------
    op.create_table(
        "membership",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "role",
            postgresql.ENUM(name="user_role", create_type=False),
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(name="user_status", create_type=False),
            nullable=False,
            server_default="invited",
        ),
        sa.Column(
            "invited_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_membership_workspace_user"),
    )
    op.create_index("ix_membership_user", "membership", ["user_id"])
    op.create_index("ix_membership_workspace_role", "membership", ["workspace_id", "role"])

    # One membership per existing user, carrying the role and status the
    # account already had. `created_at` is the user's, not now(): the row
    # records when that access began, and every screen that sorts by it would
    # otherwise show a whole workspace as having joined during the deploy.
    op.execute(
        """
        INSERT INTO membership (id, workspace_id, user_id, role, status, created_at, updated_at)
        SELECT gen_random_uuid(), u.workspace_id, u.id, u.role, u.status, u.created_at, u.updated_at
        FROM "user" u
        """
    )

    # --- workspace ----------------------------------------------------------
    op.execute("DROP INDEX IF EXISTS uq_workspace_singleton")
    op.add_column(
        "workspace",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "workspace",
        sa.Column("created_by", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_workspace_created_by_user",
        "workspace",
        "user",
        ["created_by"],
        ["id"],
        ondelete="SET NULL",
    )
    # Deduplicate before the unique index, or an installation that somehow
    # holds two same-named workspaces fails the deploy instead of the rename.
    op.execute(
        """
        UPDATE workspace w
        SET name = w.name || ' (' || left(w.id::text, 8) || ')'
        WHERE EXISTS (
            SELECT 1 FROM workspace other
            WHERE lower(other.name) = lower(w.name) AND other.id < w.id
        )
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_workspace_name ON workspace (lower(name))")

    # --- user ---------------------------------------------------------------
    op.add_column(
        "user",
        sa.Column("is_superadmin", sa.Boolean(), nullable=False, server_default=sa.text("false")),
    )
    op.add_column(
        "user",
        sa.Column("last_workspace_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_user_last_workspace",
        "user",
        "workspace",
        ["last_workspace_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute('UPDATE "user" SET last_workspace_id = workspace_id')

    # The oldest active admin becomes the administrator of the whole system.
    # `ORDER BY created_at, id` rather than `created_at` alone so two admins
    # created in the same transaction resolve to the same winner on every
    # replica that runs this.
    op.execute(
        """
        UPDATE "user"
        SET is_superadmin = true
        WHERE id = (
            SELECT id FROM "user"
            WHERE role = 'admin' AND status = 'active'
            ORDER BY created_at, id
            LIMIT 1
        )
        """
    )

    op.drop_index("ix_user_workspace_email", table_name="user")
    op.drop_column("user", "role")
    op.drop_column("user", "workspace_id")


def downgrade() -> None:
    """Fold each person back into one workspace and one role.

    Lossy where it has to be: someone who joined three workspaces goes back to
    the one they joined first, and the other two memberships are dropped. There
    is no non-lossy inverse of this migration, and pretending otherwise by
    picking a different workspace would only move which access disappears.
    """
    op.add_column("user", sa.Column("workspace_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.add_column(
        "user",
        sa.Column("role", postgresql.ENUM(name="user_role", create_type=False), nullable=True),
    )
    op.execute(
        """
        UPDATE "user" u
        SET workspace_id = m.workspace_id, role = m.role
        FROM (
            SELECT DISTINCT ON (user_id) user_id, workspace_id, role
            FROM membership
            ORDER BY user_id, created_at, id
        ) m
        WHERE m.user_id = u.id
        """
    )
    # A user with no membership at all cannot exist in the old shape. The only
    # way to keep the NOT NULL is to attach them to some workspace; the oldest
    # one is the least surprising choice, and there is always at least one
    # because the old schema could not have produced a user without it.
    op.execute(
        """
        UPDATE "user"
        SET workspace_id = (SELECT id FROM workspace ORDER BY created_at, id LIMIT 1),
            role = COALESCE(role, 'viewer')
        WHERE workspace_id IS NULL
        """
    )
    op.execute('DELETE FROM "user" WHERE workspace_id IS NULL')
    op.alter_column("user", "workspace_id", nullable=False)
    op.alter_column("user", "role", nullable=False)
    op.create_foreign_key(
        "user_workspace_id_fkey", "user", "workspace", ["workspace_id"], ["id"], ondelete="CASCADE"
    )
    op.create_index("ix_user_workspace_email", "user", ["workspace_id", "email"])

    op.drop_constraint("fk_user_last_workspace", "user", type_="foreignkey")
    op.drop_column("user", "last_workspace_id")
    op.drop_column("user", "is_superadmin")

    op.execute("DROP INDEX IF EXISTS uq_workspace_name")
    op.drop_constraint("fk_workspace_created_by_user", "workspace", type_="foreignkey")
    op.drop_column("workspace", "created_by")
    op.drop_column("workspace", "archived_at")

    # Back to one workspace. Everything hanging off the others goes with them,
    # which is why this direction is for a failed deploy and nothing else.
    op.execute(
        """
        DELETE FROM workspace
        WHERE id <> (SELECT id FROM workspace ORDER BY created_at, id LIMIT 1)
        """
    )
    op.execute("CREATE UNIQUE INDEX uq_workspace_singleton ON workspace ((true))")

    op.drop_index("ix_membership_workspace_role", table_name="membership")
    op.drop_index("ix_membership_user", table_name="membership")
    op.drop_table("membership")
