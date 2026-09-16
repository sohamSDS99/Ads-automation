"""Initial schema — the thirteen tables of PRD §6.

Revision ID: 0001
Revises:
Create Date: 2026-09-16
"""

from __future__ import annotations

from collections.abc import Sequence

import pgvector.sqlalchemy
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

EMBEDDING_DIM = 1536

# Enum values are spelled out here rather than imported from the models: a
# migration is a frozen snapshot, and must not change when the models do.
ENUMS: dict[str, tuple[str, ...]] = {
    "user_role": ("admin", "operator", "approver", "viewer"),
    "user_status": ("invited", "active", "disabled"),
    "credential_scope": ("workspace", "project", "user"),
    "credential_kind": ("openrouter", "google_ads", "dataforseo", "smtp"),
    "run_trigger": ("manual", "schedule"),
    "run_status": (
        "queued",
        "running",
        "awaiting_approval",
        "succeeded",
        "failed",
        "cancelled",
    ),
    "run_mode": ("full", "partial"),
    "node_run_status": (
        "queued",
        "running",
        "awaiting_approval",
        "succeeded",
        "failed",
        "skipped",
    ),
    "evidence_source": (
        "google_ads",
        "dataforseo",
        "transparency",
        "web",
        "csv",
        "derived",
    ),
    "approval_status": ("pending", "approved", "rejected", "expired"),
    "approval_required_role": ("admin", "approver"),
    "export_format": ("pdf", "docx", "md", "json"),
}


def _enum(name: str) -> postgresql.ENUM:
    """Reference an already-created Postgres enum type."""
    return postgresql.ENUM(*ENUMS[name], name=name, create_type=False)


def _uuid_pk() -> sa.Column[sa.Uuid]:
    return sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False)


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column[sa.DateTime]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        nullable=nullable,
        server_default=sa.text("now()") if default_now else None,
    )


def upgrade() -> None:
    # --- extensions --------------------------------------------------------
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS citext")

    # --- enum types --------------------------------------------------------
    for name, values in ENUMS.items():
        rendered = ", ".join(f"'{value}'" for value in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")

    # --- workspace ---------------------------------------------------------
    op.create_table(
        "workspace",
        _uuid_pk(),
        sa.Column("name", sa.Text(), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    # Exactly one workspace, forever. A unique index on a constant expression
    # is the cheapest way to say "at most one row" in Postgres.
    op.execute("CREATE UNIQUE INDEX uq_workspace_singleton ON workspace ((true))")

    # --- user --------------------------------------------------------------
    op.create_table(
        "user",
        _uuid_pk(),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False, unique=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("role", _enum("user_role"), nullable=False),
        sa.Column("status", _enum("user_status"), nullable=False, server_default="invited"),
        _ts("last_login_at"),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
    )
    op.create_index("ix_user_workspace_email", "user", ["workspace_id", "email"])

    # --- invite ------------------------------------------------------------
    op.create_table(
        "invite",
        _uuid_pk(),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("role", _enum("user_role"), nullable=False),
        sa.Column("token_hash", sa.Text(), nullable=False),
        sa.Column(
            "invited_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _ts("expires_at", nullable=False),
        _ts("accepted_at"),
        _ts("created_at", nullable=False, default_now=True),
    )
    op.create_index(
        "uq_invite_open_email",
        "invite",
        ["workspace_id", "email"],
        unique=True,
        postgresql_where=sa.text("accepted_at IS NULL"),
    )

    # --- audit_log ---------------------------------------------------------
    op.create_table(
        "audit_log",
        _uuid_pk(),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "actor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=False),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("ip", postgresql.INET(), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
    )
    op.execute(
        "CREATE INDEX ix_audit_log_workspace_created ON audit_log (workspace_id, created_at DESC)"
    )

    # --- project -----------------------------------------------------------
    op.create_table(
        "project",
        _uuid_pk(),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("domain", sa.Text(), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        sa.Column(
            "product_context",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "markets",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "settings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # --- credential --------------------------------------------------------
    op.create_table(
        "credential",
        _uuid_pk(),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("scope", _enum("credential_scope"), nullable=False),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", _enum("credential_kind"), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column(
            "meta",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        _ts("created_at", nullable=False, default_now=True),
        _ts("last_tested_at"),
        sa.Column("last_test_ok", sa.Boolean(), nullable=True),
        sa.CheckConstraint(
            "(scope = 'workspace' AND project_id IS NULL AND user_id IS NULL)"
            " OR (scope = 'project' AND project_id IS NOT NULL AND user_id IS NULL)"
            " OR (scope = 'user' AND user_id IS NOT NULL AND project_id IS NULL)",
            name="ck_credential_scope_target",
        ),
    )

    # --- run ---------------------------------------------------------------
    op.create_table(
        "run",
        _uuid_pk(),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "triggered_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("trigger", _enum("run_trigger"), nullable=False),
        sa.Column("status", _enum("run_status"), nullable=False, server_default="queued"),
        sa.Column("mode", _enum("run_mode"), nullable=False, server_default="full"),
        sa.Column("node_filter", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("token_in", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("token_out", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "parent_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.execute("CREATE INDEX ix_run_project_started ON run (project_id, started_at DESC)")

    # --- node_run ----------------------------------------------------------
    op.create_table(
        "node_run",
        _uuid_pk(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("status", _enum("node_run_status"), nullable=False, server_default="queued"),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("1")),
        sa.Column("input_hash", sa.Text(), nullable=True),
        sa.Column("output", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "evidence_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=False,
            server_default=sa.text("'{}'::uuid[]"),
        ),
        sa.Column("model", sa.Text(), nullable=True),
        sa.Column("token_in", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("token_out", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")),
        sa.Column("latency_ms", sa.Integer(), nullable=True),
        _ts("started_at"),
        _ts("finished_at"),
        sa.Column("error", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.UniqueConstraint("run_id", "node_id", "attempt", name="uq_node_run_attempt"),
    )
    op.create_index("ix_node_run_run_node", "node_run", ["run_id", "node_id"])

    # --- evidence ----------------------------------------------------------
    op.create_table(
        "evidence",
        _uuid_pk(),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("source", _enum("evidence_source"), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        _ts("fetched_at", nullable=False, default_now=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("content_text", sa.Text(), nullable=True),
        sa.Column("embedding", pgvector.sqlalchemy.Vector(EMBEDDING_DIM), nullable=True),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.UniqueConstraint("project_id", "hash", name="uq_evidence_project_hash"),
    )
    op.create_index("ix_evidence_project_source_kind", "evidence", ["project_id", "source", "kind"])
    op.execute(
        "CREATE INDEX ix_evidence_embedding_hnsw ON evidence "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    # --- approval ----------------------------------------------------------
    op.create_table(
        "approval",
        _uuid_pk(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("status", _enum("approval_status"), nullable=False, server_default="pending"),
        sa.Column("required_role", _enum("approval_required_role"), nullable=False),
        sa.Column(
            "assignee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "proposal",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("edited_proposal", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "decided_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("decided_at"),
        _ts("due_at"),
        _ts("created_at", nullable=False, default_now=True),
    )
    op.create_index("ix_approval_status_role", "approval", ["status", "required_role"])

    # --- report ------------------------------------------------------------
    op.create_table(
        "report",
        _uuid_pk(),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("markdown", sa.Text(), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
    )

    # --- export ------------------------------------------------------------
    op.create_table(
        "export",
        _uuid_pk(),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("format", _enum("export_format"), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
    )

    # --- schedule ----------------------------------------------------------
    op.create_table(
        "schedule",
        _uuid_pk(),
        sa.Column(
            "workspace_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("workspace.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "project_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("project.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("cron", sa.Text(), nullable=False),
        sa.Column("timezone", sa.Text(), nullable=False, server_default="UTC"),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "last_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="SET NULL"),
            nullable=True,
        ),
        _ts("next_at"),
    )

    # `audit_log` is append-only by contract (PRD §6). The grant that enforces
    # it belongs with the least-privilege application role, which is P8.


def downgrade() -> None:
    for table in (
        "schedule",
        "export",
        "report",
        "approval",
        "evidence",
        "node_run",
        "run",
        "credential",
        "project",
        "audit_log",
        "invite",
        "user",
        "workspace",
    ):
        op.drop_table(table)
    for name in ENUMS:
        op.execute(f"DROP TYPE IF EXISTS {name}")
