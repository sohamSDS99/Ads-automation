"""Stage 04's database guarantees, asserted against the database (PRD §7).

The same posture as `test_stage03_schema`: every guarantee here is DDL, so
every test drives straight at Postgres. An approved brief, a released asset, a
released package and a reviewer's decision are records Stage 05 and an auditor
will both read, and a promise kept only by application code ends the first
time somebody writes a second path to the table.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    STAGE04_ENUMS,
    Approval,
    ApprovalRequiredRole,
    AssetDecision,
    AssetDecisionChoice,
    CampaignPlan,
    CampaignPlanStatus,
    ClaimRecord,
    ContentGuideline,
    CreativeAsset,
    CreativeAssetKind,
    CreativeBrief,
    CreativePackage,
    CreativePackageStatus,
    GenerationJob,
    GenerationModality,
    GuidelineMode,
    GuidelineStatus,
    MediaArtifact,
    MediaArtifactDerivation,
    MediaArtifactRole,
    Report,
    ResearchAcceptance,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)

pytestmark = pytest.mark.asyncio


def _now() -> datetime:
    return datetime.now(UTC)


def _run(
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    stage: RunStage,
    source_run_id: uuid.UUID | None = None,
    **extra: Any,
) -> Run:
    return Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=actor,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.QUEUED,
        stage=stage,
        source_run_id=source_run_id,
        **extra,
    )


async def _creative_run(
    db: AsyncSession, ws: uuid.UUID, project: uuid.UUID, actor: uuid.UUID
) -> Run:
    research = _run(ws, project, actor, RunStage.RESEARCH)
    db.add(research)
    await db.flush()
    creative = _run(
        ws,
        project,
        actor,
        RunStage.CREATIVE,
        source_run_id=research.id,
        pins=[{"ruleset_version": "v1.0", "reason": "start", "at": _now().isoformat()}],
    )
    db.add(creative)
    await db.flush()
    return creative


async def _asset(db: AsyncSession, run: Run, **extra: Any) -> CreativeAsset:
    fields: dict[str, Any] = {
        "workspace_id": run.workspace_id,
        "project_id": run.project_id,
        "creative_run_id": run.id,
        "node_id": "4.2.1",
        "campaign_ref": "c1",
        "ad_group_ref": "ag1",
        "kind": CreativeAssetKind.HEADLINE,
        "surface": "search_headline",
        "text": "Safety data sheets, sorted",
        "generated_by_ai": True,
        "content_hash": "h1",
    }
    asset = CreativeAsset(**(fields | extra))
    db.add(asset)
    await db.flush()
    return asset


# ---------------------------------------------------------------------------
# enum types — 0019 and db/models.py must agree label for label
# ---------------------------------------------------------------------------


async def test_every_stage04_enum_matches_the_database(db: AsyncSession) -> None:
    for enum_cls, type_name in STAGE04_ENUMS:
        labels = (
            (
                await db.execute(
                    sa.text(
                        "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                        "WHERE t.typname = :name ORDER BY e.enumsortorder"
                    ),
                    {"name": type_name},
                )
            )
            .scalars()
            .all()
        )
        assert labels == [member.value for member in enum_cls], type_name


@pytest.mark.parametrize(
    ("type_name", "label"),
    [
        ("run_stage", "creative"),
        ("export_artifact_type", "creative_package"),
        ("export_format", "editor_zip"),
        ("amendment_origin", "creative_exception"),
    ],
)
async def test_the_four_added_labels_exist(db: AsyncSession, type_name: str, label: str) -> None:
    found = (
        await db.execute(
            sa.text(
                "SELECT 1 FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = :name AND e.enumlabel = :label"
            ),
            {"name": type_name, "label": label},
        )
    ).scalar_one_or_none()
    assert found == 1


# ---------------------------------------------------------------------------
# the run CHECK — added, not replacing anything (PRD §7.5 note 2)
# ---------------------------------------------------------------------------


async def test_a_creative_run_needs_a_source_run(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    db.add(_run(workspace_id, project_id, admin_user.id, RunStage.CREATIVE))
    with pytest.raises(IntegrityError, match="ck_run_creative_has_source"):
        await db.flush()


async def test_a_guideline_run_still_inserts_without_a_source(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = _run(workspace_id, project_id, admin_user.id, RunStage.GUIDELINE, bindings={})
    db.add(run)
    await db.flush()
    assert run.source_run_id is None


async def test_a_plan_run_without_a_source_is_still_rejected(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    db.add(_run(workspace_id, project_id, admin_user.id, RunStage.PLAN))
    with pytest.raises(IntegrityError, match="ck_run_plan_has_source"):
        await db.flush()


async def test_the_stage03_run_checks_are_untouched(db: AsyncSession) -> None:
    """ADD, never drop or replace: both earlier CHECKs keep their exact text."""
    rows = dict(
        (
            await db.execute(
                sa.text(
                    "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conrelid = 'run'::regclass AND contype = 'c'"
                )
            )
        ).all()
    )
    assert set(rows) == {
        "ck_run_plan_has_source",
        "ck_run_guideline_has_bindings",
        "ck_run_creative_has_source",
        # 0021 (S4-P4): additive, like the one before it.
        "ck_run_creative_input_only_creative",
    }
    assert "stage <> 'plan'::run_stage" in rows["ck_run_plan_has_source"]
    assert "stage <> 'research'::run_stage" in rows["ck_run_plan_has_source"]
    assert "jsonb_typeof(bindings)" in rows["ck_run_guideline_has_bindings"]
    assert "jsonb_typeof(creative_input)" in rows["ck_run_creative_input_only_creative"]


@pytest.mark.parametrize(
    ("stage", "value"),
    [(RunStage.RESEARCH, "{}"), (RunStage.CREATIVE, '"not an object"')],
)
async def test_only_a_creative_run_carries_an_input_and_it_is_an_object(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    stage: RunStage,
    value: str,
) -> None:
    """0021: `creative_input` is NULL, or a JSON object on a creative run."""
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    target = run.id if stage is RunStage.CREATIVE else run.source_run_id
    with pytest.raises(sa.exc.IntegrityError, match="ck_run_creative_input_only_creative"):
        await db.execute(
            sa.text("UPDATE run SET creative_input = CAST(:value AS jsonb) WHERE id = :id"),
            {"value": value, "id": target},
        )
    await db.rollback()


async def test_a_creative_run_with_a_source_inserts_and_carries_its_pins(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    assert run.pins is not None and run.pins[0]["reason"] == "start"


async def test_existing_claims_default_to_harvest(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = _run(workspace_id, project_id, admin_user.id, RunStage.GUIDELINE, bindings={})
    db.add(run)
    await db.flush()
    guideline = ContentGuideline(
        workspace_id=workspace_id,
        project_id=project_id,
        guideline_run_id=run.id,
        schema_version="1.0",
        version_major=1,
        version_minor=0,
        status=GuidelineStatus.DRAFT,
        mode=GuidelineMode.STANDALONE,
        bindings={},
        unbound_inputs=[],
    )
    db.add(guideline)
    await db.flush()
    claim = ClaimRecord(
        workspace_id=workspace_id,
        project_id=project_id,
        first_seen_guideline_id=guideline.id,
        claim_text="ISO 9001 certified",
        normalized_text="iso 9001 certified",
        claim_type="certification",
    )
    db.add(claim)
    await db.flush()
    await db.refresh(claim)
    assert claim.origin == "harvest"


# ---------------------------------------------------------------------------
# trigger a — an approved brief is frozen
# ---------------------------------------------------------------------------


async def _brief(db: AsyncSession, run: Run, *, approved: bool) -> CreativeBrief:
    brief = CreativeBrief(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        creative_run_id=run.id,
        schema_version="1.0",
        payload={"concepts": ["a"]},
        markdown="# brief",
        brief_hash="hash-1",
        approved_hash="hash-1" if approved else None,
    )
    db.add(brief)
    await db.flush()
    return brief


@pytest.mark.parametrize(
    ("column", "value"),
    [("payload", {"concepts": ["b"]}), ("markdown", "# changed"), ("brief_hash", "hash-2")],
)
async def test_an_approved_brief_rejects_changes_to_what_was_approved(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    column: str,
    value: Any,
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    brief = await _brief(db, run, approved=True)
    with pytest.raises(DBAPIError, match="was approved at G7"):
        await db.execute(
            sa.update(CreativeBrief).where(CreativeBrief.id == brief.id).values(**{column: value})
        )
    await db.rollback()


async def test_an_unapproved_brief_is_editable_and_can_be_approved(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    brief = await _brief(db, run, approved=False)
    await db.execute(
        sa.update(CreativeBrief)
        .where(CreativeBrief.id == brief.id)
        .values(payload={"concepts": ["b"]}, brief_hash="hash-2")
    )
    # The statement that approves is itself permitted — the guard reads OLD.
    await db.execute(
        sa.update(CreativeBrief).where(CreativeBrief.id == brief.id).values(approved_hash="hash-2")
    )
    # And afterwards the columns outside the frozen three still move.
    await db.execute(
        sa.update(CreativeBrief).where(CreativeBrief.id == brief.id).values(approved_hash="hash-2")
    )


# ---------------------------------------------------------------------------
# trigger b — a released asset never moves
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("column", "value"), [("text", "edited"), ("status", "dropped")])
async def test_a_frozen_asset_rejects_every_change(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    column: str,
    value: Any,
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    asset = await _asset(db, run, frozen_at=_now())
    with pytest.raises(DBAPIError, match="was frozen at release"):
        await db.execute(
            sa.update(CreativeAsset).where(CreativeAsset.id == asset.id).values(**{column: value})
        )
    await db.rollback()


async def test_an_unfrozen_asset_is_editable_and_can_be_frozen(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    asset = await _asset(db, run)
    await db.execute(
        sa.update(CreativeAsset).where(CreativeAsset.id == asset.id).values(text="edited")
    )
    await db.execute(
        sa.update(CreativeAsset)
        .where(CreativeAsset.id == asset.id)
        .values(frozen_at=_now(), status="released")
    )


# ---------------------------------------------------------------------------
# trigger c — a released package: only status and the two banners
# ---------------------------------------------------------------------------


async def _package(
    db: AsyncSession, run: Run, actor: uuid.UUID, *, status: CreativePackageStatus
) -> CreativePackage:
    """A package needs a real plan and a real guideline to point at."""
    research = (await db.execute(sa.select(Run).where(Run.id == run.source_run_id))).scalar_one()
    report = Report(run_id=research.id, schema_version="1.2", payload={}, markdown="")
    db.add(report)
    await db.flush()
    acceptance = ResearchAcceptance(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        run_id=research.id,
        report_id=report.id,
        accepted_by=actor,
        launch_readiness_at_acceptance="go",
    )
    db.add(acceptance)
    await db.flush()
    plan_run = _run(run.workspace_id, run.project_id, actor, RunStage.PLAN, research.id)
    db.add(plan_run)
    await db.flush()
    plan = CampaignPlan(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        plan_run_id=plan_run.id,
        acceptance_id=acceptance.id,
        schema_version="1.0",
        version=1,
        status=CampaignPlanStatus.FROZEN,
    )
    guideline_run = _run(run.workspace_id, run.project_id, actor, RunStage.GUIDELINE, bindings={})
    db.add_all([plan, guideline_run])
    await db.flush()
    guideline = ContentGuideline(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        guideline_run_id=guideline_run.id,
        schema_version="1.0",
        version_major=1,
        version_minor=0,
        status=GuidelineStatus.DRAFT,
        mode=GuidelineMode.STANDALONE,
        bindings={},
        unbound_inputs=[],
    )
    db.add(guideline)
    await db.flush()
    package = CreativePackage(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        creative_run_id=run.id,
        schema_version="1.0",
        version=1,
        status=status,
        plan_id=plan.id,
        plan_version=1,
        guideline_id=guideline.id,
        ruleset_version="v1.0",
        payload={"ads": []},
    )
    db.add(package)
    await db.flush()
    return package


@pytest.mark.parametrize(
    ("column", "value"),
    [("payload", {"ads": ["x"]}), ("version", 2), ("package_hash", "h"), ("cost_usd", 1)],
)
async def test_a_released_package_rejects_changes_outside_the_allow_list(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    column: str,
    value: Any,
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    package = await _package(db, run, admin_user.id, status=CreativePackageStatus.RELEASED)
    with pytest.raises(DBAPIError, match="is released"):
        await db.execute(
            sa.update(CreativePackage)
            .where(CreativePackage.id == package.id)
            .values(**{column: value})
        )
    await db.rollback()


async def test_a_released_package_may_be_superseded_and_flagged(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    package = await _package(db, run, admin_user.id, status=CreativePackageStatus.RELEASED)
    # Through the ORM, so `updated_at`'s onupdate stamp rides along — the
    # permitted change must be makeable by the application, not only by SQL.
    package.plan_superseded = True
    package.ruleset_superseded = True
    package.status = CreativePackageStatus.SUPERSEDED
    await db.flush()


async def test_a_draft_package_is_freely_editable(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    package = await _package(db, run, admin_user.id, status=CreativePackageStatus.DRAFT)
    await db.execute(
        sa.update(CreativePackage)
        .where(CreativePackage.id == package.id)
        .values(payload={"ads": ["x"]}, status="released", package_hash="h")
    )


# ---------------------------------------------------------------------------
# trigger d — a decision is append-only
# ---------------------------------------------------------------------------


async def _decision(db: AsyncSession, run: Run, actor: uuid.UUID) -> AssetDecision:
    asset = await _asset(db, run)
    approval = Approval(
        run_id=run.id,
        node_id="4.4.3",
        gate_key="G8",
        required_role=ApprovalRequiredRole.APPROVER,
    )
    db.add(approval)
    await db.flush()
    decision = AssetDecision(
        approval_id=approval.id,
        asset_id=asset.id,
        round=1,
        decision=AssetDecisionChoice.APPROVE,
        decided_by=actor,
    )
    db.add(decision)
    await db.flush()
    return decision


async def test_an_asset_decision_rejects_every_update(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    decision = await _decision(db, run, admin_user.id)
    with pytest.raises(DBAPIError, match="append-only"):
        await db.execute(
            sa.update(AssetDecision)
            .where(AssetDecision.id == decision.id)
            .values(decision="reject")
        )
    await db.rollback()


async def test_an_asset_decision_may_be_inserted(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    """The permitted operation on an append-only table is the append."""
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    decision = await _decision(db, run, admin_user.id)
    assert decision.id is not None


async def test_one_decision_per_asset_per_approval(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    decision = await _decision(db, run, admin_user.id)
    db.add(
        AssetDecision(
            approval_id=decision.approval_id,
            asset_id=decision.asset_id,
            round=2,
            decision=AssetDecisionChoice.REJECT,
            decided_by=admin_user.id,
        )
    )
    with pytest.raises(IntegrityError, match="uq_asset_decision_approval_asset"):
        await db.flush()


# ---------------------------------------------------------------------------
# Law 39 and Law 37 in DDL
# ---------------------------------------------------------------------------


def _artifact(asset: CreativeAsset, transform: dict[str, Any] | None) -> MediaArtifact:
    return MediaArtifact(
        workspace_id=asset.workspace_id,
        asset_id=asset.id,
        role=MediaArtifactRole.RENDITION,
        storage_path="creative/x.png",
        media_type="image/png",
        width=1200,
        height=628,
        bytes=1,
        sha256="s",
        aspect_ratio="1.91:1",
        derivation=MediaArtifactDerivation.CROP,
        transform=transform,
    )


async def test_a_non_uniform_scale_is_rejected(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    asset = await _asset(db, run, kind=CreativeAssetKind.IMAGE)
    db.add(_artifact(asset, {"sx": 0.5, "sy": 0.6}))
    with pytest.raises(IntegrityError, match="ck_media_artifact_uniform_scale"):
        await db.flush()


@pytest.mark.parametrize("transform", [None, {"sx": 0.5, "sy": 0.5}])
async def test_a_uniform_scale_or_no_transform_is_accepted(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    admin_user: Any,
    transform: dict[str, Any] | None,
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    asset = await _asset(db, run, kind=CreativeAssetKind.IMAGE)
    db.add(_artifact(asset, transform))
    await db.flush()


def _job(run: Run, key: str, openrouter_job_id: str | None = None) -> GenerationJob:
    return GenerationJob(
        workspace_id=run.workspace_id,
        project_id=run.project_id,
        creative_run_id=run.id,
        node_id="4.4.1",
        modality=GenerationModality.VIDEO,
        model_id="some/model",
        capability_hash="cap",
        request={"prompt_sha256": "p"},
        idempotency_key=key,
        openrouter_job_id=openrouter_job_id,
        estimate_usd=Decimal("1.50"),
    )


async def test_a_duplicate_idempotency_key_is_rejected(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    db.add(_job(run, "run:asset:1"))
    await db.flush()
    db.add(_job(run, "run:asset:1"))
    with pytest.raises(IntegrityError, match="idempotency_key"):
        await db.flush()


async def test_a_duplicate_openrouter_job_id_is_rejected(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, admin_user: Any
) -> None:
    run = await _creative_run(db, workspace_id, project_id, admin_user.id)
    db.add_all([_job(run, "k1", "or-1"), _job(run, "k2", None), _job(run, "k3", None)])
    await db.flush()
    db.add(_job(run, "k4", "or-1"))
    with pytest.raises(IntegrityError, match="openrouter_job_id"):
        await db.flush()
