"""SQLAlchemy 2.0 models — the whole schema from PRD §6.

Thirteen tables, one workspace. The Workspace row is a singleton enforced in
the database, not in application code; `user`, `invite`, `audit_log`,
`project`, `credential`, `run` and `schedule` all hang off it, and every query
reaches them through `WorkspaceScopedRepo` (see `repo.py`).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, ENUM, INET, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

#: Width of `Evidence.embedding`, and therefore of the HNSW index built on it.
#: pgvector fixes a column's dimension at DDL time, so this is a schema constant
#: rather than configuration: changing it is a migration, not an env var.
#:
#: 384 is `BAAI/bge-small-en-v1.5`. PRD §6 wrote 1536 assuming a hosted OpenAI-
#: shaped embedder; OpenRouter turned out to serve no embedding model at all
#: (PRD §20 Q6), so the local model the PRD named as the fallback is the model,
#: and the column narrowed to match it in migration 0002.
EMBEDDING_DIM = 384


class Base(DeclarativeBase):
    """Declarative base for every table in the application."""


# ---------------------------------------------------------------------------
# Enums. The Postgres types are created explicitly by the migration, so every
# mapped column declares `create_type=False`.
# ---------------------------------------------------------------------------


class UserRole(StrEnum):
    ADMIN = "admin"
    OPERATOR = "operator"
    APPROVER = "approver"
    VIEWER = "viewer"


class UserStatus(StrEnum):
    INVITED = "invited"
    ACTIVE = "active"
    DISABLED = "disabled"


class CredentialScope(StrEnum):
    WORKSPACE = "workspace"
    PROJECT = "project"
    USER = "user"


class CredentialKind(StrEnum):
    OPENROUTER = "openrouter"
    GOOGLE_ADS = "google_ads"
    DATAFORSEO = "dataforseo"
    SMTP = "smtp"


class RunTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunMode(StrEnum):
    FULL = "full"
    PARTIAL = "partial"


class NodeRunStatus(StrEnum):
    """Per-node lifecycle.

    PRD §6 leaves `NodeRun.status` untyped; these values are the ones §7.2
    execution semantics and the §13.2 status palette actually require.
    """

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class EvidenceSource(StrEnum):
    GOOGLE_ADS = "google_ads"
    DATAFORSEO = "dataforseo"
    TRANSPARENCY = "transparency"
    WEB = "web"
    CSV = "csv"
    DERIVED = "derived"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ApprovalRequiredRole(StrEnum):
    """Who may decide a gate. A strict subset of `UserRole` (PRD §6)."""

    ADMIN = "admin"
    APPROVER = "approver"


class ExportFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    MD = "md"
    JSON = "json"


def _enum(enum_cls: type[StrEnum], name: str) -> ENUM:
    return ENUM(
        enum_cls,
        name=name,
        create_type=False,
        values_callable=lambda cls: [member.value for member in cls],
    )


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _now() -> Any:
    return sa.func.now()


# ---------------------------------------------------------------------------
# Tables
# ---------------------------------------------------------------------------


class Workspace(Base):
    """The singleton workspace. Exactly one row, enforced by a unique index."""

    __tablename__ = "workspace"
    __table_args__ = (
        # A unique index on a constant expression permits exactly one row.
        sa.Index("uq_workspace_singleton", sa.text("(true)"), unique=True),
    )

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


class User(Base):
    """A member of the workspace. Disabled, never deleted (PRD §6.1)."""

    __tablename__ = "user"
    __table_args__ = (sa.Index("ix_user_workspace_email", "workspace_id", "email"),)

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Argon2id. Never returned by any endpoint.
    password_hash: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        _enum(UserStatus, "user_status"), nullable=False, server_default=UserStatus.INVITED.value
    )
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), onupdate=_now(), nullable=False
    )


class Invite(Base):
    """A single-use, hashed invite token. One open invite per email."""

    __tablename__ = "invite"
    __table_args__ = (
        sa.Index(
            "uq_invite_open_email",
            "workspace_id",
            "email",
            unique=True,
            postgresql_where=sa.text("accepted_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(CITEXT, nullable=False)
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), nullable=False)
    # SHA-256 of the 32-byte token. The token itself is never stored.
    token_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    invited_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class AuditLog(Base):
    """Append-only. No UPDATE or DELETE grant is issued on this table."""

    __tablename__ = "audit_log"
    __table_args__ = (
        sa.Index("ix_audit_log_workspace_created", "workspace_id", sa.text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    # NULL only when the actor is the scheduler (PRD §15 NF5c).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(sa.Text, nullable=False)
    target_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    target_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    ip: Mapped[str | None] = mapped_column(INET)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class Project(Base):
    """A brand/market research target. Everything a run needs to be reproducible."""

    __tablename__ = "project"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    domain: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    # Bumped on every write; `PATCH` requires If-Unmodified-Since (PRD §6.1).
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), onupdate=_now(), nullable=False
    )
    product_context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    markets: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )


class Credential(Base):
    """An AES-256-GCM sealed secret. Resolution order at call time: user > project > workspace."""

    __tablename__ = "credential"
    __table_args__ = (
        sa.CheckConstraint(
            "(scope = 'workspace' AND project_id IS NULL AND user_id IS NULL)"
            " OR (scope = 'project' AND project_id IS NOT NULL AND user_id IS NULL)"
            " OR (scope = 'user' AND user_id IS NOT NULL AND project_id IS NULL)",
            name="ck_credential_scope_target",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    scope: Mapped[CredentialScope] = mapped_column(
        _enum(CredentialScope, "credential_scope"), nullable=False
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE")
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="CASCADE")
    )
    kind: Mapped[CredentialKind] = mapped_column(
        _enum(CredentialKind, "credential_kind"), nullable=False
    )
    ciphertext: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(sa.LargeBinary, nullable=False)
    # Masked hints only — last4, account id. Never the secret.
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(sa.Boolean)


class Run(Base):
    """One execution of the research DAG."""

    __tablename__ = "run"
    __table_args__ = (sa.Index("ix_run_project_started", "project_id", sa.text("started_at DESC")),)

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    # NULL only when trigger = 'schedule' (PRD §6).
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL")
    )
    trigger: Mapped[RunTrigger] = mapped_column(_enum(RunTrigger, "run_trigger"), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), nullable=False, server_default=RunStatus.QUEUED.value
    )
    mode: Mapped[RunMode] = mapped_column(
        _enum(RunMode, "run_mode"), nullable=False, server_default=RunMode.FULL.value
    )
    node_filter: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    cost_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")
    )
    token_in: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    token_out: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    parent_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="SET NULL")
    )


class NodeRun(Base):
    """One attempt at one node. Persisted before the executor moves on (PRD §7.2)."""

    __tablename__ = "node_run"
    __table_args__ = (
        sa.UniqueConstraint("run_id", "node_id", "attempt", name="uq_node_run_attempt"),
        sa.Index("ix_node_run_run_node", "run_id", "node_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[NodeRunStatus] = mapped_column(
        _enum(NodeRunStatus, "node_run_status"),
        nullable=False,
        server_default=NodeRunStatus.QUEUED.value,
    )
    attempt: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    input_hash: Mapped[str | None] = mapped_column(sa.Text)
    # Not in the PRD §6 column list. `GET /runs/{id}/nodes/{node_id}` is
    # specified to return the prompt (PRD §14), and after a repair pass or a
    # model substitution the prompt that was actually sent cannot be
    # re-derived from the inputs. Added in migration 0002.
    prompt: Mapped[str | None] = mapped_column(sa.Text)
    output: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'::uuid[]")
    )
    model: Mapped[str | None] = mapped_column(sa.Text)
    token_in: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    token_out: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    cost_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")
    )
    latency_ms: Mapped[int | None] = mapped_column(sa.Integer)
    started_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Evidence(Base):
    """A retrieved fact. Deduped per project by content hash, reused across runs."""

    __tablename__ = "evidence"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "hash", name="uq_evidence_project_hash"),
        sa.Index("ix_evidence_project_source_kind", "project_id", "source", "kind"),
        sa.Index(
            "ix_evidence_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    # NULL when the evidence arrived outside a run — a CSV uploaded in the
    # setup wizard has no run to belong to (PRD §9.5, migration 0002).
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=True
    )
    source: Mapped[EvidenceSource] = mapped_column(
        _enum(EvidenceSource, "evidence_source"), nullable=False
    )
    source_url: Mapped[str | None] = mapped_column(sa.Text)
    fetched_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    content_text: Mapped[str | None] = mapped_column(sa.Text)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM))
    hash: Mapped[str] = mapped_column(sa.Text, nullable=False)


class Approval(Base):
    """A halted gate awaiting a human decision (PRD §7.2 item 5)."""

    __tablename__ = "approval"
    __table_args__ = (sa.Index("ix_approval_status_role", "status", "required_role"),)

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[ApprovalStatus] = mapped_column(
        _enum(ApprovalStatus, "approval_status"),
        nullable=False,
        server_default=ApprovalStatus.PENDING.value,
    )
    required_role: Mapped[ApprovalRequiredRole] = mapped_column(
        _enum(ApprovalRequiredRole, "approval_required_role"), nullable=False
    )
    # NULL => any user holding `required_role` may decide.
    assignee_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL")
    )
    proposal: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    edited_proposal: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    decision_note: Mapped[str | None] = mapped_column(sa.Text)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL")
    )
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class Report(Base):
    """The synthesised Research Report. Exactly one per run."""

    __tablename__ = "report"

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    schema_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    markdown: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class Export(Base):
    """A rendered artifact on the worker's Volume.

    `path` is a storage key, never a filesystem path.
    """

    __tablename__ = "export"

    id: Mapped[uuid.UUID] = _pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("report.id", ondelete="CASCADE"), nullable=False
    )
    format: Mapped[ExportFormat] = mapped_column(
        _enum(ExportFormat, "export_format"), nullable=False
    )
    path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class Schedule(Base):
    """A recurring run. Polled once a minute by the worker's arq cron (PRD §5.2)."""

    __tablename__ = "schedule"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    created_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    cron: Mapped[str] = mapped_column(sa.Text, nullable=False)
    timezone: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="UTC")
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )
    last_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="SET NULL")
    )
    next_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


#: Every table the migration must create, in dependency order.
ALL_TABLES: tuple[str, ...] = (
    "workspace",
    "user",
    "invite",
    "audit_log",
    "project",
    "credential",
    "run",
    "node_run",
    "evidence",
    "approval",
    "report",
    "export",
    "schedule",
)
