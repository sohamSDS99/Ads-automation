"""Stage 03 — the cold-start schema (Stage 03 PRD §7).

Ten tables, one new column, one rewritten CHECK. The two lines worth reading
before anything else:

**The `run` CHECK.** Stage 02 wrote `(stage = 'plan') = (source_run_id IS NOT
NULL)`. As an equality that says two things: a plan run must name its source,
and nothing but a plan run may have one. The first is still true. The second
was a proxy for "a research run must not claim a source" — true, and tested —
that also happened to exclude guideline runs, which *do* consume research when
a binding resolves. Both halves survive here; only the part that named `plan`
where it meant "not research" is widened. Without this swap no guideline run
that bound research can be inserted at all, which is why it is the first
statement in `upgrade()`.

**The `content_guideline` ⇄ `rule_set` cycle.** The two tables reference each
other, so one of the foreign keys has to be added after both tables exist.
`content_guideline.ruleset_id` is the nullable side and therefore the one that
is deferred; publish sets it and `status='published'` in a single statement so
the immutability trigger, which reads the row as it was before the statement,
still sees `ready_to_publish` (PRD §7.4 note 5).

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # The column first: the CHECK below references it.
    op.add_column("run", sa.Column("bindings", postgresql.JSONB(astext_type=sa.Text())))

    # Stage 02's equality, split into the implication it meant. Dropped and
    # recreated rather than altered — Postgres has no ALTER CONSTRAINT for a
    # CHECK expression — and the name is reused so nothing downstream has to
    # learn a second one.
    op.drop_constraint("ck_run_plan_has_source", "run", type_="check")
    op.create_check_constraint(
        "ck_run_plan_has_source",
        "run",
        # Both halves of Stage 02's equality, with only the part that excluded
        # guideline runs removed. See `db/models.py` for why the second half
        # stays.
        "(stage <> 'plan' OR source_run_id IS NOT NULL) AND (stage <> 'research' OR source_run_id IS NULL)",
    )
    # '{}' is valid and means "bound nothing", which is the standalone mode and
    # the point of the stage. NULL is not: it cannot be told apart from "nobody
    # has asked yet".
    op.create_check_constraint(
        "ck_run_guideline_has_bindings",
        "run",
        "stage <> 'guideline' OR (bindings IS NOT NULL AND jsonb_typeof(bindings) = 'object')",
    )

    op.create_table(
        "policy_source",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("jurisdiction", sa.Text(), nullable=True),
        sa.Column("area", sa.Text(), nullable=True),
        sa.Column("selector", sa.Text(), nullable=True),
        sa.Column("last_hash", sa.Text(), nullable=True),
        sa.Column("last_checked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("poll_cron", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("workspace_id", "url", name="uq_policy_source_workspace_url"),
    )
    op.create_table(
        "claim_signature",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("signer_id", sa.UUID(), nullable=False),
        sa.Column("claim_ids", postgresql.ARRAY(sa.UUID()), nullable=False),
        sa.Column("set_hash", sa.Text(), nullable=False),
        sa.Column("decisions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("statement", sa.Text(), nullable=False),
        sa.Column(
            "method", postgresql.ENUM("step_up_password", name="signature_method"), nullable=False
        ),
        sa.Column("reauth_token_id", sa.Text(), nullable=False),
        sa.Column("ip", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column(
            "signed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("voided_by", sa.UUID(), nullable=True),
        sa.Column("void_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["signer_id"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["voided_by"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_claim_signature_project_signed",
        "claim_signature",
        ["project_id", sa.literal_column("signed_at DESC")],
        unique=False,
    )
    op.create_index("ix_claim_signature_signer", "claim_signature", ["signer_id"], unique=False)
    op.create_table(
        "signoff_matrix",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("brand_owner_id", sa.UUID(), nullable=False),
        sa.Column("legal_owner_id", sa.UUID(), nullable=False),
        sa.Column("performance_owner_id", sa.UUID(), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("previous_id", sa.UUID(), nullable=True),
        sa.Column("set_by", sa.UUID(), nullable=False),
        sa.Column(
            "set_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["brand_owner_id"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["legal_owner_id"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["performance_owner_id"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["previous_id"], ["signoff_matrix.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["set_by"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "version", name="uq_signoff_matrix_project_version"),
    )
    op.create_index(
        "uq_signoff_matrix_current",
        "signoff_matrix",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.create_table(
        "content_guideline",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("guideline_run_id", sa.UUID(), nullable=False),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("version_major", sa.Integer(), nullable=False),
        sa.Column("version_minor", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "draft",
                "blocked",
                "ready_to_publish",
                "published",
                "superseded",
                name="guideline_status",
            ),
            server_default="draft",
            nullable=False,
        ),
        sa.Column(
            "mode",
            postgresql.ENUM(
                "standalone",
                "research_linked",
                "plan_linked",
                "fully_linked",
                name="guideline_mode",
            ),
            nullable=False,
        ),
        sa.Column(
            "bindings",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "unbound_inputs",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("markdown", sa.Text(), nullable=True),
        sa.Column("ruleset_id", sa.UUID(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by", sa.UUID(), nullable=True),
        sa.Column("published_approval_ids", postgresql.ARRAY(sa.UUID()), nullable=True),
        sa.Column("signature_stale", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "binding_superseded", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["guideline_run_id"], ["run.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["published_by"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("guideline_run_id"),
        sa.UniqueConstraint(
            "project_id",
            "version_major",
            "version_minor",
            name="uq_content_guideline_project_version",
        ),
    )
    op.create_index(
        "ix_content_guideline_project_status",
        "content_guideline",
        ["project_id", "status"],
        unique=False,
    )
    op.create_table(
        "human_task",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("guideline_run_id", sa.UUID(), nullable=True),
        sa.Column("node_id", sa.Text(), nullable=True),
        sa.Column("task_key", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("instructions", sa.Text(), nullable=False),
        sa.Column("assignee_id", sa.UUID(), nullable=False),
        sa.Column(
            "required_artifacts",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("submitted_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("attachment_paths", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending",
                "in_progress",
                "completed",
                "not_required",
                "blocked",
                "expired",
                name="human_task_status",
            ),
            server_default="pending",
            nullable=False,
        ),
        sa.Column(
            "blocking_for",
            postgresql.ENUM("publish", "launch", name="human_task_blocking"),
            nullable=False,
        ),
        sa.Column("completed_by", sa.UUID(), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["assignee_id"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["completed_by"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["guideline_run_id"], ["run.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_human_task_assignee_status", "human_task", ["assignee_id", "status"], unique=False
    )
    op.create_index(
        "ix_human_task_project_key_status",
        "human_task",
        ["project_id", "task_key", "status"],
        unique=False,
    )
    op.create_table(
        "claim_record",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("first_seen_guideline_id", sa.UUID(), nullable=False),
        sa.Column("claim_text", sa.Text(), nullable=False),
        sa.Column("normalized_text", sa.Text(), nullable=False),
        sa.Column(
            "surface_forms",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "claim_type",
            postgresql.ENUM(
                "superlative",
                "comparative",
                "quantified",
                "certification",
                "guarantee",
                "endorsement",
                "pricing",
                "safety_regulatory",
                name="claim_type",
            ),
            nullable=False,
        ),
        sa.Column(
            "market_scope",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "languages",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        sa.Column(
            "observed_on",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("substantiation", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "evidence_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "risk_tier",
            postgresql.ENUM("low", "medium", "high", name="claim_risk_tier"),
            server_default="medium",
            nullable=False,
        ),
        sa.Column(
            "status",
            postgresql.ENUM(
                "unsupported",
                "pending_signoff",
                "approved",
                "rejected",
                "expired",
                "revoked",
                name="claim_status",
            ),
            server_default="unsupported",
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_signature_id", sa.UUID(), nullable=True),
        sa.Column("superseded_by", sa.UUID(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["current_signature_id"], ["claim_signature.id"], ondelete="SET NULL", use_alter=True
        ),
        sa.ForeignKeyConstraint(
            ["first_seen_guideline_id"], ["content_guideline.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["superseded_by"], ["claim_record.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_claim_record_project_status_expiry",
        "claim_record",
        ["project_id", "status", "expires_at"],
        unique=False,
    )
    op.create_index(
        "uq_claim_record_current",
        "claim_record",
        ["project_id", "normalized_text"],
        unique=True,
        postgresql_where=sa.text("superseded_by IS NULL"),
    )
    op.create_table(
        "human_task_handover",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("task_id", sa.UUID(), nullable=False),
        sa.Column("from_user", sa.UUID(), nullable=False),
        sa.Column("to_user", sa.UUID(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("performed_by", sa.UUID(), nullable=False),
        sa.Column(
            "voided_signature_ids",
            postgresql.ARRAY(sa.UUID()),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["from_user"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["performed_by"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["task_id"], ["human_task.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["to_user"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_human_task_handover_task", "human_task_handover", ["task_id"], unique=False)
    op.create_table(
        "rule_set",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("guideline_id", sa.UUID(), nullable=False),
        sa.Column("ruleset_version", sa.Text(), nullable=False),
        sa.Column("compiled", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("compiler_version", sa.Text(), nullable=False),
        sa.Column("constants_version", sa.Text(), nullable=False),
        sa.Column("rule_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("hash", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["guideline_id"], ["content_guideline.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("hash", name="uq_rule_set_hash"),
        sa.UniqueConstraint("project_id", "ruleset_version", name="uq_rule_set_project_version"),
    )
    op.create_table(
        "policy_amendment",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=True),
        sa.Column(
            "origin",
            postgresql.ENUM(
                "policy_watch", "claim_expiry", "disapproval", "manual", name="amendment_origin"
            ),
            nullable=False,
        ),
        sa.Column(
            "detected_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "change_kind",
            postgresql.ENUM(
                "mechanical",
                "substantive",
                "signature_affecting",
                "unclassified",
                name="amendment_change_kind",
            ),
            server_default="unclassified",
            nullable=False,
        ),
        sa.Column("diff", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("proposed_rule_changes", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "open",
                "needs_review",
                "applied",
                "dismissed",
                "auto_applied",
                name="amendment_status",
            ),
            server_default="open",
            nullable=False,
        ),
        sa.Column("applied_ruleset_id", sa.UUID(), nullable=True),
        sa.Column("voided_signature_ids", postgresql.ARRAY(sa.UUID()), nullable=True),
        sa.Column("reviewed_by", sa.UUID(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["applied_ruleset_id"], ["rule_set.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["reviewed_by"], ["user.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["source_id"], ["policy_source.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_policy_amendment_project_status",
        "policy_amendment",
        ["project_id", "status", sa.literal_column("detected_at DESC")],
        unique=False,
    )
    op.create_table(
        "disapproval_event",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("workspace_id", sa.UUID(), nullable=False),
        sa.Column("project_id", sa.UUID(), nullable=False),
        sa.Column("ad_resource_name", sa.Text(), nullable=False),
        sa.Column("campaign_ref", sa.Text(), nullable=True),
        sa.Column("asset_ref", sa.Text(), nullable=True),
        sa.Column("policy_topic", sa.Text(), nullable=False),
        sa.Column("policy_detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("learned_rule_id", sa.Text(), nullable=True),
        sa.Column("amendment_id", sa.UUID(), nullable=True),
        sa.Column(
            "status",
            postgresql.ENUM(
                "new", "rule_proposed", "rule_applied", "ignored", name="disapproval_status"
            ),
            server_default="new",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["amendment_id"], ["policy_amendment.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id",
            "ad_resource_name",
            "policy_topic",
            "observed_at",
            name="uq_disapproval_event_observation",
        ),
    )
    op.create_index(
        "ix_disapproval_event_project_status",
        "disapproval_event",
        ["project_id", "status"],
        unique=False,
    )
    # The two deferred foreign keys. Both are `use_alter` in the models, and
    # autogenerate emits *neither* of them for a table it is creating in the
    # same revision — it drops them silently rather than ordering them, so
    # they are written out here by hand. `alembic check` is what catches a
    # third one being added later and forgotten.
    op.create_foreign_key(
        "fk_content_guideline_ruleset_id",
        "content_guideline",
        "rule_set",
        ["ruleset_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_claim_record_current_signature_id",
        "claim_record",
        "claim_signature",
        ["current_signature_id"],
        ["id"],
        ondelete="SET NULL",
    )

    _install_triggers()


#: Every Postgres enum type `upgrade()` creates. `op.drop_table` does not drop
#: the types `op.create_table` created — the asymmetry is Alembic's, not ours —
#: so a downgrade that omits these leaves twelve orphan types behind and the
#: next `upgrade head` fails on "type already exists". Listed by hand rather
#: than discovered at runtime, so a later revision's enum cannot be dropped by
#: this one's downgrade.
ENUM_TYPES: tuple[str, ...] = (
    "guideline_status",
    "guideline_mode",
    "claim_type",
    "claim_risk_tier",
    "claim_status",
    "signature_method",
    "human_task_status",
    "human_task_blocking",
    "amendment_origin",
    "amendment_change_kind",
    "amendment_status",
    "disapproval_status",
)


#: Trigger a — a published guideline is frozen where it matters.
#:
#: `status`, `signature_stale` and `binding_superseded` stay writable on
#: purpose: a reassigned legal owner has to be able to flag a live version as
#: stale, and superseding one is how the next MAJOR is published. Freezing
#: those three as well would leave the living rulebook unable to say it has
#: stopped being current.
PUBLISHED_GUIDELINE_GUARD = """
CREATE OR REPLACE FUNCTION content_guideline_published_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'published' AND (
           NEW.payload       IS DISTINCT FROM OLD.payload
        OR NEW.markdown      IS DISTINCT FROM OLD.markdown
        OR NEW.ruleset_id    IS DISTINCT FROM OLD.ruleset_id
        OR NEW.version_major IS DISTINCT FROM OLD.version_major
        OR NEW.version_minor IS DISTINCT FROM OLD.version_minor
    ) THEN
        RAISE EXCEPTION
            'content_guideline % is published: payload, markdown, ruleset_id and '
            'the version numbers are immutable. Changes mint a new version.', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: Trigger b — a RuleSet never changes, with no exceptions at all.
#:
#: Stage 04 pins `ruleset_version` on every creative run so an asset can be
#: re-audited a year later against the rules that actually applied when it was
#: made. That is only true if the row cannot move, so this one has no
#: allow-list to argue about.
RULE_SET_GUARD = """
CREATE OR REPLACE FUNCTION rule_set_immutable_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'rule_set % is immutable: a compiled ruleset is never edited, it is '
        'recompiled into a new row.', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

#: Trigger c — a signature is append-only except for its void columns.
#:
#: Voiding has to stay writable because reassigning the legal owner voids every
#: signature the outgoing owner made. Everything else is the record of what one
#: named person attested to, and a correction is a new signature.
CLAIM_SIGNATURE_GUARD = """
CREATE OR REPLACE FUNCTION claim_signature_append_only_guard() RETURNS trigger AS $$
BEGIN
    IF ROW(NEW.*) IS DISTINCT FROM ROW(OLD.*) AND (
           NEW.id              IS DISTINCT FROM OLD.id
        OR NEW.workspace_id    IS DISTINCT FROM OLD.workspace_id
        OR NEW.project_id      IS DISTINCT FROM OLD.project_id
        OR NEW.signer_id       IS DISTINCT FROM OLD.signer_id
        OR NEW.claim_ids       IS DISTINCT FROM OLD.claim_ids
        OR NEW.set_hash        IS DISTINCT FROM OLD.set_hash
        OR NEW.decisions       IS DISTINCT FROM OLD.decisions
        OR NEW.statement       IS DISTINCT FROM OLD.statement
        OR NEW.method          IS DISTINCT FROM OLD.method
        OR NEW.reauth_token_id IS DISTINCT FROM OLD.reauth_token_id
        OR NEW.ip              IS DISTINCT FROM OLD.ip
        OR NEW.user_agent      IS DISTINCT FROM OLD.user_agent
        OR NEW.signed_at       IS DISTINCT FROM OLD.signed_at
        OR NEW.expires_at      IS DISTINCT FROM OLD.expires_at
        OR NEW.created_at      IS DISTINCT FROM OLD.created_at
    ) THEN
        RAISE EXCEPTION
            'claim_signature % is append-only: only voided_at, voided_by and '
            'void_reason may change. A correction is a new signature.', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

TRIGGERS: tuple[tuple[str, str, str], ...] = (
    ("content_guideline_published", "content_guideline", "content_guideline_published_guard"),
    ("rule_set_immutable", "rule_set", "rule_set_immutable_guard"),
    ("claim_signature_append_only", "claim_signature", "claim_signature_append_only_guard"),
)


def _install_triggers() -> None:
    for body in (PUBLISHED_GUIDELINE_GUARD, RULE_SET_GUARD, CLAIM_SIGNATURE_GUARD):
        op.execute(body)
    for trigger, table, function in TRIGGERS:
        op.execute(
            f"CREATE TRIGGER {trigger} BEFORE UPDATE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {function}()"
        )


def downgrade() -> None:
    for trigger, table, function in TRIGGERS:
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")

    # The FK first: it is what keeps the two tables from being dropped in
    # either order.
    op.drop_constraint("fk_content_guideline_ruleset_id", "content_guideline", type_="foreignkey")
    op.drop_constraint("fk_claim_record_current_signature_id", "claim_record", type_="foreignkey")

    # Back to Stage 02's equality. Safe only because every guideline run is
    # dropped with `content_guideline` above — a surviving one would violate it.
    op.drop_constraint("ck_run_guideline_has_bindings", "run", type_="check")
    op.drop_constraint("ck_run_plan_has_source", "run", type_="check")
    op.execute("DELETE FROM run WHERE stage = 'guideline'")
    op.create_check_constraint(
        "ck_run_plan_has_source", "run", "(stage = 'plan') = (source_run_id IS NOT NULL)"
    )
    op.drop_column("run", "bindings")

    op.drop_index("ix_disapproval_event_project_status", table_name="disapproval_event")
    op.drop_table("disapproval_event")
    op.drop_index("ix_policy_amendment_project_status", table_name="policy_amendment")
    op.drop_table("policy_amendment")
    op.drop_table("rule_set")
    op.drop_index("ix_human_task_handover_task", table_name="human_task_handover")
    op.drop_table("human_task_handover")
    op.drop_index(
        "uq_claim_record_current",
        table_name="claim_record",
        postgresql_where=sa.text("superseded_by IS NULL"),
    )
    op.drop_index("ix_claim_record_project_status_expiry", table_name="claim_record")
    op.drop_table("claim_record")
    op.drop_index("ix_human_task_project_key_status", table_name="human_task")
    op.drop_index("ix_human_task_assignee_status", table_name="human_task")
    op.drop_table("human_task")
    op.drop_index("ix_content_guideline_project_status", table_name="content_guideline")
    op.drop_table("content_guideline")
    op.drop_index(
        "uq_signoff_matrix_current",
        table_name="signoff_matrix",
        postgresql_where=sa.text("superseded_at IS NULL"),
    )
    op.drop_table("signoff_matrix")
    op.drop_index("ix_claim_signature_signer", table_name="claim_signature")
    op.drop_index("ix_claim_signature_project_signed", table_name="claim_signature")
    op.drop_table("claim_signature")
    op.drop_table("policy_source")

    # Last, and only now: a type cannot be dropped while a column still uses
    # it, so this has to follow every drop_table above rather than sit beside
    # the column drop it looks like it belongs with.
    for type_name in ENUM_TYPES:
        op.execute(f"DROP TYPE IF EXISTS {type_name}")
