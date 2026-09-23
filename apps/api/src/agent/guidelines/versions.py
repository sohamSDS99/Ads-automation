"""The `content_guideline` row's life before it is published (PRD §7.2, §12.4).

S3-P0 created the table, the trigger and the read routes. It did not create a
writer, and neither did S3-P2..P5 — so until this module, every
`ContentGuideline` row in the repository was written by a test fixture. That is
the gap this file closes, and the reason it has to close *here* is
`ClaimRecord.first_seen_guideline_id`: it is NOT NULL and it points at
`content_guideline.id`, so no claim can be registered until a guideline row
exists. 3.2.2 registers claims and runs a dozen nodes before 3.6.1, which means
the row cannot wait for the synthesis that fills it in.

**So the row is created at run start, empty.** `payload` and `markdown` are
nullable exactly for this: the guideline exists from the moment the run does,
holds `status='draft'`, and 3.6.1 fills it in. That also makes the Rulebook
Viewer, the claims register and the linter playground addressable mid-run
rather than only after the last node lands, which is what S3-P7 and S3-P8 need.

**The version a draft holds is provisional.** `uq_content_guideline_project_
version` is `(project_id, version_major, version_minor)`, so a draft has to
take *some* version, and publish re-mints it inside the same statement that
seals the row. The provisional number is almost always the one that sticks; it
does not when a draft is abandoned, and a published history that skips v2
because v2 was never finished is more honest than one that renumbers.

`next_major` counts **every** row, not only published ones. Counting only the
published ones would hand an abandoned draft's number to the next publish and
fail on the unique index — a 409 for a reason nobody could act on.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    ClaimRecord,
    ClaimSignature,
    ContentGuideline,
    GuidelineMode,
    GuidelineStatus,
    RuleSet,
)
from agent.export.guideline_contract import GUIDELINE_SCHEMA_VERSION

log = structlog.get_logger(__name__)


async def next_major(db: AsyncSession, project_id: uuid.UUID) -> int:
    """The next MAJOR for this project. 1 when there is nothing yet."""
    highest = (
        await db.execute(
            sa.select(sa.func.max(ContentGuideline.version_major)).where(
                ContentGuideline.project_id == project_id
            )
        )
    ).scalar_one_or_none()
    return int(highest or 0) + 1


async def for_run(db: AsyncSession, run_id: uuid.UUID) -> ContentGuideline | None:
    """This run's guideline row. `guideline_run_id` is UNIQUE, so at most one."""
    return (
        await db.execute(
            sa.select(ContentGuideline).where(ContentGuideline.guideline_run_id == run_id)
        )
    ).scalar_one_or_none()


async def ensure_draft(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    run_id: uuid.UUID,
    mode: GuidelineMode,
    bindings: dict[str, object] | None = None,
    unbound_inputs: list[str] | None = None,
) -> ContentGuideline:
    """The draft row for this run, created if it does not exist yet.

    Idempotent because it is called from two places that cannot coordinate: the
    entry route, which wants the row to exist before the first node runs, and
    3.6.1, which must not fail on a run started before this code shipped.
    Flushes rather than commits — the caller owns the transaction, and the entry
    route's is the one that also holds the `Run`.
    """
    existing = await for_run(db, run_id)
    if existing is not None:
        return existing

    row = ContentGuideline(
        workspace_id=workspace_id,
        project_id=project_id,
        guideline_run_id=run_id,
        schema_version=GUIDELINE_SCHEMA_VERSION,
        version_major=await next_major(db, project_id),
        version_minor=0,
        status=GuidelineStatus.DRAFT,
        mode=mode,
        bindings=dict(bindings or {}),
        unbound_inputs=list(unbound_inputs or []),
    )
    db.add(row)
    await db.flush()
    log.info(
        "guideline.draft_created",
        guideline_id=str(row.id),
        run_id=str(run_id),
        version=f"{row.version_major}.{row.version_minor}",
    )
    return row


async def claims_with_signatures(
    db: AsyncSession, project_id: uuid.UUID
) -> list[tuple[ClaimRecord, ClaimSignature | None]]:
    """Every current claim in the project with its signature, if it has one.

    An outer join, and it has to be: a rejected or expired claim carries no
    signature and still belongs in the index — `ref_status` turns it into the
    blocking entry that stops copy asserting it. An inner join here would make a
    rejected claim invisible to the linter, which reads as "never mentioned"
    rather than "explicitly forbidden".

    Superseded rows are excluded. `uq_claim_record_current` already makes them
    the only duplicates possible on `(project_id, normalized_text)`, and a
    superseded row in the index would licence text its replacement may have
    rejected.
    """
    result = await db.execute(
        sa.select(ClaimRecord, ClaimSignature)
        .outerjoin(ClaimSignature, ClaimSignature.id == ClaimRecord.current_signature_id)
        .where(ClaimRecord.project_id == project_id, ClaimRecord.superseded_by.is_(None))
        .order_by(ClaimRecord.id)
    )
    return [(row[0], row[1]) for row in result.all()]


async def current_ruleset(db: AsyncSession, guideline: ContentGuideline) -> RuleSet | None:
    """The ruleset that actually governs this guideline right now.

    **Not `guideline.ruleset_id`.** That column records what the guideline was
    published *with* and never moves again — the trigger forbids it. An
    amendment mints `v{major}.{minor+1}` as a new `rule_set` row and leaves the
    guideline untouched (`policy/lifecycle.mint_minor` explains why at length),
    so after one mechanical amendment the governing ruleset and the published
    one are different rows. Stage 04 must be handed the governing one, or a
    policy change that auto-applied would never reach the creative it was
    applied for.

    Ordered by minor descending, parsed out of `ruleset_version` rather than
    stored separately, because `ruleset_version` is the pin and a second copy of
    the number is a second thing that can disagree.
    """
    rows = (
        (await db.execute(sa.select(RuleSet).where(RuleSet.guideline_id == guideline.id)))
        .scalars()
        .all()
    )
    if not rows:
        return None
    prefix = f"{guideline.version_major}."
    return max(rows, key=lambda row: (_minor_of(row.ruleset_version, prefix), row.created_at))


def _minor_of(ruleset_version: str, prefix: str) -> int:
    """The MINOR in `"{major}.{minor}+{digest}"`, or -1 if it is another major."""
    if not ruleset_version.startswith(prefix):
        return -1
    tail = ruleset_version[len(prefix) :].split("+", 1)[0]
    return int(tail) if tail.isdigit() else -1


def published_version_of(guideline: ContentGuideline, ruleset: RuleSet | None) -> str:
    """What a reader is told the version is. The ruleset's pin wins when there
    is one, because an amendment can have moved it past the guideline's own
    `version_minor` — which the trigger froze at publish."""
    if ruleset is not None:
        return ruleset.ruleset_version
    return f"{guideline.version_major}.{guideline.version_minor}"


def is_stale(guideline: ContentGuideline, *, now: datetime) -> bool:
    """Whether the published rulebook carries a caveat a reader must see.

    Not a blocker and never has been: §7.2 keeps `signature_stale` and
    `binding_superseded` as flags on a *published* row precisely so the version
    stays published and serving while the linter un-licenses what it must.
    """
    del now  # reserved: claim expiry is swept in S3-P9, not recomputed here
    return bool(guideline.signature_stale or guideline.binding_superseded)
