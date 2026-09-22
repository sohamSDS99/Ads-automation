"""Stage 03's database guarantees, asserted against the database (PRD §7).

Everything here is enforced by DDL rather than by a service layer, so every
test drives the ORM straight at Postgres. That is the point: a published
rulebook, the compiled program Stage 04 lints against and a named person's
signature are legal records, and a guarantee that lives in application code
ends the first time somebody writes a second path to the table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    ClaimSignature,
    ContentGuideline,
    GuidelineMode,
    GuidelineStatus,
    RuleSet,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
    SignatureMethod,
)

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(UTC)


async def _guideline_run(
    db: AsyncSession, *, workspace_id: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> Run:
    run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=actor,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.QUEUED,
        stage=RunStage.GUIDELINE,
        bindings={},
    )
    db.add(run)
    await db.flush()
    return run


async def _published(
    db: AsyncSession,
    *,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    major: int = 1,
) -> ContentGuideline:
    run = await _guideline_run(db, workspace_id=workspace_id, project_id=project_id, actor=actor)
    guideline = ContentGuideline(
        workspace_id=workspace_id,
        project_id=project_id,
        guideline_run_id=run.id,
        schema_version="1.0",
        version_major=major,
        version_minor=0,
        status=GuidelineStatus.PUBLISHED,
        mode=GuidelineMode.STANDALONE,
        bindings={},
        unbound_inputs=["research", "plan"],
        payload={"executive_summary": "as published"},
        markdown="# published",
        published_at=_now(),
        published_by=actor,
    )
    db.add(guideline)
    await db.flush()
    return guideline


# ---------------------------------------------------------------------------
# the run CHECK constraints — §23's "single most important line in the migration"
# ---------------------------------------------------------------------------


async def test_a_guideline_run_needs_no_source_run(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """Law 21, at the schema level. Stage 02's CHECK forbade exactly this."""
    run = await _guideline_run(
        db, workspace_id=workspace_id, project_id=project_id, actor=admin_user.id
    )
    assert run.source_run_id is None
    assert run.stage is RunStage.GUIDELINE


async def test_a_guideline_run_may_also_carry_a_source_run(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """A research binding writes `source_run_id`, which Stage 02's equality —
    "only a plan run may have a source" — would have rejected."""
    research = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.RESEARCH,
    )
    db.add(research)
    await db.flush()

    bound = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.QUEUED,
        stage=RunStage.GUIDELINE,
        source_run_id=research.id,
        bindings={"research_run_id": str(research.id)},
    )
    db.add(bound)
    await db.flush()
    assert bound.source_run_id == research.id


async def test_a_plan_run_still_needs_a_source_run(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """The half of Stage 02's CHECK that was load-bearing must survive intact."""
    db.add(
        Run(
            workspace_id=workspace_id,
            project_id=project_id,
            triggered_by=admin_user.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.QUEUED,
            stage=RunStage.PLAN,
            source_run_id=None,
        )
    )
    with pytest.raises(IntegrityError, match="ck_run_plan_has_source"):
        await db.flush()


async def test_a_guideline_run_needs_bindings(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """NULL cannot be told apart from "nobody asked yet"; '{}' says "bound nothing"."""
    db.add(
        Run(
            workspace_id=workspace_id,
            project_id=project_id,
            triggered_by=admin_user.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.QUEUED,
            stage=RunStage.GUIDELINE,
            bindings=None,
        )
    )
    with pytest.raises(IntegrityError, match="ck_run_guideline_has_bindings"):
        await db.flush()


async def test_json_null_is_not_a_binding(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """The JSON scalar `null` must not satisfy the constraint either.

    This is the hole `bindings IS NOT NULL` alone leaves open, and it is not
    hypothetical: a plain `JSONB` column stores Python `None` as JSON `null`,
    which is a jsonb scalar and passes `IS NOT NULL`. The column is declared
    `none_as_null=True` so this mapper cannot produce it, and the CHECK
    requires a JSON object so no other client can either.
    """
    db.add(
        Run(
            workspace_id=workspace_id,
            project_id=project_id,
            triggered_by=admin_user.id,
            trigger=RunTrigger.MANUAL,
            status=RunStatus.QUEUED,
            stage=RunStage.GUIDELINE,
            bindings=sa.null(),
        )
    )
    with pytest.raises(IntegrityError, match="ck_run_guideline_has_bindings"):
        await db.flush()
    await db.rollback()


async def test_a_json_scalar_is_rejected_by_the_database_not_the_mapper(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """Straight SQL, so the guarantee is the database's and not the ORM's."""
    with pytest.raises(IntegrityError, match="ck_run_guideline_has_bindings"):
        await db.execute(
            sa.text(
                "INSERT INTO run (id, workspace_id, project_id, triggered_by, trigger, "
                "status, mode, stage, bindings) VALUES (gen_random_uuid(), :w, :p, :u, "
                "'manual', 'queued', 'full', 'guideline', 'null'::jsonb)"
            ),
            {"w": workspace_id, "p": project_id, "u": admin_user.id},
        )
    await db.rollback()


# ---------------------------------------------------------------------------
# trigger a — a published guideline is immutable where it matters
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("payload", {"executive_summary": "rewritten after the fact"}),
        ("markdown", "# rewritten"),
        ("version_major", 9),
        ("version_minor", 9),
    ],
)
async def test_update_to_a_published_guideline_raises(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    column: str,
    value: Any,
) -> None:
    guideline = await _published(
        db, workspace_id=workspace_id, project_id=project_id, actor=admin_user.id
    )
    with pytest.raises(DBAPIError, match="is published"):
        await db.execute(
            sa.update(ContentGuideline)
            .where(ContentGuideline.id == guideline.id)
            .values(**{column: value})
        )
    await db.rollback()


async def test_a_published_guideline_may_still_be_marked_stale(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """`status`, `signature_stale` and `binding_superseded` must stay writable.

    A reassigned legal owner has to be able to flag a live version without
    rewriting it — freezing those three too would make the living rulebook
    unable to say that it has gone stale.
    """
    guideline = await _published(
        db, workspace_id=workspace_id, project_id=project_id, actor=admin_user.id
    )
    await db.execute(
        sa.update(ContentGuideline)
        .where(ContentGuideline.id == guideline.id)
        .values(signature_stale=True, binding_superseded=True, status=GuidelineStatus.SUPERSEDED)
    )
    await db.flush()
    refreshed = await db.get(ContentGuideline, guideline.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.signature_stale is True
    assert refreshed.status is GuidelineStatus.SUPERSEDED


async def test_an_unpublished_guideline_is_freely_editable(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """The trigger reads the *existing* row, so a draft is untouched by it."""
    run = await _guideline_run(
        db, workspace_id=workspace_id, project_id=project_id, actor=admin_user.id
    )
    draft = ContentGuideline(
        workspace_id=workspace_id,
        project_id=project_id,
        guideline_run_id=run.id,
        schema_version="1.0",
        version_major=0,
        version_minor=0,
        status=GuidelineStatus.DRAFT,
        mode=GuidelineMode.STANDALONE,
        payload={"executive_summary": "draft"},
    )
    db.add(draft)
    await db.flush()
    await db.execute(
        sa.update(ContentGuideline)
        .where(ContentGuideline.id == draft.id)
        .values(payload={"executive_summary": "edited"}, markdown="# edited")
    )
    await db.flush()


# ---------------------------------------------------------------------------
# trigger b — a RuleSet never changes at all
# ---------------------------------------------------------------------------


async def test_every_update_to_a_ruleset_raises(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """No exceptions, not even a harmless-looking one.

    An asset linted against `2.3+a91c4f7e` must be re-auditable against exactly
    those rules a year later, and that is only true if the row cannot move.
    """
    guideline = await _published(
        db, workspace_id=workspace_id, project_id=project_id, actor=admin_user.id
    )
    ruleset = RuleSet(
        workspace_id=workspace_id,
        project_id=project_id,
        guideline_id=guideline.id,
        ruleset_version="1.0+deadbeef",
        compiled={"rules": []},
        compiler_version="0.1.0",
        constants_version="2026.09.1",
        rule_count=0,
        hash="deadbeef",
    )
    db.add(ruleset)
    await db.flush()

    with pytest.raises(DBAPIError, match="immutable"):
        await db.execute(sa.update(RuleSet).where(RuleSet.id == ruleset.id).values(rule_count=1))
    await db.rollback()


# ---------------------------------------------------------------------------
# trigger c — a signature is append-only except for its void columns
# ---------------------------------------------------------------------------


async def _signature(
    db: AsyncSession, *, workspace_id: uuid.UUID, project_id: uuid.UUID, signer: uuid.UUID
) -> ClaimSignature:
    signature = ClaimSignature(
        workspace_id=workspace_id,
        project_id=project_id,
        signer_id=signer,
        claim_ids=[],
        set_hash="0" * 64,
        decisions=[],
        statement="I confirm these claims are legally safe to run.",
        method=SignatureMethod.STEP_UP_PASSWORD,
        reauth_token_id="tok_1",
        expires_at=_now() + timedelta(days=365),
    )
    db.add(signature)
    await db.flush()
    return signature


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("decisions", [{"claim_id": "x", "decision": "approved"}]),
        ("set_hash", "1" * 64),
        ("statement", "I confirm something else entirely."),
        ("expires_at", None),
    ],
)
async def test_claim_signature_is_append_only(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    column: str,
    value: Any,
) -> None:
    """A correction is a new signature, never an edit (PRD §13)."""
    signature = await _signature(
        db, workspace_id=workspace_id, project_id=project_id, signer=admin_user.id
    )
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            sa.update(ClaimSignature)
            .where(ClaimSignature.id == signature.id)
            .values(**{column: value})
        )
    await db.rollback()


async def test_a_signature_may_be_voided(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """Voiding is the one write a signature accepts, and it is additive.

    Reassigning the legal owner voids every signature they made, so if this
    were frozen too the reassignment path would have nowhere to write.
    """
    signature = await _signature(
        db, workspace_id=workspace_id, project_id=project_id, signer=admin_user.id
    )
    await db.execute(
        sa.update(ClaimSignature)
        .where(ClaimSignature.id == signature.id)
        .values(voided_at=_now(), voided_by=admin_user.id, void_reason="legal owner reassigned")
    )
    await db.flush()
    refreshed = await db.get(ClaimSignature, signature.id, populate_existing=True)
    assert refreshed is not None
    assert refreshed.voided_at is not None
    assert refreshed.void_reason == "legal owner reassigned"
