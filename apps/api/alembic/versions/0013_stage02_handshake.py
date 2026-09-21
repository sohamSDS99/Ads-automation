"""Stage 02 — the handshake schema (Stage 02 PRD §7).

Three new tables, four altered columns, one generalised table. Nothing here
executes a plan; this revision is what makes a plan *representable*.

Four decisions are worth stating once, because the rest of Stage 02 depends on
all four:

* **`Run.stage` is on `run`, not in a second table.** A plan run is a run — it
  has nodes, evidence, approvals, a cost and an SSE channel, and every one of
  those already hangs off `run_id`. The `CHECK` is what keeps the two stages
  honest about each other: a plan run without the research run it consumes is
  not a plan run, and a research run that claims a source is a contradiction.
* **`source_run_id` cascades.** A plan run derived from a deleted research run
  cannot satisfy the `CHECK`, so `SET NULL` is not available and `RESTRICT`
  would deadlock the `project → run` cascade. Deleting research therefore
  deletes the plans built on it, which is also the only honest reading.
* **`Export` becomes polymorphic** rather than gaining a second nullable FK.
  `artifact_id` has no foreign key by construction; `artifact_type` is what
  says which table to look in. Existing download URLs resolve by `Export.id`
  and keep working.
* **Freezing is enforced in the database.** Stage 02 law 17 says a frozen plan
  is immutable; a trigger is the only place that statement is true regardless
  of which process is writing.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | None = None
depends_on: str | None = None

# Enum values are spelled out rather than imported from the models: a migration
# is a frozen snapshot and must not change when the models do (0001's rule).
RUN_STAGE = ("research", "plan")
CAMPAIGN_PLAN_STATUS = ("draft", "blocked", "ready_to_freeze", "frozen", "superseded")
EXPORT_ARTIFACT_TYPE = ("research_report", "campaign_plan")

#: `export_format` before and after. The downgrade needs the old list, and
#: writing it out beats reconstructing it from the new one.
EXPORT_FORMAT_OLD = ("pdf", "docx", "md", "json", "csv")
EXPORT_FORMAT_NEW = (*EXPORT_FORMAT_OLD, "editor_csv", "xlsx")

#: The freeze guard, as PRD §7.2 words it: payload, markdown and version are
#: sealed once `status = 'frozen'`. Everything else on the row — `updated_at`,
#: `source_superseded`, a later `status` transition — stays writable, because
#: a frozen plan whose source was re-accepted still has to be able to say so.
FREEZE_GUARD = """
CREATE OR REPLACE FUNCTION campaign_plan_freeze_guard() RETURNS trigger AS $$
BEGIN
    IF OLD.status = 'frozen' AND (
           NEW.payload  IS DISTINCT FROM OLD.payload
        OR NEW.markdown IS DISTINCT FROM OLD.markdown
        OR NEW.version  IS DISTINCT FROM OLD.version
    ) THEN
        RAISE EXCEPTION
            'campaign_plan % is frozen: payload, markdown and version are immutable. '
            'Changes require a new version from a new plan run.', OLD.id
            USING ERRCODE = 'restrict_violation';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;
"""


def _enum(name: str) -> postgresql.ENUM:
    """Reference a type this migration already created. Never creates one."""
    return postgresql.ENUM(name=name, create_type=False)


def upgrade() -> None:
    for name, values in (
        ("run_stage", RUN_STAGE),
        ("campaign_plan_status", CAMPAIGN_PLAN_STATUS),
        ("export_artifact_type", EXPORT_ARTIFACT_TYPE),
    ):
        rendered = ", ".join(f"'{value}'" for value in values)
        op.execute(f"CREATE TYPE {name} AS ENUM ({rendered})")

    _upgrade_run()
    _upgrade_approval()
    _create_research_acceptance()
    _create_campaign_plan()
    _create_plan_calc()
    _generalise_export()


def downgrade() -> None:
    _degeneralise_export()
    op.drop_table("plan_calc")
    op.execute("DROP TRIGGER IF EXISTS campaign_plan_freeze ON campaign_plan")
    op.execute("DROP FUNCTION IF EXISTS campaign_plan_freeze_guard()")
    op.drop_table("campaign_plan")
    op.drop_table("research_acceptance")

    op.drop_index("ix_approval_gate_status", table_name="approval")
    op.drop_column("approval", "recalc_state")
    op.drop_column("approval", "gate_key")

    op.execute("DROP INDEX ix_run_project_stage_started")
    op.drop_constraint("ck_run_plan_has_source", "run", type_="check")
    op.drop_column("run", "input_hash")
    op.drop_column("run", "source_run_id")
    op.drop_column("run", "stage")

    for name in ("export_artifact_type", "campaign_plan_status", "run_stage"):
        op.execute(f"DROP TYPE {name}")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def _upgrade_run() -> None:
    # Added nullable, backfilled, then tightened. Postgres would fill the
    # default in one statement, but the three-step version is what makes
    # "every pre-existing run reads 'research'" a line someone can point at.
    op.add_column("run", sa.Column("stage", _enum("run_stage"), nullable=True))
    op.execute("UPDATE run SET stage = 'research' WHERE stage IS NULL")
    op.alter_column("run", "stage", nullable=False, server_default="research")

    op.add_column(
        "run",
        sa.Column(
            "source_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.add_column("run", sa.Column("input_hash", sa.Text(), nullable=True))
    op.create_check_constraint(
        "ck_run_plan_has_source",
        "run",
        "(stage = 'plan') = (source_run_id IS NOT NULL)",
    )
    op.execute(
        "CREATE INDEX ix_run_project_stage_started ON run (project_id, stage, started_at DESC)"
    )


# ---------------------------------------------------------------------------
# approval
# ---------------------------------------------------------------------------


def _upgrade_approval() -> None:
    # 'R0' is deliberately not one of the research gates R1–R3: every existing
    # row predates gate keys, and labelling them R1 would be an invention. R0
    # reads as "a research gate from before gates were keyed", which is true.
    op.add_column(
        "approval",
        sa.Column("gate_key", sa.Text(), nullable=False, server_default="R0"),
    )
    op.add_column("approval", sa.Column("recalc_state", postgresql.JSONB(), nullable=True))
    op.create_index("ix_approval_gate_status", "approval", ["gate_key", "status"])


# ---------------------------------------------------------------------------
# new tables
# ---------------------------------------------------------------------------


def _create_research_acceptance() -> None:
    op.create_table(
        "research_acceptance",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "accepted_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "accepted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("launch_readiness_at_acceptance", sa.Text(), nullable=False),
        sa.Column("override_reason", sa.Text(), nullable=True),
        sa.Column(
            "superseded_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_acceptance.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    # Exactly one current acceptance per project. Partial, so the history of
    # superseded acceptances stays on the table rather than being deleted to
    # make room for the next one.
    op.create_index(
        "uq_research_acceptance_current",
        "research_acceptance",
        ["project_id"],
        unique=True,
        postgresql_where=sa.text("superseded_by IS NULL"),
    )
    op.create_index("ix_research_acceptance_project", "research_acceptance", ["project_id"])


def _create_campaign_plan() -> None:
    op.create_table(
        "campaign_plan",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
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
            "plan_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "acceptance_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("research_acceptance.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("schema_version", sa.Text(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", _enum("campaign_plan_status"), nullable=False, server_default="draft"),
        sa.Column(
            "payload", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("markdown", sa.Text(), nullable=False, server_default=""),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "frozen_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "frozen_approval_ids",
            postgresql.ARRAY(postgresql.UUID(as_uuid=True)),
            nullable=True,
        ),
        sa.Column(
            "source_superseded", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("project_id", "version", name="uq_campaign_plan_project_version"),
    )
    op.execute(
        "CREATE INDEX ix_campaign_plan_project_version ON campaign_plan (project_id, version DESC)"
    )

    op.execute(FREEZE_GUARD)
    op.execute(
        "CREATE TRIGGER campaign_plan_freeze BEFORE UPDATE ON campaign_plan "
        "FOR EACH ROW EXECUTE FUNCTION campaign_plan_freeze_guard()"
    )


def _create_plan_calc() -> None:
    op.create_table(
        "plan_calc",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "plan_run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("run.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("node_id", sa.Text(), nullable=False),
        sa.Column("formula_id", sa.Text(), nullable=False),
        sa.Column("calc_version", sa.Text(), nullable=False),
        sa.Column("inputs", postgresql.JSONB(), nullable=False),
        sa.Column("inputs_hash", sa.Text(), nullable=False),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "evidence_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("evidence.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "plan_run_id", "formula_id", "inputs_hash", name="uq_plan_calc_run_formula_inputs"
        ),
    )
    op.create_index("ix_plan_calc_run_node", "plan_calc", ["plan_run_id", "node_id"])


# ---------------------------------------------------------------------------
# export — one artifact column instead of one FK per artifact kind
# ---------------------------------------------------------------------------


def _generalise_export() -> None:
    for value in ("editor_csv", "xlsx"):
        op.execute(f"ALTER TYPE export_format ADD VALUE IF NOT EXISTS '{value}'")

    op.add_column(
        "export",
        sa.Column(
            "artifact_type",
            _enum("export_artifact_type"),
            nullable=False,
            server_default="research_report",
        ),
    )
    op.add_column("export", sa.Column("artifact_id", postgresql.UUID(as_uuid=True), nullable=True))
    op.execute("UPDATE export SET artifact_id = report_id")
    op.alter_column("export", "artifact_id", nullable=False)

    op.drop_index("ix_export_report_id", table_name="export")
    op.drop_column("export", "report_id")
    op.create_index("ix_export_artifact", "export", ["artifact_type", "artifact_id"])


def _degeneralise_export() -> None:
    # Plan exports have no `report` to point at, so they cannot survive the
    # reverse migration. Deleting them is the only truthful option: the
    # alternative is a dangling FK or an invented report id.
    op.execute("DELETE FROM export WHERE artifact_type = 'campaign_plan'")

    op.add_column(
        "export",
        sa.Column(
            "report_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("report.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.execute("UPDATE export SET report_id = artifact_id")
    op.alter_column("export", "report_id", nullable=False)

    op.drop_index("ix_export_artifact", table_name="export")
    op.drop_column("export", "artifact_id")
    op.drop_column("export", "artifact_type")
    op.create_index("ix_export_report_id", "export", ["report_id"])

    # `ALTER TYPE … DROP VALUE` does not exist. Rebuilding the type is the
    # documented way back, and is safe here because the rows that used the two
    # new values were deleted above.
    rendered = ", ".join(f"'{value}'" for value in EXPORT_FORMAT_OLD)
    op.execute("ALTER TYPE export_format RENAME TO export_format_old")
    op.execute(f"CREATE TYPE export_format AS ENUM ({rendered})")
    op.execute(
        "ALTER TABLE export ALTER COLUMN format TYPE export_format "
        "USING format::text::export_format"
    )
    op.execute("DROP TYPE export_format_old")
