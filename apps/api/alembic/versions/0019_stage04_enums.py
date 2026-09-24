"""Stage 04's enum labels and enum types, and nothing else.

The same split, for the same reason, as 0015: Postgres will not let a
transaction add an enum label and then write a row that uses it, so an
`ALTER TYPE ... ADD VALUE` sharing a revision with the tables that read the new
label fails at `upgrade head` on an empty database — and `upgrade head` is
Railway's `preDeployCommand`, which makes that the worst place to find out.

Everything below runs in an `autocommit_block()`, which is Alembic's spelling
of `isolation_level="AUTOCOMMIT"` for one block of one revision: the labels are
durable before 0020 begins.

**The type names are the database's, not the PRD's.** Stage 04 PRD §23 writes
`policy_amendment_origin`; migration 0016 named that type `amendment_origin`,
and an `ALTER TYPE` on a name that does not exist fails the deploy. The PRD's
name describes the column (`policy_amendment.origin`), this one is the type.

**The new types are named for their table, never *as* their table.** Every
Postgres table owns a composite type of the same name, so an enum called
`asset_decision` would collide with the `asset_decision` table 0020 creates.
Hence `asset_decision_choice`, `creative_asset_kind` and so on.

`IF NOT EXISTS` / the `pg_type` probe make every statement idempotent: a
half-applied upgrade that is retried must not fail on what it already did.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

from alembic import op

revision: str = "0019"
down_revision: str | None = "0018"
branch_labels: str | None = None
depends_on: str | None = None

#: (type, label) — new labels on types that already exist.
NEW_LABELS: tuple[tuple[str, str], ...] = (
    ("run_stage", "creative"),
    ("export_artifact_type", "creative_package"),
    ("export_format", "editor_zip"),
    ("amendment_origin", "creative_exception"),
)

#: (type, labels) — every new enum in Stage 04 PRD §7.2, in the PRD's order.
#: `db/models.py` declares the matching `StrEnum`s; `test_stage04_schema`
#: asserts the two agree label for label.
NEW_TYPES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "creative_asset_kind",
        (
            "headline",
            "long_headline",
            "description",
            "path",
            "sitelink",
            "callout",
            "structured_snippet",
            "promotion",
            "price",
            "lead_form",
            "business_name",
            "video_script",
            "image",
            "video",
            "logo",
        ),
    ),
    (
        "creative_asset_status",
        (
            "draft",
            "linted",
            "reserve",
            "awaiting_review",
            "approved",
            "rejected",
            "dropped",
            "awaiting_exception",
            "released",
        ),
    ),
    ("creative_asset_variant", ("A", "B")),
    ("generation_modality", ("image", "video")),
    (
        "generation_status",
        (
            "queued",
            "submitting",
            "submitted",
            "in_progress",
            "completed",
            "failed",
            "cancelled",
            "expired",
            "timed_out",
            "blocked_by_budget",
            "unknown_submit_state",
        ),
    ),
    (
        "media_artifact_role",
        (
            "candidate",
            "master",
            "rendition",
            "clip",
            "preview",
            "thumbnail",
            "poster",
            "frame_sample",
        ),
    ),
    (
        "media_artifact_derivation",
        ("native", "relaid", "crop", "composited", "encoded", "ingested"),
    ),
    ("asset_decision_choice", ("approve", "reject", "regenerate")),
    ("creative_exception_kind", ("new_claim", "disclaimer", "image_right")),
    ("creative_exception_status", ("open", "cleared", "rejected", "withdrawn")),
    ("landing_audit_verdict", ("ok", "needs_change", "blocking_for_launch", "unreachable")),
    ("preview_device", ("mobile", "desktop")),
    ("preview_verdict", ("pass", "warning", "blocking", "unavailable")),
    (
        "creative_package_status",
        ("draft", "blocked", "ready_to_release", "released", "superseded"),
    ),
    ("media_reference_kind", ("product_reference", "style_reference")),
    ("media_reference_origin", ("own", "licensed", "third_party")),
)


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for type_name, label in NEW_LABELS:
            op.execute(f"ALTER TYPE {type_name} ADD VALUE IF NOT EXISTS '{label}'")
        for type_name, labels in NEW_TYPES:
            values = ", ".join(f"'{label}'" for label in labels)
            # Postgres has no CREATE TYPE IF NOT EXISTS; the probe is the
            # idempotent form.
            op.execute(
                # Every name and label is a module constant above, never input.
                "DO $$ BEGIN "  # noqa: S608
                f"IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = '{type_name}') THEN "
                f"CREATE TYPE {type_name} AS ENUM ({values}); "
                "END IF; END $$"
            )


def downgrade() -> None:
    # Postgres cannot drop a label from an enum type. Undoing the four
    # ALTER TYPEs means recreating each type without its Stage 04 label,
    # rewriting every column that uses it and dropping the old type — a data
    # migration, not a downgrade. The sixteen new types *could* be dropped, but
    # dropping half a revision leaves a schema no revision describes.
    raise NotImplementedError(
        "Postgres cannot drop an enum label, so this revision is irreversible. "
        "Undoing it means recreating each of "
        f"{', '.join(sorted({t for t, _ in NEW_LABELS}))} without the Stage 04 "
        "labels, rewriting every column that uses them, and dropping the old "
        "types — a data migration, not a downgrade. Downgrade 0020 instead: it "
        "removes everything that reads these labels and types, which leaves "
        "them unused and harmless."
    )
