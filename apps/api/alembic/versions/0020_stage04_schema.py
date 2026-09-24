"""Stage 04 — the creative schema (Stage 04 PRD §7).

Ten tables, three new columns, one new CHECK, four triggers, one uniform-scale
CHECK. The lines worth reading first:

**The `run` CHECK is added, not rewritten.** Stage 03 rewrote
`ck_run_plan_has_source` because its equality excluded guideline runs. Stage 04
needs only the implication "a creative run names its source" — the frozen
plan's `plan_run_id` — and that is a constraint of its own,
`ck_run_creative_has_source`. The two existing constraints are not touched,
which is what keeps `stage='plan'` with a NULL source rejected and
`stage='guideline'` with a NULL source accepted.

**The enum types belong to 0019.** Every column below references a type 0019
created, with `create_type=False`, and `downgrade()` here drops none of them:
0019 is irreversible, so a type dropped here would leave `upgrade head` with a
revision history claiming it exists when it does not.

**Four triggers carry four immutability promises** that no code path can be
trusted to keep on its own: an approved brief, a released asset, a released
package and a reviewer's decision.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0020"
down_revision: str | None = "0019"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _enum(name: str) -> postgresql.ENUM:
    # 0019 created every one of these. `create_type=False` is what stops
    # `create_table` issuing a second CREATE TYPE and failing on it.
    return postgresql.ENUM(name=name, create_type=False)


def _id() -> sa.Column[sa.UUID]:
    return sa.Column("id", sa.UUID(), nullable=False)


def _workspace() -> sa.Column[sa.UUID]:
    return sa.Column("workspace_id", sa.UUID(), nullable=False)


def _project() -> sa.Column[sa.UUID]:
    return sa.Column("project_id", sa.UUID(), nullable=False)


def _run() -> sa.Column[sa.UUID]:
    return sa.Column("creative_run_id", sa.UUID(), nullable=False)


def _ts(name: str) -> sa.Column[sa.DateTime]:
    return sa.Column(
        name, sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
    )


def _jsonb(name: str, *, nullable: bool = False) -> sa.Column[postgresql.JSONB]:
    if nullable:
        return sa.Column(name, postgresql.JSONB(astext_type=sa.Text()), nullable=True)
    return sa.Column(
        name,
        postgresql.JSONB(astext_type=sa.Text()),
        server_default=sa.text("'{}'::jsonb"),
        nullable=False,
    )


def _uuids(name: str) -> sa.Column[postgresql.ARRAY[sa.UUID]]:
    return sa.Column(
        name,
        postgresql.ARRAY(sa.UUID()),
        server_default=sa.text("'{}'::uuid[]"),
        nullable=False,
    )


def _workspace_project_fks() -> list[sa.ForeignKeyConstraint]:
    return [
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["project_id"], ["project.id"], ondelete="CASCADE"),
    ]


def _run_fk() -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(["creative_run_id"], ["run.id"], ondelete="CASCADE")


def upgrade() -> None:
    # -- the three altered tables ------------------------------------------
    op.add_column("run", sa.Column("pins", postgresql.JSONB(astext_type=sa.Text())))
    # ADDED beside ck_run_plan_has_source and ck_run_guideline_has_bindings,
    # which stay exactly as 0016 left them.
    op.create_check_constraint(
        "ck_run_creative_has_source",
        "run",
        "stage <> 'creative' OR source_run_id IS NOT NULL",
    )
    op.add_column("approval", sa.Column("draft_state", postgresql.JSONB(astext_type=sa.Text())))
    op.add_column(
        "claim_record",
        sa.Column("origin", sa.Text(), server_default="harvest", nullable=False),
    )

    # -- the ten new tables, parents first ---------------------------------
    op.create_table(
        "creative_brief",
        _id(),
        _workspace(),
        _project(),
        _run(),
        sa.Column("schema_version", sa.Text(), nullable=False),
        _jsonb("payload"),
        sa.Column("markdown", sa.Text(), server_default="", nullable=False),
        sa.Column("brief_hash", sa.Text(), nullable=False),
        sa.Column("approval_id", sa.UUID(), nullable=True),
        sa.Column("approved_hash", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        *_workspace_project_fks(),
        _run_fk(),
        sa.ForeignKeyConstraint(["approval_id"], ["approval.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creative_run_id"),
    )

    op.create_table(
        "media_reference",
        _id(),
        _workspace(),
        _project(),
        sa.Column("kind", _enum("media_reference_kind"), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("product_ref", sa.Text(), nullable=True),
        sa.Column("origin", _enum("media_reference_origin"), nullable=False),
        sa.Column("rights_statement", sa.Text(), nullable=False),
        sa.Column("attested_by", sa.UUID(), nullable=False),
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("retired_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        *_workspace_project_fks(),
        sa.ForeignKeyConstraint(["attested_by"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("project_id", "sha256", name="uq_media_reference_project_sha256"),
    )

    op.create_table(
        "creative_asset",
        _id(),
        _workspace(),
        _project(),
        _run(),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("campaign_ref", sa.Text(), nullable=False),
        sa.Column("ad_group_ref", sa.Text(), nullable=True),
        sa.Column("ad_ref", sa.Text(), nullable=True),
        sa.Column("kind", _enum("creative_asset_kind"), nullable=False),
        sa.Column("surface", sa.Text(), nullable=False),
        sa.Column("variant", _enum("creative_asset_variant"), nullable=True),
        sa.Column("category", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=True),
        _jsonb("fields"),
        _uuids("claim_ids"),
        _jsonb("offer_binding", nullable=True),
        sa.Column("pin_position", sa.Text(), nullable=True),
        sa.Column("generated_by_ai", sa.Boolean(), nullable=False),
        sa.Column("status", _enum("creative_asset_status"), server_default="draft", nullable=False),
        _jsonb("lint", nullable=True),
        sa.Column("ruleset_version", sa.Text(), nullable=True),
        _jsonb("lineage"),
        sa.Column("content_hash", sa.Text(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        *_workspace_project_fks(),
        _run_fk(),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_creative_asset_run_kind_status",
        "creative_asset",
        ["creative_run_id", "kind", "status"],
    )
    op.create_index(
        "ix_creative_asset_run_ad_group", "creative_asset", ["creative_run_id", "ad_group_ref"]
    )

    op.create_table(
        "generation_job",
        _id(),
        _workspace(),
        _project(),
        _run(),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=True),
        sa.Column("round", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("modality", _enum("generation_modality"), nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("provider_tag", sa.Text(), nullable=True),
        sa.Column("capability_hash", sa.Text(), nullable=False),
        _jsonb("request"),
        # Law 37. Never drop this to "allow retries" (PRD §7.5 note 4).
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("openrouter_job_id", sa.Text(), nullable=True),
        sa.Column("status", _enum("generation_status"), server_default="queued", nullable=False),
        sa.Column("estimate_usd", sa.Numeric(12, 4), nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 4), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("polls", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("next_poll_at", sa.DateTime(timezone=True), nullable=True),
        _jsonb("error", nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        *_workspace_project_fks(),
        _run_fk(),
        sa.ForeignKeyConstraint(["asset_id"], ["creative_asset.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("openrouter_job_id"),
    )
    op.create_index("ix_generation_job_run_status", "generation_job", ["creative_run_id", "status"])
    op.create_index(
        "ix_generation_job_status_next_poll", "generation_job", ["status", "next_poll_at"]
    )

    op.create_table(
        "media_artifact",
        _id(),
        _workspace(),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("job_id", sa.UUID(), nullable=True),
        sa.Column("role", _enum("media_artifact_role"), nullable=False),
        sa.Column("storage_path", sa.Text(), nullable=False),
        sa.Column("media_type", sa.Text(), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("aspect_ratio", sa.Text(), nullable=False),
        sa.Column("derivation", _enum("media_artifact_derivation"), nullable=False),
        sa.Column("derived_from", sa.UUID(), nullable=True),
        _jsonb("transform", nullable=True),
        _jsonb("probe"),
        _jsonb("disclosure", nullable=True),
        _ts("created_at"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspace.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["creative_asset.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["job_id"], ["generation_job.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["derived_from"], ["media_artifact.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        # Law 39 — RELAY OUT, NEVER STRETCH. A transform that scales the two
        # axes differently is a stretched image, and this is the one place
        # that cannot be argued with.
        sa.CheckConstraint(
            "transform IS NULL OR (transform->>'sx') = (transform->>'sy')",
            name="ck_media_artifact_uniform_scale",
        ),
    )

    op.create_table(
        "asset_decision",
        _id(),
        sa.Column("approval_id", sa.UUID(), nullable=False),
        sa.Column("asset_id", sa.UUID(), nullable=False),
        sa.Column("round", sa.Integer(), nullable=False),
        sa.Column("decision", _enum("asset_decision_choice"), nullable=False),
        _jsonb("checklist"),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("model_override", sa.Text(), nullable=True),
        _jsonb("params_override", nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=False),
        _ts("decided_at"),
        sa.ForeignKeyConstraint(["approval_id"], ["approval.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["asset_id"], ["creative_asset.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_id", "asset_id", name="uq_asset_decision_approval_asset"),
    )

    op.create_table(
        "creative_exception",
        _id(),
        _workspace(),
        _project(),
        _run(),
        sa.Column("kind", _enum("creative_exception_kind"), nullable=False),
        sa.Column("subject_text", sa.Text(), nullable=True),
        _uuids("asset_ids"),
        sa.Column("occurrences", sa.Integer(), server_default=sa.text("1"), nullable=False),
        _jsonb("proposed"),
        _uuids("evidence_ids"),
        _uuids("fallback_asset_ids"),
        sa.Column(
            "status", _enum("creative_exception_status"), server_default="open", nullable=False
        ),
        sa.Column("human_task_id", sa.UUID(), nullable=True),
        sa.Column("claim_record_id", sa.UUID(), nullable=True),
        sa.Column("signature_id", sa.UUID(), nullable=True),
        sa.Column("decided_by", sa.UUID(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column("set_hash", sa.Text(), nullable=True),
        sa.Column("reauth_token_id", sa.Text(), nullable=True),
        _ts("created_at"),
        _ts("updated_at"),
        *_workspace_project_fks(),
        _run_fk(),
        sa.ForeignKeyConstraint(["human_task_id"], ["human_task.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["claim_record_id"], ["claim_record.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["signature_id"], ["claim_signature.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["decided_by"], ["user.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "landing_page_audit",
        _id(),
        _run(),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("final_url", sa.Text(), nullable=True),
        sa.Column("http_status", sa.Integer(), nullable=True),
        sa.Column(
            "ad_group_refs",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'::text[]"),
            nullable=False,
        ),
        _jsonb("metrics"),
        _jsonb("patch", nullable=True),
        sa.Column("verdict", _enum("landing_audit_verdict"), nullable=False),
        _jsonb("screenshots"),
        _uuids("evidence_ids"),
        _ts("created_at"),
        _run_fk(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creative_run_id", "url", name="uq_landing_page_audit_run_url"),
    )

    op.create_table(
        "render_preview",
        _id(),
        _run(),
        sa.Column("ad_ref", sa.Text(), nullable=False),
        sa.Column("device", _enum("preview_device"), nullable=False),
        _jsonb("combination"),
        sa.Column("storage_path", sa.Text(), nullable=True),
        _jsonb("dom_metrics"),
        _jsonb("spec_diff"),
        _jsonb("visual_diff", nullable=True),
        sa.Column("template_version", sa.Text(), nullable=False),
        sa.Column("verdict", _enum("preview_verdict"), nullable=False),
        _ts("created_at"),
        _run_fk(),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "creative_package",
        _id(),
        _workspace(),
        _project(),
        _run(),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "status", _enum("creative_package_status"), server_default="draft", nullable=False
        ),
        sa.Column("plan_id", sa.UUID(), nullable=False),
        sa.Column("plan_version", sa.Integer(), nullable=False),
        sa.Column("guideline_id", sa.UUID(), nullable=False),
        sa.Column("ruleset_version", sa.Text(), nullable=False),
        sa.Column("brief_hash", sa.Text(), nullable=True),
        _jsonb("payload"),
        _jsonb("manifest", nullable=True),
        sa.Column("package_hash", sa.Text(), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.UUID(), nullable=True),
        sa.Column("released_approval_ids", postgresql.ARRAY(sa.UUID()), nullable=True),
        sa.Column("plan_superseded", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "ruleset_superseded", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("cost_usd", sa.Numeric(12, 4), server_default=sa.text("0"), nullable=False),
        _ts("created_at"),
        _ts("updated_at"),
        *_workspace_project_fks(),
        _run_fk(),
        # NO ACTION, not RESTRICT — see `CreativePackage` in db/models.py.
        sa.ForeignKeyConstraint(["plan_id"], ["campaign_plan.id"]),
        sa.ForeignKeyConstraint(["guideline_id"], ["content_guideline.id"]),
        sa.ForeignKeyConstraint(["released_by"], ["user.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("creative_run_id"),
        sa.UniqueConstraint("project_id", "version", name="uq_creative_package_project_version"),
    )

    _install_triggers()


#: Trigger a — once G7 approved a brief, the brief is what was approved.
#:
#: `approval_id` and `approved_hash` themselves stay writable: they are how the
#: approval is recorded, and the guard reads OLD, so the statement that sets
#: `approved_hash` is never blocked by it.
CREATIVE_BRIEF_GUARD = """
CREATE OR REPLACE FUNCTION creative_brief_approved_freeze_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.approved_hash IS NOT NULL AND (
           NEW.payload    IS DISTINCT FROM OLD.payload
        OR NEW.markdown   IS DISTINCT FROM OLD.markdown
        OR NEW.brief_hash IS DISTINCT FROM OLD.brief_hash
    ) THEN
        RAISE EXCEPTION
            'creative_brief % was approved at G7: payload, markdown and brief_hash '
            'are immutable. A changed brief is a new approval.', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: Trigger b — a released asset never moves, with no allow-list at all.
CREATIVE_ASSET_GUARD = """
CREATE OR REPLACE FUNCTION creative_asset_frozen_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.frozen_at IS NOT NULL THEN
        RAISE EXCEPTION
            'creative_asset % was frozen at release: it is immutable. A changed '
            'asset belongs to a new package version.', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: Trigger c — a released package: only `status` and the two `*_superseded`
#: banners may move (Law 42, PRD §4.5).
#:
#: Written as "everything except" over the whole row rather than as a list of
#: frozen columns, so a column a later phase adds is frozen by default instead
#: of by someone remembering. `updated_at` is excused with the three because
#: the ORM stamps it on every UPDATE: without it the permitted change could
#: not be made through the application at all.
CREATIVE_PACKAGE_GUARD = """
CREATE OR REPLACE FUNCTION creative_package_released_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'released' AND (
        to_jsonb(NEW) - 'status' - 'plan_superseded' - 'ruleset_superseded' - 'updated_at'
        IS DISTINCT FROM
        to_jsonb(OLD) - 'status' - 'plan_superseded' - 'ruleset_superseded' - 'updated_at'
    ) THEN
        RAISE EXCEPTION
            'creative_package % is released: only status, plan_superseded and '
            'ruleset_superseded may change. A changed package is a new version.', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""

#: Trigger d — a reviewer's decision is append-only, full stop.
ASSET_DECISION_GUARD = """
CREATE OR REPLACE FUNCTION asset_decision_append_only_guard() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'asset_decision % is append-only: a changed decision is a new approval '
        'round, never an edited row.', OLD.id
        USING ERRCODE = 'restrict_violation';
END;
$$ LANGUAGE plpgsql;
"""

TRIGGERS: tuple[tuple[str, str, str], ...] = (
    ("creative_brief_approved_freeze", "creative_brief", "creative_brief_approved_freeze_guard"),
    ("creative_asset_frozen", "creative_asset", "creative_asset_frozen_guard"),
    ("creative_package_released", "creative_package", "creative_package_released_guard"),
    ("asset_decision_append_only", "asset_decision", "asset_decision_append_only_guard"),
)


def _install_triggers() -> None:
    for body in (
        CREATIVE_BRIEF_GUARD,
        CREATIVE_ASSET_GUARD,
        CREATIVE_PACKAGE_GUARD,
        ASSET_DECISION_GUARD,
    ):
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

    op.drop_table("creative_package")
    op.drop_table("render_preview")
    op.drop_table("landing_page_audit")
    op.drop_table("creative_exception")
    op.drop_table("asset_decision")
    op.drop_table("media_artifact")
    op.drop_index("ix_generation_job_status_next_poll", table_name="generation_job")
    op.drop_index("ix_generation_job_run_status", table_name="generation_job")
    op.drop_table("generation_job")
    op.drop_index("ix_creative_asset_run_ad_group", table_name="creative_asset")
    op.drop_index("ix_creative_asset_run_kind_status", table_name="creative_asset")
    op.drop_table("creative_asset")
    op.drop_table("media_reference")
    op.drop_table("creative_brief")

    # A creative run without its tables is a row no code can read, and an
    # export pointing at a package that no longer exists is a dangling
    # pointer. Same precedent as 0016's guideline runs. The labels themselves
    # stay: 0019 owns them and cannot be undone.
    op.execute("DELETE FROM export WHERE artifact_type = 'creative_package'")
    op.execute("DELETE FROM run WHERE stage = 'creative'")

    op.drop_column("claim_record", "origin")
    op.drop_column("approval", "draft_state")
    op.drop_constraint("ck_run_creative_has_source", "run", type_="check")
    op.drop_column("run", "pins")

    # No DROP TYPE: every enum used above belongs to 0019. See the module
    # docstring.
