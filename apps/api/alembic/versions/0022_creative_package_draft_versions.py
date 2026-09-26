"""Let a project hold more than one unreleased creative package.

Migration 0020 shipped `creative_package` with `version INTEGER NOT NULL` and
a table-level `UNIQUE (project_id, version)` — the shape 0014 already had to
take off `campaign_plan`, and for the same reason: `version` is minted at
release (Stage 04 PRD §12.4: `max(version) + 1`), so a package is written
with a placeholder first, and any two placeholders in one project collide.
Two packages in one project are the ordinary case — every creative run makes
one, and `blocked → draft: re-run` (§12.4) is what a rejection leads to.

So, exactly as 0014 did for plans:

* every **unreleased** package is `version = 0` — draft, blocked,
  ready_to_release;
* uniqueness is a **partial** index `WHERE version > 0`, so every version a
  release ever minted stays unique within its project, including after the
  package is superseded.

`version > 0` is therefore also the exact test for "was this package ever
released".

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

from alembic import op

revision: str = "0022"
down_revision: str | None = "0021"
branch_labels: str | None = None
depends_on: str | None = None

INDEX_NAME = "uq_creative_package_project_version_minted"
CONSTRAINT_NAME = "uq_creative_package_project_version"


def upgrade() -> None:
    # No writer existed before S4-P16 (4.7.1 was a stub); normalise anyway, so
    # a hand-made placeholder cannot block the index.
    op.execute(
        "UPDATE creative_package SET version = 0 "
        "WHERE status NOT IN ('released', 'superseded')"
    )
    op.drop_constraint(CONSTRAINT_NAME, "creative_package", type_="unique")
    op.execute(
        f"CREATE UNIQUE INDEX {INDEX_NAME} ON creative_package (project_id, version) "
        "WHERE version > 0"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
    # Only succeeds where no project holds two unreleased packages — the state
    # this revision exists to allow. Failing loudly beats discarding a package.
    op.create_unique_constraint(CONSTRAINT_NAME, "creative_package", ["project_id", "version"])
