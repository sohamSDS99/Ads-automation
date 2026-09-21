"""Let a project hold more than one unfrozen plan.

Migration 0013 shipped `campaign_plan` with `version INTEGER NOT NULL` and a
table-level `UNIQUE (project_id, version)`. Nothing wrote a plan row until
S2-P5b, so nothing exercised it, and the shape has a defect: **the second
unfrozen plan in a project cannot be inserted at all.**

`version` is minted at freeze (Stage 02 PRD §12.2), so every plan is written
with a placeholder first. Whatever that placeholder is, two of them in one
project collide on the unique constraint. And a second plan is not an edge
case — §12.2's own lifecycle has `blocked → draft: re-run`, which is what
happens the first time an approver rejects a gate.

The fix keeps the guarantee the constraint was there for and drops the part
that was never wanted:

* every **unfrozen** plan is `version = 0` — draft, blocked, ready_to_freeze;
* uniqueness is enforced by a **partial** index `WHERE version > 0`, so every
  version the freeze ever minted stays unique within its project, including
  after the plan is superseded and its status is no longer `frozen`.

`version > 0` is therefore also the exact test for "has this plan been
frozen", which is what the Plan Viewer's history list renders as `—`.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

from alembic import op

revision: str = "0014"
down_revision: str | None = "0013"
branch_labels: str | None = None
depends_on: str | None = None

INDEX_NAME = "uq_campaign_plan_project_version_minted"
CONSTRAINT_NAME = "uq_campaign_plan_project_version"


def upgrade() -> None:
    # No rows exist in practice — S2-P5b is the first writer — but a
    # placeholder-versioned row from a hand-run experiment would block the
    # partial index, so normalise before creating it.
    op.execute("UPDATE campaign_plan SET version = 0 WHERE status <> 'frozen'")
    op.drop_constraint(CONSTRAINT_NAME, "campaign_plan", type_="unique")
    op.execute(
        f"CREATE UNIQUE INDEX {INDEX_NAME} ON campaign_plan (project_id, version) WHERE version > 0"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    # Restoring the table constraint can only succeed where no project holds
    # two unfrozen plans, which is precisely the state this migration exists
    # to make reachable. Failing loudly beats silently discarding a plan.
    op.create_unique_constraint(CONSTRAINT_NAME, "campaign_plan", ["project_id", "version"])
