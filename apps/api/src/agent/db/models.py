"""SQLAlchemy 2.0 models — the whole schema from PRD §6.

Fifteen tables, many workspaces. A workspace is one company, or one business
function inside one; `invite`, `audit_log`, `project`, `credential`, `run` and
`schedule` all hang off it, and every query reaches them through
`WorkspaceScopedRepo` (see `repo.py`).

`user` is the exception, and the reason for `membership`. A person is one
account with one password however many workspaces they work in, so the account
is global and its *access* is per-workspace: `membership` carries the role and
whether that access is live. Two consequences worth stating once, because the
rest of the codebase depends on both:

* Authorization is a property of the (user, active workspace) pair, never of
  the user alone. `Principal.role` is the membership's role, resolved per
  request, so revoking access in one workspace cannot leak into another.
* `user.is_superadmin` is the one platform-wide grant. It reaches every
  workspace without a membership row, which is exactly why every use of it is
  written to the audit log of the workspace it touched.
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
#: and the column narrowed to match it in migration 0003.
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
    WEBSHARE = "webshare"
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
    #: Kept after the SERP source was removed. No connector writes it any
    #: more, but evidence already stored carries it, and a citation that
    #: cannot name where it came from is worse than a label with no writer.
    SERP = "serp"
    WEB = "web"
    CSV = "csv"
    DERIVED = "derived"
    #: A document a person uploaded as business context (migration 0008). Kept
    #: apart from `CSV`, which means a mapped CRM export and nothing else: these
    #: two arrive through the same kind of form and answer completely different
    #: questions, and an evidence filter that conflated them would show a
    #: pricing PDF under "closed-won deals".
    UPLOAD = "upload"


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
    """The five deliverables of PRD §12. `csv` arrived in migration 0004."""

    PDF = "pdf"
    DOCX = "docx"
    MD = "md"
    JSON = "json"
    CSV = "csv"


class ExportStatus(StrEnum):
    """The lifecycle `GET /exports/{job_id}` reports (PRD §12)."""

    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"


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
    """One company, or one business function inside one. Archived, never deleted.

    There used to be exactly one row, held down by a unique index on a constant
    expression. That index is gone (migration 0011): the product separates a
    company's research from every other company's, and separates one business
    function's from the next, and both of those are this row.

    Archiving rather than deleting is not squeamishness. A workspace owns
    projects, runs, evidence and an audit log, all of it `ON DELETE CASCADE`;
    "remove this workspace" must not be one mis-click away from erasing the
    record of everything that was ever decided in it.
    """

    __tablename__ = "workspace"
    __table_args__ = (
        # Two workspaces called "Paid Search" is a mistake every time, and the
        # switcher cannot tell them apart. Case-insensitive because `CITEXT`
        # is what the rest of this schema uses for names people type.
        sa.Index("uq_workspace_name", sa.text("lower(name)"), unique=True),
    )

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    #: NULL for the workspace the bootstrap created, which predates any account.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    #: Set to hide the workspace from every switcher and refuse new sessions
    #: into it. The rows underneath are untouched.
    archived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )

    @property
    def is_archived(self) -> bool:
        return self.archived_at is not None


class User(Base):
    """An account. Global, one password, disabled but never deleted (PRD §6.1).

    Deliberately carries no role and no workspace. Which workspaces this person
    reaches, and what they may do in each, is `membership` — see the module
    docstring. What is left here is the identity: who they are, how they prove
    it, and the one platform-wide grant.
    """

    __tablename__ = "user"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(CITEXT, nullable=False, unique=True)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    # Argon2id. Never returned by any endpoint.
    password_hash: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    #: The *account* lifecycle, which is not the same question as membership
    #: status. `invited` means no password has ever been set; `disabled` locks
    #: the person out of every workspace at once, however many memberships
    #: still say `active`.
    status: Mapped[UserStatus] = mapped_column(
        _enum(UserStatus, "user_status"), nullable=False, server_default=UserStatus.INVITED.value
    )
    #: The whole-system administrator. Reaches every workspace, with or without
    #: a membership, and is the only holder of `Permission.PLATFORM_ADMIN`.
    is_superadmin: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    #: Where to put them on their next sign-in. A hint for choosing the active
    #: workspace, never a grant: the membership is re-checked regardless.
    last_workspace_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="SET NULL"), nullable=True
    )
    last_login_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), onupdate=_now(), nullable=False
    )


class Membership(Base):
    """One person's access to one workspace, and the role they hold in it.

    The join table is where authorization lives now. `role` is read from here
    on every authenticated request, so a demotion or a revocation takes effect
    on the caller's next call rather than whenever their session is rebuilt —
    the same guarantee `user.role` used to give, kept while the account itself
    became global.

    `status` mirrors `user.status` in spelling and means something narrower:
    `invited` is an unaccepted invitation to *this* workspace (the account may
    be years old and active elsewhere), and `disabled` removes this workspace
    from that person's switcher without touching their account.
    """

    __tablename__ = "membership"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_membership_workspace_user"),
        # "Which workspaces can I reach?" runs on every sign-in and every
        # render of the switcher.
        sa.Index("ix_membership_user", "user_id"),
        sa.Index("ix_membership_workspace_role", "workspace_id", "role"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[UserRole] = mapped_column(_enum(UserRole, "user_role"), nullable=False)
    status: Mapped[UserStatus] = mapped_column(
        _enum(UserStatus, "user_status"), nullable=False, server_default=UserStatus.INVITED.value
    )
    #: NULL for the founding admin of a workspace, who was not invited by anyone.
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
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


class ProjectDocument(Base):
    """A business-context file someone uploaded, as text.

    The row is the *record* of the upload; the readable content lives twice, on
    purpose. `text` here is what the screen shows and what a re-chunk would
    start from, and the passages in `evidence` are what a node may cite. One
    without the other gives you either a library nothing reads or evidence rows
    whose provenance nobody can inspect.

    The original bytes are not kept. Railway attaches the Volume to `worker`
    alone (PRD §5.2), so storing them from `api` would mean a cross-service hop
    on every upload to hold a file whose only use — producing this text — has
    already happened.
    """

    __tablename__ = "project_document"
    __table_args__ = (
        # The same file twice is a mistake, not an update. Uploading it again
        # answers 409 with the existing row rather than doubling every passage
        # of it in the evidence the nodes read.
        sa.UniqueConstraint("project_id", "sha256", name="uq_project_document_sha"),
    )

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    uploaded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(sa.Text, nullable=False)
    media_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    byte_size: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: The extracted text, already truncated to `document_max_chars`.
    text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    char_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    passage_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    #: "page", "row", "paragraph" — whatever the format counts in — and how many
    #: of them the file held before any budget was applied.
    unit: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    unit_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default="0")
    #: What the extractor could not do: pages with no text layer, a file cut off
    #: at the character budget. Shown next to the file, never swallowed.
    warnings: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
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
    __table_args__ = (
        sa.Index("ix_run_project_started", "project_id", sa.text("started_at DESC")),
        # The reaper's sweep has no project to narrow by, so the composite index
        # above cannot serve it (PRD §16, "Worker killed").
        sa.Index("ix_run_status", "status"),
    )

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
    # setup wizard has no run to belong to (PRD §9.5, migration 0003).
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
    __table_args__ = (
        sa.Index("ix_approval_status_role", "status", "required_role"),
        # One *open* question per gate per run (migration 0004). Partial, so a
        # gate that was rejected and re-run keeps its history alongside the new
        # pending row.
        sa.Index(
            "uq_approval_pending_per_gate",
            "run_id",
            "node_id",
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
        ),
    )

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
    #: Which SLA milestones have already been nudged for — `["half"]`, then
    #: `["due", "half"]`. On the row rather than in Redis because a reminder
    #: re-sent after a cache flush reads to the recipient as a broken system,
    #: and because "has this been chased?" is a fact about the approval.
    reminders_sent: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
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
    """One export job, and — once it finishes — the artifact it produced.

    The row is written when the API accepts the request, not when the file
    exists, so `id` doubles as the job id the client polls (PRD §12). `path` is
    a storage key, never a filesystem path; nothing outside `agent.storage`
    turns it into one.
    """

    __tablename__ = "export"

    id: Mapped[uuid.UUID] = _pk()
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("report.id", ondelete="CASCADE"), nullable=False
    )
    format: Mapped[ExportFormat] = mapped_column(
        _enum(ExportFormat, "export_format"), nullable=False
    )
    status: Mapped[ExportStatus] = mapped_column(
        _enum(ExportStatus, "export_status"),
        nullable=False,
        server_default=ExportStatus.QUEUED.value,
    )
    #: Set only once `status` reaches `ready`.
    path: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    bytes: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    #: The failure, in words a person can act on. Set only when `status='failed'`.
    error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    ready_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    #: Who asked. Nullable because a scheduled export (P8) has no human behind it,
    #: and because a user may be deleted long after their export was generated.
    requested_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )

    __table_args__ = (
        sa.Index("ix_export_report_id", "report_id"),
        sa.Index("ix_export_status", "status"),
    )

    @property
    def is_downloadable(self) -> bool:
        """A `ready` row always has a file behind it. Anything else does not."""
        return self.status is ExportStatus.READY and bool(self.path)


class Schedule(Base):
    """A recurring run. Polled once a minute by the worker's arq cron (PRD §5.2)."""

    __tablename__ = "schedule"
    __table_args__ = (
        # Partial: a disabled schedule is never an answer to "what is due?", so
        # it has no business in the index the poller hits sixty times an hour.
        sa.Index("ix_schedule_due", "next_at", postgresql_where=sa.text("enabled")),
    )

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
    "membership",
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
    "project_document",
)
