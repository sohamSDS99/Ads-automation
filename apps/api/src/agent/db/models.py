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
    #: A named human must perform an act the agent cannot perform for them
    #: (Stage 03 PRD §8.4). Deliberately not `awaiting_approval`: an approval
    #: is "the agent proposed and a human confirmed" and any holder of the
    #: role may confirm it. A person-task has exactly one assignee, no admin
    #: fallback, and for H1 a step-up-authenticated signature. Collapsing the
    #: two would leave the run table unable to tell them apart, and the
    #: approvals inbox offering the wrong control to the wrong person.
    AWAITING_HUMAN_TASK = "awaiting_human_task"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RunMode(StrEnum):
    FULL = "full"
    PARTIAL = "partial"


class RunStage(StrEnum):
    """Which pipeline a run executes (Stage 02 PRD §7.1).

    One `run` table, two DAGs. A plan run consumes exactly one research run and
    says so in `source_run_id`; the `CHECK` in migration 0013 makes the two
    facts inseparable, so "a plan run" and "a run with a source" are the same
    statement rather than two that can drift apart.
    """

    RESEARCH = "research"
    PLAN = "plan"
    #: Stage 03. Unlike `plan`, a guideline run has **no** upstream gate: it
    #: starts on a project that has never run research and has no frozen plan
    #: (Stage 03 law 21). `ck_run_plan_has_source` was rewritten in migration
    #: 0016 for exactly this reason — its original form also asserted that only
    #: a plan run may carry a source, which a guideline run that resolved a
    #: research binding contradicts.
    GUIDELINE = "guideline"
    #: Stage 04. Gated harder than `plan`: a creative run consumes a frozen,
    #: non-superseded plan (named in `source_run_id` — the plan's
    #: `plan_run_id`) *and* a published ruleset (pinned in `Run.pins`).
    #: `ck_run_creative_has_source` is the database half of CR-E1.
    CREATIVE = "creative"


class NodeRunStatus(StrEnum):
    """Per-node lifecycle.

    PRD §6 leaves `NodeRun.status` untyped; these values are the ones §7.2
    execution semantics and the §13.2 status palette actually require.
    """

    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    #: Halted for one named person rather than for any holder of a role. The
    #: run-level twin is `RunStatus.AWAITING_HUMAN_TASK`; keeping the two apart
    #: is what lets the inbox offer the right control to the right person.
    AWAITING_HUMAN_TASK = "awaiting_human_task"
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
    """The five deliverables of PRD §12. `csv` arrived in migration 0004.

    `editor_csv` and `xlsx` are Stage 02's two (migration 0013): the Google Ads
    Editor import sheets and the media plan as a workbook. Neither has a writer
    until S2-P5 — the values exist here because the enum and the exporters ship
    in different phases, and a format the database cannot spell is a worse
    place to discover that than a format nothing requests yet.
    """

    PDF = "pdf"
    DOCX = "docx"
    MD = "md"
    JSON = "json"
    CSV = "csv"
    EDITOR_CSV = "editor_csv"
    XLSX = "xlsx"

    #: The Stage 04 handoff (Stage 03 PRD §14). A compiled `RuleSet`, pinned by
    #: `ruleset_version`, hashed. The only export another stage reads.
    RULESET_JSON = "ruleset_json"

    #: Stage 04 (migration 0019). The Google Ads Editor bundle of a released
    #: creative package. No writer until the exports phase.
    EDITOR_ZIP = "editor_zip"


class ExportArtifactType(StrEnum):
    """What an `Export` row points at (Stage 02 PRD §7.1).

    `Export.artifact_id` deliberately carries no foreign key: two tables are
    exportable and Postgres cannot reference both from one column. This enum is
    the discriminator that says which one to look in, and `ExportRepo` is the
    only place that turns the pair back into a row.
    """

    RESEARCH_REPORT = "research_report"
    CAMPAIGN_PLAN = "campaign_plan"
    CONTENT_GUIDELINE = "content_guideline"
    CREATIVE_PACKAGE = "creative_package"


class CampaignPlanStatus(StrEnum):
    """The plan lifecycle (Stage 02 PRD §7.2).

    `blocked` is a rejected gate, not a failure: the run ended, the plan exists,
    and a human said no to part of it. `superseded` is what an older version
    becomes when a newer one is frozen.
    """

    DRAFT = "draft"
    BLOCKED = "blocked"
    READY_TO_FREEZE = "ready_to_freeze"
    FROZEN = "frozen"
    SUPERSEDED = "superseded"


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
    """Retired: the vault that used to hold a per-workspace copy of every key.

    Nothing reads or writes this table any more. Secrets are read from the
    deployment's environment (`credential_kinds.KindSpec.from_env`), a workspace's
    say is `SourceConnection` below, and the one value neither of those can hold
    — a Google refresh token — is sealed onto that row's `grant_ciphertext`.

    It is still declared, and still on disk, because the rows are AES-256-GCM
    ciphertext that no endpoint could ever read back: dropping the table would
    destroy the only copy of any secret an operator had not also written into
    their environment — a Google Ads refresh token most of all, which consent
    minted directly into here and never showed anyone. `scripts/vault-to-env.py`
    prints those rows as the `.env` lines that replace them. Drop the table
    after that has been run, in its own migration, never as a side effect of
    this change.
    """

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


class SourceConnection(Base):
    """One workspace's decision to use one source. The row *is* the decision.

    There is no `connected` column, because there is no third state: a row means
    the administrator switched this source on, and disconnecting deletes it. A
    boolean would let a workspace hold `connected = false`, which is the same
    fact as no row and a second way to write it.

    What the row carries besides the decision is the last verdict — the outcome
    of the live call that `POST /connections/{kind}/test` makes, and that
    connecting makes on the administrator's behalf. It lives here rather than in
    memory so that "this key stopped working" survives a reload, and it is
    deleted along with the connection because a disconnected source has no
    state worth keeping.

    The secret itself is nowhere near this table. It is in the environment, and
    the resolution in `credentials.resolve_values` is exactly: is there a row,
    and does the environment supply the values.
    """

    __tablename__ = "source_connection"
    __table_args__ = (
        # One decision per source per workspace. Connecting twice is the same
        # decision, not a second one, so the write is an upsert against this.
        sa.UniqueConstraint("workspace_id", "kind", name="uq_source_connection_workspace_kind"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[CredentialKind] = mapped_column(
        _enum(CredentialKind, "credential_kind"), nullable=False
    )
    connected_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    connected_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    last_tested_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_test_ok: Mapped[bool | None] = mapped_column(sa.Boolean)
    #: The sentence the upstream gave, shown next to the verdict. Never a secret:
    #: `_run_test` builds it from the connector's own status line.
    last_test_detail: Mapped[str | None] = mapped_column(sa.Text)
    #: Masked hints from the last successful test — an account name, a customer
    #: id, the last four of a key. Same rule as the retired vault's `meta`.
    #:
    #: `default` as well as `server_default`: the server default only fills the
    #: column at INSERT, so a row built in Python and written to before its
    #: first flush reads `None` here. That is exactly what `connect` does — it
    #: adds the row, then records the verdict of the test it just ran — and it
    #: raised `TypeError` on the merge until this existed.
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=sa.text("'{}'::jsonb")
    )

    #: The half of an OAuth source's credential the deployment cannot hold: the
    #: refresh token a person's consent minted, AES-256-GCM sealed exactly as
    #: the retired vault sealed everything, with this row's id as the AAD so a
    #: ciphertext moved to another workspace's row fails to open rather than
    #: quietly authorising as somebody else.
    #:
    #: Null for every source whose credential is entirely the deployment's,
    #: which is all of them but Google Ads. The non-secret half of a grant — the
    #: customer id, the manager it is reached through, the accounts it can see —
    #: is in `meta`, because it is showable and the refresh token never is.
    grant_ciphertext: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    grant_nonce: Mapped[bytes | None] = mapped_column(sa.LargeBinary)
    granted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    #: Who signed in. Not `connected_by`: the person who switched a source on
    #: and the person whose Google account it now reads are often not the same,
    #: and when a grant stops working it is the second one who has to fix it.
    #: `SET NULL` rather than `RESTRICT` — a grant outlives the person who gave
    #: it, and deleting a colleague must not be blocked by a connection.
    granted_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL")
    )


class Run(Base):
    """One execution of the research DAG."""

    __tablename__ = "run"
    __table_args__ = (
        sa.Index("ix_run_project_started", "project_id", sa.text("started_at DESC")),
        sa.Index(
            "ix_run_project_stage_started",
            "project_id",
            "stage",
            sa.text("started_at DESC"),
        ),
        # A plan run is defined by the research it consumes, so the two facts
        # are one constraint rather than two columns that can disagree.
        #
        # Stage 03 widened that equality rather than halving it. As an
        # equality it said two things at once: a plan run must name its source,
        # and *only* a plan run may have one. The first is still true. The
        # second was only ever a proxy for what was meant — a research run must
        # not claim a source, because nothing about a research run consumes
        # one — and as written it also excluded guideline runs, which do.
        #
        # Both halves are kept, under the original name, because the second one
        # is load-bearing and has its own test: dropping it would let a
        # research run carry a pointer nothing reads and every later stage
        # would have to decide what it meant.
        sa.CheckConstraint(
            "(stage <> 'plan' OR source_run_id IS NOT NULL) "
            "AND (stage <> 'research' OR source_run_id IS NULL)",
            name="ck_run_plan_has_source",
        ),
        # '{}' is a valid value: a guideline run that bound nothing is the
        # standalone mode, which is the point of the stage. NULL is not — it
        # cannot be told apart from "nobody has asked yet".
        #
        # `jsonb_typeof(...) = 'object'` is the half that is easy to leave out
        # and expensive to leave out. Without it, the JSON scalar `null` — what
        # a plain `JSONB` column stores for Python `None` — satisfies
        # `IS NOT NULL` and the constraint asserts nothing. Both halves are
        # needed: `jsonb_typeof(NULL)` is NULL, and a CHECK passes on NULL.
        sa.CheckConstraint(
            "stage <> 'guideline' OR (bindings IS NOT NULL AND jsonb_typeof(bindings) = 'object')",
            name="ck_run_guideline_has_bindings",
        ),
        # Stage 04, CR-E1 in DDL: a creative run names the frozen plan it
        # writes into. *Added* beside the two above, never folded into them —
        # rewriting `ck_run_plan_has_source` again would re-open what its
        # own test pins down.
        sa.CheckConstraint(
            "stage <> 'creative' OR source_run_id IS NOT NULL",
            name="ck_run_creative_has_source",
        ),
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
    stage: Mapped[RunStage] = mapped_column(
        _enum(RunStage, "run_stage"), nullable=False, server_default=RunStage.RESEARCH.value
    )
    #: `GuidelineBindings` for `stage='guideline'`, NULL for every other stage
    #: (`ck_run_guideline_has_bindings`). A resolved research binding is *also*
    #: written to `source_run_id`, so one column answers "what research does
    #: this run consume" for both stages; the plan binding has no such column
    #: and lives only here.
    #: `none_as_null` is load-bearing, not tidiness. Plain `JSONB` serialises
    #: Python `None` to the JSON value `null`, which is a jsonb scalar and not
    #: SQL NULL — so `bindings IS NOT NULL` is true of it and
    #: `ck_run_guideline_has_bindings` waves it through. That would leave three
    #: states where the column is meant to have two, and the third one means
    #: nothing. The CHECK additionally requires a JSON *object*, so the
    #: guarantee holds for any client, not only for this mapper.
    bindings: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    #: The research run this plan run consumes. NULL for research runs, and
    #: never NULL for plan runs — see `ck_run_plan_has_source`. Distinct from
    #: `parent_run_id`, which keeps its Stage 01 meaning: the previous run of
    #: the *same* stage.
    source_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE")
    )
    #: sha256 of the `PlanInput` this run was started from. Written at launch,
    #: never recomputed: it is what lets a later run tell whether it is looking
    #: at the same research it was planned against.
    input_hash: Mapped[str | None] = mapped_column(sa.Text)
    #: Stage 04 only: the append-only list of `{ruleset_version, reason, at}`
    #: a creative run was made under (PRD §4.4). The first entry has
    #: `reason='start'`; the only other reason is `h3_clearance`. NULL for
    #: every other stage. `none_as_null` for the reason `bindings` gives.
    pins: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB(none_as_null=True))
    #: Stage 04 only: the `CreativeInput` this run was started from, written
    #: in the run's own INSERT (migration 0021, PRD §4.3 rule 1). The executor
    #: re-hashes it against `input_hash` before the first node and hands it to
    #: every node read-only. `ck_run_creative_input_only_creative`.
    creative_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))


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
        sa.Index("ix_approval_gate_status", "gate_key", "status"),
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
    #: Which gate this is: 'G1'..'G4' for plan gates, 'R1'..'R3' for research.
    #: Rows written before migration 0013 read 'R0' — not one of the three,
    #: because labelling them would be an invention rather than a backfill.
    gate_key: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="R0")
    #: The last what-if payload shown to the approver on a budget gate. Stage
    #: 02 S2-P3 writes it; nothing reads it before then.
    recalc_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    #: A reviewer's saved, unsubmitted per-item decisions on G8/G8b (Stage 04
    #: PRD §7.1). A draft, never a decision: `AssetDecision` rows are those.
    draft_state: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
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
    artifact_type: Mapped[ExportArtifactType] = mapped_column(
        _enum(ExportArtifactType, "export_artifact_type"),
        nullable=False,
        server_default=ExportArtifactType.RESEARCH_REPORT.value,
    )
    #: The `report.id` or `campaign_plan.id` this export renders. No foreign
    #: key: the target table is `artifact_type`, and Postgres cannot reference
    #: two tables from one column. `ExportRepo` is the only resolver.
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
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
        sa.Index("ix_export_artifact", "artifact_type", "artifact_id"),
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


# ---------------------------------------------------------------------------
# Stage 02 — the campaign plan (Stage 02 PRD §7.2)
# ---------------------------------------------------------------------------


class ResearchAcceptance(Base):
    """A human read a finished research report and considers it fit to plan from.

    This is not a fourth research gate. The three gates are already decided by
    the time a report exists; acceptance is the separate, deliberate act that
    Stage 02 keys off, and the reason nothing chains automatically from a
    research run finishing (Stage 02 law 18).

    A project accumulates acceptances over time and exactly one of them is
    current — the one with `superseded_by IS NULL`, enforced by a partial
    unique index rather than by application code, because "current" is a fact
    about the table and two processes accepting at once must not both win.
    """

    __tablename__ = "research_acceptance"
    __table_args__ = (
        sa.Index(
            "uq_research_acceptance_current",
            "project_id",
            unique=True,
            postgresql_where=sa.text("superseded_by IS NULL"),
        ),
        sa.Index("ix_research_acceptance_project", "project_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    #: One acceptance per research run, ever. Re-accepting the same run is the
    #: same statement twice, not a new fact.
    run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    report_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("report.id", ondelete="CASCADE"), nullable=False
    )
    accepted_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    accepted_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    note: Mapped[str | None] = mapped_column(sa.Text)
    #: The verdict at the moment of acceptance, copied rather than joined: a
    #: re-run of research must not retroactively change what was accepted.
    launch_readiness_at_acceptance: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Non-null only when an admin accepted a `no_go` report. Printed on the
    #: plan cover page and written to the audit log (Stage 02 PRD §18).
    override_reason: Mapped[str | None] = mapped_column(sa.Text)
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("research_acceptance.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )

    @property
    def is_current(self) -> bool:
        return self.superseded_by is None


class CampaignPlan(Base):
    """One version of a media plan. Frozen versions are immutable (Stage 02 law 17).

    The immutability is a database trigger, not a code path: `payload`,
    `markdown` and `version` cannot change once `status = 'frozen'`, whichever
    process is doing the writing. Everything else on the row stays writable,
    because a frozen plan whose source research was re-accepted still has to be
    able to raise `source_superseded`.
    """

    __tablename__ = "campaign_plan"
    __table_args__ = (
        # Partial, not a table constraint (migration 0014). `version` is minted
        # at freeze, so every unfrozen plan carries 0 — and a project holds
        # more than one of those the first time a gate is rejected and the run
        # is repeated. Uniqueness is what the freeze needs, and it needs it
        # only over versions the freeze actually minted.
        sa.Index(
            "uq_campaign_plan_project_version_minted",
            "project_id",
            "version",
            unique=True,
            postgresql_where=sa.text("version > 0"),
        ),
        sa.Index("ix_campaign_plan_project_version", "project_id", sa.text("version DESC")),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    plan_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    #: RESTRICT, not CASCADE: the acceptance is the plan's provenance, and a
    #: plan that cannot name what it was planned from is not auditable.
    acceptance_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("research_acceptance.id", ondelete="RESTRICT"),
        nullable=False,
    )
    schema_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: 1, 2, 3 … per project, minted at freeze and unique with `project_id`
    #: from then on. **0 until then**, for every plan in every unfrozen state
    #: — which is why the uniqueness above is partial. `version > 0` is the
    #: test for "this plan has been frozen at least once".
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    status: Mapped[CampaignPlanStatus] = mapped_column(
        _enum(CampaignPlanStatus, "campaign_plan_status"),
        nullable=False,
        server_default=CampaignPlanStatus.DRAFT.value,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    frozen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    frozen_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL")
    )
    #: The four gate decisions the freeze sealed. Ids rather than a join,
    #: because the freeze is a snapshot of what was decided at that moment.
    frozen_approval_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    #: Set when a newer research acceptance lands. A frozen plan stays valid
    #: and gains a banner; a draft cannot be frozen until it is re-run (§4.4).
    source_superseded: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), onupdate=_now(), nullable=False
    )

    @property
    def is_frozen(self) -> bool:
        return self.status is CampaignPlanStatus.FROZEN


class PlanCalc(Base):
    """The audit trail behind one number in the plan (Stage 02 law 14).

    Every figure a plan asserts comes from a registered `@formula` in `calc/`
    and leaves one of these rows behind, so an approver can ask "where did
    $47 come from" and get the formula, its inputs and the constants version
    rather than a model's recollection.

    The unique key is `(plan_run_id, formula_id, inputs_hash)`: recomputing the
    same formula over the same inputs inside one run reuses the row instead of
    writing a second answer to the same question.
    """

    __tablename__ = "plan_calc"
    __table_args__ = (
        sa.UniqueConstraint(
            "plan_run_id", "formula_id", "inputs_hash", name="uq_plan_calc_run_formula_inputs"
        ),
        sa.Index("ix_plan_calc_run_node", "plan_run_id", "node_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    plan_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False
    )
    node_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: e.g. 'economics.max_cpa_v1'. The registry key, not a description.
    formula_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Constants-file version plus code version, so a number can be reproduced
    #: against the thresholds that were current when it was computed.
    calc_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    inputs_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    result: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: The `derived` Evidence row this calculation produced. Nullable so the
    #: calculation survives evidence pruning; the number keeps its provenance
    #: either way.
    evidence_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("evidence.id", ondelete="SET NULL")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Stage 03 — Content Guidelines (PRD §7.2)
#
# Ten tables. Three of them are enforced immutable or append-only by database
# trigger rather than by application code (migration 0016), because the thing
# being protected is a legal record: a published rulebook, the compiled program
# Stage 04 lints against, and a named person's signature. A guarantee that
# lives in a service layer is a guarantee that ends the first time somebody
# writes a second service layer.
# ---------------------------------------------------------------------------


class GuidelineStatus(StrEnum):
    """PRD §12.4. `succeeded` on the run never means `published`."""

    DRAFT = "draft"
    #: A gate was rejected, H1 was rejected wholesale, or the critique returned
    #: a blocking issue. The run still finished; the artifact is not publishable.
    BLOCKED = "blocked"
    READY_TO_PUBLISH = "ready_to_publish"
    PUBLISHED = "published"
    SUPERSEDED = "superseded"


class GuidelineMode(StrEnum):
    """Which optional bindings resolved at run start (PRD §4.2).

    Derived from what actually resolved, never taken from the client. The four
    values are not a quality ranking — `standalone` is a first-class, tested
    path (law 21) — they are a record of what the rulebook was built from.
    """

    STANDALONE = "standalone"
    RESEARCH_LINKED = "research_linked"
    PLAN_LINKED = "plan_linked"
    FULLY_LINKED = "fully_linked"


class ClaimType(StrEnum):
    """The shape of an assertion, which is what the detector pass can see.

    Not "is it true" — that is the signature's job, and it is a human's.
    """

    SUPERLATIVE = "superlative"
    COMPARATIVE = "comparative"
    QUANTIFIED = "quantified"
    CERTIFICATION = "certification"
    GUARANTEE = "guarantee"
    ENDORSEMENT = "endorsement"
    PRICING = "pricing"
    SAFETY_REGULATORY = "safety_regulatory"


class ClaimRiskTier(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ClaimStatus(StrEnum):
    """PRD §9.3. Four of these six behave identically at lint time: blocking.

    `unsupported`, `rejected`, `expired` and `revoked` all mean "not licensed",
    and the linter must not treat them differently. They are kept apart because
    *why* a claim is unlicensed is what the writer needs to read, and because
    the routes back to `approved` differ.
    """

    UNSUPPORTED = "unsupported"
    PENDING_SIGNOFF = "pending_signoff"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    REVOKED = "revoked"


class SignatureMethod(StrEnum):
    """How the signer proved it was them. One value today, and it is recorded
    rather than assumed so that adding WebAuthn later does not silently
    re-describe every signature already taken."""

    STEP_UP_PASSWORD = "step_up_password"  # noqa: S105 — a method name, not a secret


class HumanTaskStatus(StrEnum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    #: 3.3.1 found nothing requiring verification. Not a skip and not a
    #: failure: the finding is that the obligation does not apply, which is the
    #: auditable part.
    NOT_REQUIRED = "not_required"
    BLOCKED = "blocked"
    EXPIRED = "expired"


class HumanTaskBlocking(StrEnum):
    """Which board this task stops (PRD §5.4).

    H1 blocks publish: a claims register with no terminal decisions licenses
    nothing and the ruleset would ship inert. H2 blocks launch: a company can
    hold a complete, correct rulebook before it is verified to advertise.
    """

    PUBLISH = "publish"
    LAUNCH = "launch"


class AmendmentOrigin(StrEnum):
    POLICY_WATCH = "policy_watch"
    CLAIM_EXPIRY = "claim_expiry"
    DISAPPROVAL = "disapproval"
    MANUAL = "manual"
    #: Stage 04: applied by the H3 transaction when a legal owner clears a
    #: creative exception (migration 0019).
    CREATIVE_EXCEPTION = "creative_exception"


class AmendmentChangeKind(StrEnum):
    """PRD §8.6, law 29. The consequence of each class is deterministic code;
    only the classification itself is a model call."""

    #: A value inside an existing rule changed. Auto-applies, mints a MINOR.
    MECHANICAL = "mechanical"
    #: A rule appeared or disappeared. Never auto-applies.
    SUBSTANTIVE = "substantive"
    #: Touches a rule whose authority is a legal signature. Voids it.
    SIGNATURE_AFFECTING = "signature_affecting"
    #: Classifier confidence below the floor. Treated as `substantive` —
    #: ambiguity resolves toward the human, always.
    UNCLASSIFIED = "unclassified"


class AmendmentStatus(StrEnum):
    OPEN = "open"
    NEEDS_REVIEW = "needs_review"
    APPLIED = "applied"
    DISMISSED = "dismissed"
    AUTO_APPLIED = "auto_applied"


class DisapprovalStatus(StrEnum):
    NEW = "new"
    RULE_PROPOSED = "rule_proposed"
    RULE_APPLIED = "rule_applied"
    IGNORED = "ignored"


class ContentGuideline(Base):
    """One version of the rulebook (PRD §7.2, §12.1).

    A published row is append-only in the strict sense: migration 0016 installs
    a BEFORE UPDATE trigger that rejects any change to `payload`, `markdown`,
    `ruleset_id`, `version_major` or `version_minor` once `status='published'`.
    Only `status`, `signature_stale` and `binding_superseded` may move
    afterwards, which is what lets a published version be marked stale without
    being rewritten.
    """

    __tablename__ = "content_guideline"
    __table_args__ = (
        sa.UniqueConstraint(
            "project_id",
            "version_major",
            "version_minor",
            name="uq_content_guideline_project_version",
        ),
        sa.Index("ix_content_guideline_project_status", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    guideline_run_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    schema_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    version_major: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    version_minor: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[GuidelineStatus] = mapped_column(
        _enum(GuidelineStatus, "guideline_status"),
        nullable=False,
        server_default=GuidelineStatus.DRAFT.value,
    )
    mode: Mapped[GuidelineMode] = mapped_column(
        _enum(GuidelineMode, "guideline_mode"), nullable=False
    )
    bindings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    #: Which optional inputs were absent. Rendered on the rulebook header and
    #: in every export: a rulebook built without legal guardrails must not look
    #: as authoritative as one built with them (PRD §4.3).
    unbound_inputs: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    markdown: Mapped[str | None] = mapped_column(sa.Text)
    #: Nullable, and that nullability is what breaks the cycle with `rule_set`.
    #: `use_alter` defers the constraint so the two tables can be created in
    #: either order; PRD §7.4 note 5 explains why publish must set this and
    #: `status` in one statement.
    ruleset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("rule_set.id", ondelete="SET NULL", use_alter=True),
    )
    published_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    published_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT")
    )
    published_approval_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        ARRAY(UUID(as_uuid=True))
    )
    #: The legal owner was reassigned, or an amendment voided a signature this
    #: version's ruleset depended on. The version stays published and serving —
    #: the linter un-licenses the affected claims from that moment instead.
    signature_stale: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    binding_superseded: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), onupdate=_now(), nullable=False
    )


class ClaimRecord(Base):
    """One thing we assert, and whether we are licensed to assert it.

    Claims outlive guideline versions: they are a property of the project, not
    of the run that first harvested them. `first_seen_guideline_id` records
    where one came from without tying its life to that version.
    """

    __tablename__ = "claim_record"
    __table_args__ = (
        sa.Index(
            "uq_claim_record_current",
            "project_id",
            "normalized_text",
            unique=True,
            postgresql_where=sa.text("superseded_by IS NULL"),
        ),
        #: The expiry sweep's query, and the "expiring within 30 days" badge.
        sa.Index("ix_claim_record_project_status_expiry", "project_id", "status", "expires_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    first_seen_guideline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("content_guideline.id", ondelete="CASCADE"),
        nullable=False,
    )
    claim_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    normalized_text: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: The phrasings that assert this claim. What the licence pass matches a
    #: candidate span against.
    surface_forms: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    claim_type: Mapped[ClaimType] = mapped_column(_enum(ClaimType, "claim_type"), nullable=False)
    market_scope: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")
    )
    languages: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")
    )
    #: Where we already say it: urls, ad ids. Harvested, not proposed.
    observed_on: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")
    )
    #: `{evidence_ids[], document_refs[], method, as_of}`. A claim with no
    #: evidence is marked `unsupported` and stays that way — the model never
    #: manufactures substantiation.
    substantiation: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'::uuid[]")
    )
    risk_tier: Mapped[ClaimRiskTier] = mapped_column(
        _enum(ClaimRiskTier, "claim_risk_tier"),
        nullable=False,
        server_default=ClaimRiskTier.MEDIUM.value,
    )
    status: Mapped[ClaimStatus] = mapped_column(
        _enum(ClaimStatus, "claim_status"),
        nullable=False,
        server_default=ClaimStatus.UNSUPPORTED.value,
    )
    #: A claims register without expiry is a register of things that used to be
    #: true. NULL until a signature sets one.
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    current_signature_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("claim_signature.id", ondelete="SET NULL", use_alter=True),
    )
    superseded_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("claim_record.id", ondelete="SET NULL")
    )
    #: `harvest` (Stage 03) or `creative_exception` (a claim a legal owner
    #: licensed through H3, Stage 04 PRD §7.1). Text, not an enum, as the PRD
    #: writes it; the default makes every existing row `harvest`.
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="harvest")
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), onupdate=_now(), nullable=False
    )


class ClaimSignature(Base):
    """A named person said these exact claims are safe to run (PRD §7.2, §13).

    Append-only, enforced by trigger: every column except the three void
    columns rejects an UPDATE. A correction is a new signature, never an edit —
    that is what makes "liability sits with a named person" a fact about the
    database rather than a sentence in a diagram.

    `set_hash` is the anti-race guarantee. It is computed over the sorted
    (claim_id, normalized_text, decision) triples, so a register that changed
    between the signer reading it and pressing submit produces a different hash
    and a 409. The signer never signs a set they did not see.
    """

    __tablename__ = "claim_signature"
    __table_args__ = (
        sa.Index("ix_claim_signature_project_signed", "project_id", sa.text("signed_at DESC")),
        sa.Index("ix_claim_signature_signer", "signer_id"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    #: The named legal owner *at signing time*. RESTRICT, not SET NULL: a
    #: signature whose signer cannot be named is not a signature.
    signer_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    claim_ids: Mapped[list[uuid.UUID]] = mapped_column(ARRAY(UUID(as_uuid=True)), nullable=False)
    set_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: `[{claim_id, decision: approved|rejected, note, expires_at}]`. A
    #: partially-approved set is legal and common.
    decisions: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    #: The attestation text the signer confirmed, stored verbatim. If the
    #: wording changes later, what this person agreed to does not.
    statement: Mapped[str] = mapped_column(sa.Text, nullable=False)
    method: Mapped[SignatureMethod] = mapped_column(
        _enum(SignatureMethod, "signature_method"), nullable=False
    )
    reauth_token_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    ip: Mapped[str | None] = mapped_column(INET)
    user_agent: Mapped[str | None] = mapped_column(sa.Text)
    signed_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    voided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    voided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT")
    )
    void_reason: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class HumanTask(Base):
    """An act the agent cannot perform, assigned to one named person.

    `assignee_id NOT NULL` is the non-delegable rule expressed in DDL. Do not
    make it nullable "for flexibility" later: a task with no assignee is a task
    that falls back to a role, and a signature that falls back to a role is a
    checkbox (PRD §7.4 note 4).
    """

    __tablename__ = "human_task"
    __table_args__ = (
        sa.Index("ix_human_task_assignee_status", "assignee_id", "status"),
        sa.Index("ix_human_task_project_key_status", "project_id", "task_key", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    guideline_run_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("run.id", ondelete="SET NULL")
    )
    node_id: Mapped[str | None] = mapped_column(sa.Text)
    #: 'H1' | 'H2'. Text rather than an enum for the same reason
    #: `Approval.gate_key` is: later stages add person-tasks, and a migration
    #: per key is a toll on a value nothing branches on exhaustively.
    task_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    title: Mapped[str] = mapped_column(sa.Text, nullable=False)
    instructions: Mapped[str] = mapped_column(sa.Text, nullable=False)
    assignee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    required_artifacts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    submitted_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    attachment_paths: Mapped[list[str] | None] = mapped_column(ARRAY(sa.Text))
    status: Mapped[HumanTaskStatus] = mapped_column(
        _enum(HumanTaskStatus, "human_task_status"),
        nullable=False,
        server_default=HumanTaskStatus.PENDING.value,
    )
    blocking_for: Mapped[HumanTaskBlocking] = mapped_column(
        _enum(HumanTaskBlocking, "human_task_blocking"), nullable=False
    )
    completed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT")
    )
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), onupdate=_now(), nullable=False
    )


class HumanTaskHandover(Base):
    """Moving a person-task to somebody else. Append-only, and expensive by design.

    A handover requires a written reason, is performed by an admin, and records
    every signature it voided. Reassignment being costly is the correct
    incentive: the cheap version of this is an admin quietly signing.
    """

    __tablename__ = "human_task_handover"
    __table_args__ = (sa.Index("ix_human_task_handover_task", "task_id"),)

    id: Mapped[uuid.UUID] = _pk()
    task_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("human_task.id", ondelete="CASCADE"), nullable=False
    )
    from_user: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    to_user: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    performed_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    voided_signature_ids: Mapped[list[uuid.UUID]] = mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'::uuid[]")
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class SignOffMatrix(Base):
    """Who owns brand, legal and performance sign-off on this project (G6).

    A DAG root, despite sitting at the bottom of the stage diagram: you cannot
    route a non-delegable signature without a named owner. Exactly one matrix
    is current per project, enforced by a partial unique index rather than by
    application code — two admins setting it at once must not both win.
    """

    __tablename__ = "signoff_matrix"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "version", name="uq_signoff_matrix_project_version"),
        sa.Index(
            "uq_signoff_matrix_current",
            "project_id",
            unique=True,
            postgresql_where=sa.text("superseded_at IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    brand_owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    #: The only identity `CLAIM_SIGN` is narrowed to. Changing it voids every
    #: signature the outgoing owner made (PRD §5.2).
    legal_owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    performance_owner_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    previous_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("signoff_matrix.id", ondelete="SET NULL")
    )
    set_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    set_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    superseded_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class RuleSet(Base):
    """The compiled program Stage 04 lints against (PRD §12.2).

    Fully immutable: migration 0016 installs a BEFORE UPDATE trigger that
    rejects every UPDATE, with no exceptions at all. A change compiles a new
    row. That is what lets an asset produced six months ago be re-audited
    against exactly the rules that applied when it was made, which is the whole
    reason `ruleset_version` is pinned rather than resolved.
    """

    __tablename__ = "rule_set"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "ruleset_version", name="uq_rule_set_project_version"),
        sa.UniqueConstraint("hash", name="uq_rule_set_hash"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    guideline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("content_guideline.id", ondelete="CASCADE"),
        nullable=False,
    )
    #: "{major}.{minor}+{hash8}". The pin Stage 04 records on every creative run.
    ruleset_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    compiled: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    #: Bumped when the compiler or a pinned matcher library changes. Part of
    #: what `hash` is over, so a library bump produces a new ruleset rather
    #: than silently changing what an old pin means.
    compiler_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    constants_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rule_count: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class PolicySource(Base):
    """A watched policy page. URLs are configuration, never prompt text (law 25)."""

    __tablename__ = "policy_source"
    __table_args__ = (
        sa.UniqueConstraint("workspace_id", "url", name="uq_policy_source_workspace_url"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    label: Mapped[str] = mapped_column(sa.Text, nullable=False)
    jurisdiction: Mapped[str | None] = mapped_column(sa.Text)
    area: Mapped[str | None] = mapped_column(sa.Text)
    #: Narrows the hashed region of the page. A selector that stops matching
    #: marks the source stale — it must never quietly report "no change".
    selector: Mapped[str | None] = mapped_column(sa.Text)
    last_hash: Mapped[str | None] = mapped_column(sa.Text)
    last_checked_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_changed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("true")
    )
    poll_cron: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), onupdate=_now(), nullable=False
    )


class PolicyAmendment(Base):
    """A reason the published rulebook should change (PRD §8.6)."""

    __tablename__ = "policy_amendment"
    __table_args__ = (
        sa.Index(
            "ix_policy_amendment_project_status",
            "project_id",
            "status",
            sa.text("detected_at DESC"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    source_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("policy_source.id", ondelete="SET NULL")
    )
    origin: Mapped[AmendmentOrigin] = mapped_column(
        _enum(AmendmentOrigin, "amendment_origin"), nullable=False
    )
    detected_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    change_kind: Mapped[AmendmentChangeKind] = mapped_column(
        _enum(AmendmentChangeKind, "amendment_change_kind"),
        nullable=False,
        server_default=AmendmentChangeKind.UNCLASSIFIED.value,
    )
    diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    proposed_rule_changes: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    rationale: Mapped[str | None] = mapped_column(sa.Text)
    status: Mapped[AmendmentStatus] = mapped_column(
        _enum(AmendmentStatus, "amendment_status"),
        nullable=False,
        server_default=AmendmentStatus.OPEN.value,
    )
    applied_ruleset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("rule_set.id", ondelete="SET NULL")
    )
    voided_signature_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT")
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class DisapprovalEvent(Base):
    """Google told us exactly what was wrong, once. This is where it stops
    being thrown away (PRD §2, §8.5).

    Every row becomes a learned rule or an explicit reason it cannot. A repeat
    of a `policy_topic` that already carries a learned rule raises
    `rule_ineffective` rather than proposing a duplicate.
    """

    __tablename__ = "disapproval_event"
    __table_args__ = (
        sa.UniqueConstraint(
            "workspace_id",
            "ad_resource_name",
            "policy_topic",
            "observed_at",
            name="uq_disapproval_event_observation",
        ),
        sa.Index("ix_disapproval_event_project_status", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )
    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )
    ad_resource_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    campaign_ref: Mapped[str | None] = mapped_column(sa.Text)
    asset_ref: Mapped[str | None] = mapped_column(sa.Text)
    policy_topic: Mapped[str] = mapped_column(sa.Text, nullable=False)
    policy_detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    observed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )
    learned_rule_id: Mapped[str | None] = mapped_column(sa.Text)
    amendment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("policy_amendment.id", ondelete="SET NULL")
    )
    status: Mapped[DisapprovalStatus] = mapped_column(
        _enum(DisapprovalStatus, "disapproval_status"),
        nullable=False,
        server_default=DisapprovalStatus.NEW.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Stage 04 — the Copy & Creative Agent (Stage 04 PRD §7.2)
#
# Types created by migration 0019, tables by 0020. Each enum is named for its
# table and never *as* its table: Postgres gives every table a composite type
# of the same name, so an enum called `asset_decision` could not coexist with
# the `asset_decision` table.
#
# Nullability follows the PRD: a column §7.2 writes as `null` is nullable,
# everything else is NOT NULL. The handful of places the PRD is silent and the
# lifecycle forces NULL (a landing page that never answered has no status; a
# preview that could not render has no file) say so where they are declared.
# ---------------------------------------------------------------------------


class CreativeAssetKind(StrEnum):
    HEADLINE = "headline"
    LONG_HEADLINE = "long_headline"
    DESCRIPTION = "description"
    PATH = "path"
    SITELINK = "sitelink"
    CALLOUT = "callout"
    STRUCTURED_SNIPPET = "structured_snippet"
    PROMOTION = "promotion"
    PRICE = "price"
    LEAD_FORM = "lead_form"
    BUSINESS_NAME = "business_name"
    VIDEO_SCRIPT = "video_script"
    IMAGE = "image"
    VIDEO = "video"
    LOGO = "logo"


class CreativeAssetStatus(StrEnum):
    DRAFT = "draft"
    LINTED = "linted"
    RESERVE = "reserve"
    AWAITING_REVIEW = "awaiting_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    DROPPED = "dropped"
    AWAITING_EXCEPTION = "awaiting_exception"
    RELEASED = "released"


class CreativeAssetVariant(StrEnum):
    A = "A"
    B = "B"


class GenerationModality(StrEnum):
    IMAGE = "image"
    VIDEO = "video"


class GenerationStatus(StrEnum):
    QUEUED = "queued"
    SUBMITTING = "submitting"
    SUBMITTED = "submitted"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    TIMED_OUT = "timed_out"
    BLOCKED_BY_BUDGET = "blocked_by_budget"
    #: Law 37: surfaced to a human, never retried.
    UNKNOWN_SUBMIT_STATE = "unknown_submit_state"


class MediaArtifactRole(StrEnum):
    CANDIDATE = "candidate"
    MASTER = "master"
    RENDITION = "rendition"
    CLIP = "clip"
    PREVIEW = "preview"
    THUMBNAIL = "thumbnail"
    POSTER = "poster"
    FRAME_SAMPLE = "frame_sample"


class MediaArtifactDerivation(StrEnum):
    NATIVE = "native"
    RELAID = "relaid"
    CROP = "crop"
    COMPOSITED = "composited"
    ENCODED = "encoded"
    INGESTED = "ingested"


class AssetDecisionChoice(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"
    REGENERATE = "regenerate"


class CreativeExceptionKind(StrEnum):
    NEW_CLAIM = "new_claim"
    DISCLAIMER = "disclaimer"
    IMAGE_RIGHT = "image_right"


class CreativeExceptionStatus(StrEnum):
    OPEN = "open"
    CLEARED = "cleared"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"


class LandingAuditVerdict(StrEnum):
    OK = "ok"
    NEEDS_CHANGE = "needs_change"
    BLOCKING_FOR_LAUNCH = "blocking_for_launch"
    UNREACHABLE = "unreachable"


class PreviewDevice(StrEnum):
    MOBILE = "mobile"
    DESKTOP = "desktop"


class PreviewVerdict(StrEnum):
    PASS = "pass"  # noqa: S105 — a verdict, not a password
    WARNING = "warning"
    BLOCKING = "blocking"
    UNAVAILABLE = "unavailable"


class CreativePackageStatus(StrEnum):
    DRAFT = "draft"
    BLOCKED = "blocked"
    READY_TO_RELEASE = "ready_to_release"
    RELEASED = "released"
    SUPERSEDED = "superseded"


class MediaReferenceKind(StrEnum):
    PRODUCT_REFERENCE = "product_reference"
    STYLE_REFERENCE = "style_reference"


class MediaReferenceOrigin(StrEnum):
    OWN = "own"
    LICENSED = "licensed"
    THIRD_PARTY = "third_party"


#: (enum class, Postgres type) for every Stage 04 type. Migration 0019 creates
#: exactly these; `test_stage04_schema` asserts the database agrees label for
#: label, so a member added here without a migration fails a test rather than
#: an insert.
STAGE04_ENUMS: tuple[tuple[type[StrEnum], str], ...] = (
    (CreativeAssetKind, "creative_asset_kind"),
    (CreativeAssetStatus, "creative_asset_status"),
    (CreativeAssetVariant, "creative_asset_variant"),
    (GenerationModality, "generation_modality"),
    (GenerationStatus, "generation_status"),
    (MediaArtifactRole, "media_artifact_role"),
    (MediaArtifactDerivation, "media_artifact_derivation"),
    (AssetDecisionChoice, "asset_decision_choice"),
    (CreativeExceptionKind, "creative_exception_kind"),
    (CreativeExceptionStatus, "creative_exception_status"),
    (LandingAuditVerdict, "landing_audit_verdict"),
    (PreviewDevice, "preview_device"),
    (PreviewVerdict, "preview_verdict"),
    (CreativePackageStatus, "creative_package_status"),
    (MediaReferenceKind, "media_reference_kind"),
    (MediaReferenceOrigin, "media_reference_origin"),
)


def _workspace_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("workspace.id", ondelete="CASCADE"), nullable=False
    )


def _project_fk() -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("project.id", ondelete="CASCADE"), nullable=False
    )


def _creative_run_fk(*, unique: bool = False) -> Mapped[uuid.UUID]:
    return mapped_column(
        UUID(as_uuid=True),
        sa.ForeignKey("run.id", ondelete="CASCADE"),
        nullable=False,
        unique=unique,
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(sa.DateTime(timezone=True), server_default=_now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), onupdate=_now(), nullable=False
    )


def _uuids() -> Mapped[list[uuid.UUID]]:
    return mapped_column(
        ARRAY(UUID(as_uuid=True)), nullable=False, server_default=sa.text("'{}'::uuid[]")
    )


def _jsonb_object() -> Mapped[dict[str, Any]]:
    return mapped_column(JSONB, nullable=False, server_default=sa.text("'{}'::jsonb"))


class CreativeBrief(Base):
    """4.1's brief, and the hash G7 approved (PRD §7.2).

    `approved_hash` must equal `brief_hash` for any media submit. Once it is
    set, trigger `creative_brief_approved_freeze` makes `payload`, `markdown`
    and `brief_hash` immutable, so "the brief that was approved" and "the brief
    on the row" cannot drift apart after the fact.
    """

    __tablename__ = "creative_brief"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace_fk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    creative_run_id: Mapped[uuid.UUID] = _creative_run_fk(unique=True)
    schema_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    payload: Mapped[dict[str, Any]] = _jsonb_object()
    markdown: Mapped[str] = mapped_column(sa.Text, nullable=False, server_default="")
    #: sha256 over the canonical payload.
    brief_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: G7.
    approval_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("approval.id", ondelete="SET NULL")
    )
    approved_hash: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class MediaReference(Base):
    """A product or style reference someone attested the rights to (PRD §10.3).

    `origin='third_party'` is never sent to a provider without a cleared H3
    `image_right` (Law 44).
    """

    __tablename__ = "media_reference"
    __table_args__ = (
        sa.UniqueConstraint("project_id", "sha256", name="uq_media_reference_project_sha256"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace_fk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    kind: Mapped[MediaReferenceKind] = mapped_column(
        _enum(MediaReferenceKind, "media_reference_kind"), nullable=False
    )
    storage_path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    media_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    width: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    height: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: The sku / product_set depicted.
    product_ref: Mapped[str | None] = mapped_column(sa.Text)
    origin: Mapped[MediaReferenceOrigin] = mapped_column(
        _enum(MediaReferenceOrigin, "media_reference_origin"), nullable=False
    )
    rights_statement: Mapped[str] = mapped_column(sa.Text, nullable=False)
    attested_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    attested_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    retired_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()


class CreativeAsset(Base):
    """One piece of copy or media, written into one container of the plan.

    Frozen at release: trigger `creative_asset_frozen` rejects every change
    once `frozen_at` is set (Law 42).
    """

    __tablename__ = "creative_asset"
    __table_args__ = (
        sa.Index("ix_creative_asset_run_kind_status", "creative_run_id", "kind", "status"),
        sa.Index("ix_creative_asset_run_ad_group", "creative_run_id", "ad_group_ref"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace_fk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    creative_run_id: Mapped[uuid.UUID] = _creative_run_fk()
    node_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    campaign_ref: Mapped[str] = mapped_column(sa.Text, nullable=False)
    ad_group_ref: Mapped[str | None] = mapped_column(sa.Text)
    ad_ref: Mapped[str | None] = mapped_column(sa.Text)
    kind: Mapped[CreativeAssetKind] = mapped_column(
        _enum(CreativeAssetKind, "creative_asset_kind"), nullable=False
    )
    #: A `LintTarget.surface` literal.
    surface: Mapped[str] = mapped_column(sa.Text, nullable=False)
    variant: Mapped[CreativeAssetVariant | None] = mapped_column(
        _enum(CreativeAssetVariant, "creative_asset_variant")
    )
    category: Mapped[str | None] = mapped_column(sa.Text)
    text: Mapped[str | None] = mapped_column(sa.Text)
    #: Sitelink `{link_text, line1, line2, final_url}` and the like.
    fields: Mapped[dict[str, Any]] = _jsonb_object()
    claim_ids: Mapped[list[uuid.UUID]] = _uuids()
    #: Law 35: every numeric and date field of a promotion or price asset.
    offer_binding: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    pin_position: Mapped[str | None] = mapped_column(sa.Text)
    generated_by_ai: Mapped[bool] = mapped_column(sa.Boolean, nullable=False)
    status: Mapped[CreativeAssetStatus] = mapped_column(
        _enum(CreativeAssetStatus, "creative_asset_status"),
        nullable=False,
        server_default=CreativeAssetStatus.DRAFT.value,
    )
    lint: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    ruleset_version: Mapped[str | None] = mapped_column(sa.Text)
    #: `{origin: generated|human_edit|reserve_swap|regenerated|reused,
    #:   parent_id, by_user?, node_id}`.
    lineage: Mapped[dict[str, Any]] = _jsonb_object()
    content_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    frozen_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class GenerationJob(Base):
    """One media request to OpenRouter, committed before the network call.

    `idempotency_key UNIQUE` is Law 37 in DDL: the row exists before the POST,
    so a resumed worker finds it and re-polls `openrouter_job_id` rather than
    submitting (and paying for) the video a second time. Never drop it "to
    allow retries" (PRD §7.5 note 4).
    """

    __tablename__ = "generation_job"
    __table_args__ = (
        sa.Index("ix_generation_job_run_status", "creative_run_id", "status"),
        sa.Index("ix_generation_job_status_next_poll", "status", "next_poll_at"),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace_fk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    creative_run_id: Mapped[uuid.UUID] = _creative_run_fk()
    node_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("creative_asset.id", ondelete="SET NULL")
    )
    round: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    modality: Mapped[GenerationModality] = mapped_column(
        _enum(GenerationModality, "generation_modality"), nullable=False
    )
    model_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    provider_tag: Mapped[str | None] = mapped_column(sa.Text)
    capability_hash: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Canonical and redacted: references as sha256, never base64 (Law 44).
    request: Mapped[dict[str, Any]] = _jsonb_object()
    idempotency_key: Mapped[str] = mapped_column(sa.Text, nullable=False, unique=True)
    #: Video only.
    openrouter_job_id: Mapped[str | None] = mapped_column(sa.Text, unique=True)
    status: Mapped[GenerationStatus] = mapped_column(
        _enum(GenerationStatus, "generation_status"),
        nullable=False,
        server_default=GenerationStatus.QUEUED.value,
    )
    estimate_usd: Mapped[Decimal] = mapped_column(sa.Numeric(12, 4), nullable=False)
    cost_usd: Mapped[Decimal | None] = mapped_column(sa.Numeric(12, 4))
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    polls: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("0"))
    next_poll_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    submitted_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class MediaArtifact(Base):
    """One file: a candidate, a master, a rendition, a clip, a preview.

    `ck_media_artifact_uniform_scale` is Law 39 in DDL — relay out, never
    stretch. A crop scales both axes by the same factor or it is not stored.
    """

    __tablename__ = "media_artifact"
    __table_args__ = (
        # Law 39: RELAY OUT, NEVER STRETCH. Compared as the JSON text of each
        # factor, exactly as PRD §7.2 writes it: the writer stores one
        # computed number in both keys, so equal factors are equal text.
        sa.CheckConstraint(
            "transform IS NULL OR (transform->>'sx') = (transform->>'sy')",
            name="ck_media_artifact_uniform_scale",
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace_fk()
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("creative_asset.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("generation_job.id", ondelete="SET NULL")
    )
    role: Mapped[MediaArtifactRole] = mapped_column(
        _enum(MediaArtifactRole, "media_artifact_role"), nullable=False
    )
    storage_path: Mapped[str] = mapped_column(sa.Text, nullable=False)
    media_type: Mapped[str] = mapped_column(sa.Text, nullable=False)
    width: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    height: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(sa.Integer)
    bytes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(sa.Text, nullable=False)
    aspect_ratio: Mapped[str] = mapped_column(sa.Text, nullable=False)
    derivation: Mapped[MediaArtifactDerivation] = mapped_column(
        _enum(MediaArtifactDerivation, "media_artifact_derivation"), nullable=False
    )
    derived_from: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("media_artifact.id", ondelete="SET NULL")
    )
    #: `{crop_box, sx, sy, logo_box, encoder_args}`. NULL for a native file,
    #: which is the case the CHECK's first half admits.
    transform: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    #: Pillow / ffprobe output.
    probe: Mapped[dict[str, Any]] = _jsonb_object()
    #: `{xmp_digital_source_type, mp4_comment, visible_label?}`. NULL for an
    #: `ingested` file nobody generated — there is nothing to disclose.
    disclosure: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = _created_at()


class AssetDecision(Base):
    """One reviewer's decision on one asset at G8/G8b. Append-only.

    Trigger `asset_decision_append_only` rejects every UPDATE: a changed mind
    is a new approval round, never an edited row.
    """

    __tablename__ = "asset_decision"
    __table_args__ = (
        sa.UniqueConstraint("approval_id", "asset_id", name="uq_asset_decision_approval_asset"),
    )

    id: Mapped[uuid.UUID] = _pk()
    approval_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("approval.id", ondelete="CASCADE"), nullable=False
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("creative_asset.id", ondelete="CASCADE"), nullable=False
    )
    round: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    decision: Mapped[AssetDecisionChoice] = mapped_column(
        _enum(AssetDecisionChoice, "asset_decision_choice"), nullable=False
    )
    #: `{label_ok, product_match_ok, subjects_ok, rights_ok}`.
    checklist: Mapped[dict[str, Any]] = _jsonb_object()
    note: Mapped[str | None] = mapped_column(sa.Text)
    model_override: Mapped[str | None] = mapped_column(sa.Text)
    params_override: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    decided_by: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False
    )
    decided_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=_now(), nullable=False
    )


class CreativeException(Base):
    """Something the copy wants to say that nobody has licensed yet (Law 34).

    Cleared only through H3, which writes the `ClaimSignature`.
    """

    __tablename__ = "creative_exception"

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace_fk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    creative_run_id: Mapped[uuid.UUID] = _creative_run_fk()
    kind: Mapped[CreativeExceptionKind] = mapped_column(
        _enum(CreativeExceptionKind, "creative_exception_kind"), nullable=False
    )
    subject_text: Mapped[str | None] = mapped_column(sa.Text)
    asset_ids: Mapped[list[uuid.UUID]] = _uuids()
    occurrences: Mapped[int] = mapped_column(
        sa.Integer, nullable=False, server_default=sa.text("1")
    )
    #: claim: `{claim_type, surface_forms[], substantiation}` ·
    #: disclaimer: `{text, placement}` · image_right: `{basis, flag}`.
    proposed: Mapped[dict[str, Any]] = _jsonb_object()
    evidence_ids: Mapped[list[uuid.UUID]] = _uuids()
    fallback_asset_ids: Mapped[list[uuid.UUID]] = _uuids()
    status: Mapped[CreativeExceptionStatus] = mapped_column(
        _enum(CreativeExceptionStatus, "creative_exception_status"),
        nullable=False,
        server_default=CreativeExceptionStatus.OPEN.value,
    )
    human_task_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("human_task.id", ondelete="SET NULL")
    )
    claim_record_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("claim_record.id", ondelete="SET NULL")
    )
    signature_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("claim_signature.id", ondelete="SET NULL")
    )
    decided_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="RESTRICT")
    )
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    decision_note: Mapped[str | None] = mapped_column(sa.Text)
    set_hash: Mapped[str | None] = mapped_column(sa.Text)
    reauth_token_id: Mapped[str | None] = mapped_column(sa.Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class LandingPageAudit(Base):
    """4.5 — one landing URL, audited once per run (GETs only, Law 41)."""

    __tablename__ = "landing_page_audit"
    __table_args__ = (
        sa.UniqueConstraint("creative_run_id", "url", name="uq_landing_page_audit_run_url"),
    )

    id: Mapped[uuid.UUID] = _pk()
    creative_run_id: Mapped[uuid.UUID] = _creative_run_fk()
    url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: Both NULL when `verdict='unreachable'`: a page that never answered has
    #: no final URL and no status.
    final_url: Mapped[str | None] = mapped_column(sa.Text)
    http_status: Mapped[int | None] = mapped_column(sa.Integer)
    ad_group_refs: Mapped[list[str]] = mapped_column(
        ARRAY(sa.Text), nullable=False, server_default=sa.text("'{}'::text[]")
    )
    #: message_match, fold_px, offer bbox, fields.
    metrics: Mapped[dict[str, Any]] = _jsonb_object()
    patch: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    verdict: Mapped[LandingAuditVerdict] = mapped_column(
        _enum(LandingAuditVerdict, "landing_audit_verdict"), nullable=False
    )
    screenshots: Mapped[dict[str, Any]] = _jsonb_object()
    evidence_ids: Mapped[list[uuid.UUID]] = _uuids()
    created_at: Mapped[datetime] = _created_at()


class RenderPreview(Base):
    """4.6.4 — one ad combination, rendered on one device."""

    __tablename__ = "render_preview"

    id: Mapped[uuid.UUID] = _pk()
    creative_run_id: Mapped[uuid.UUID] = _creative_run_fk()
    ad_ref: Mapped[str] = mapped_column(sa.Text, nullable=False)
    device: Mapped[PreviewDevice] = mapped_column(
        _enum(PreviewDevice, "preview_device"), nullable=False
    )
    combination: Mapped[dict[str, Any]] = _jsonb_object()
    #: NULL when `verdict='unavailable'`: a render that did not happen has no
    #: file.
    storage_path: Mapped[str | None] = mapped_column(sa.Text)
    dom_metrics: Mapped[dict[str, Any]] = _jsonb_object()
    spec_diff: Mapped[dict[str, Any]] = _jsonb_object()
    visual_diff: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    template_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    verdict: Mapped[PreviewVerdict] = mapped_column(
        _enum(PreviewVerdict, "preview_verdict"), nullable=False
    )
    created_at: Mapped[datetime] = _created_at()


class CreativePackage(Base):
    """What Stage 05 loads. Immutable once released (Law 42).

    Trigger `creative_package_released` allows exactly three columns to move
    after `status='released'`: `status` itself (a released package can become
    `superseded`) and the two `*_superseded` banners.

    `plan_id` and `guideline_id` take the default NO ACTION rather than
    RESTRICT: both are provenance, but a project delete cascades to the plan,
    the guideline and this row in one statement, and RESTRICT is checked
    row-by-row mid-cascade where NO ACTION waits for the statement to end.
    """

    __tablename__ = "creative_package"
    __table_args__ = (
        # `version` is 0 until release mints `max(version) + 1` (§12.4), so
        # uniqueness holds over minted versions only — migration 0022, the
        # same shape 0014 gave `campaign_plan`.
        sa.Index(
            "uq_creative_package_project_version_minted",
            "project_id",
            "version",
            unique=True,
            postgresql_where=sa.text("version > 0"),
        ),
    )

    id: Mapped[uuid.UUID] = _pk()
    workspace_id: Mapped[uuid.UUID] = _workspace_fk()
    project_id: Mapped[uuid.UUID] = _project_fk()
    creative_run_id: Mapped[uuid.UUID] = _creative_run_fk(unique=True)
    schema_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: 0 until released; release mints `max(version) + 1` for the project.
    version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    status: Mapped[CreativePackageStatus] = mapped_column(
        _enum(CreativePackageStatus, "creative_package_status"),
        nullable=False,
        server_default=CreativePackageStatus.DRAFT.value,
    )
    plan_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("campaign_plan.id"), nullable=False
    )
    plan_version: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    guideline_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("content_guideline.id"), nullable=False
    )
    ruleset_version: Mapped[str] = mapped_column(sa.Text, nullable=False)
    #: NULL on a draft package whose brief has not been written yet.
    brief_hash: Mapped[str | None] = mapped_column(sa.Text)
    payload: Mapped[dict[str, Any]] = _jsonb_object()
    #: Both written at release, which is what hashes the manifest.
    manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    package_hash: Mapped[str | None] = mapped_column(sa.Text)
    released_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    released_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), sa.ForeignKey("user.id", ondelete="SET NULL")
    )
    released_approval_ids: Mapped[list[uuid.UUID] | None] = mapped_column(ARRAY(UUID(as_uuid=True)))
    plan_superseded: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    ruleset_superseded: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        sa.Numeric(12, 4), nullable=False, server_default=sa.text("0")
    )
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


#: Every table the migration must create, in dependency order.
ALL_TABLES: tuple[str, ...] = (
    "workspace",
    "user",
    "membership",
    "invite",
    "audit_log",
    "project",
    "credential",
    "source_connection",
    "run",
    "node_run",
    "evidence",
    "approval",
    "report",
    "export",
    "schedule",
    "project_document",
    "research_acceptance",
    "campaign_plan",
    "plan_calc",
    # Stage 03 (migration 0016). `rule_set` before `content_guideline` is not
    # possible — they reference each other — so the cycle is broken by
    # `content_guideline.ruleset_id` being nullable and added with use_alter.
    "content_guideline",
    "rule_set",
    "claim_signature",
    "claim_record",
    "signoff_matrix",
    "human_task",
    "human_task_handover",
    "policy_source",
    "policy_amendment",
    "disapproval_event",
    # Stage 04 (migration 0020).
    "creative_brief",
    "media_reference",
    "creative_asset",
    "generation_job",
    "media_artifact",
    "asset_decision",
    "creative_exception",
    "landing_page_audit",
    "render_preview",
    "creative_package",
)
