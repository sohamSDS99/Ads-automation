"""Seeding the two upstream artifacts Stage 04 is gated on, directly.

Stage 04's entry reads a frozen `CampaignPlan` and a published `ContentGuideline`
with its compiled `RuleSet`. Driving Stages 01–03 end to end to get them would
test those stages again; these helpers write the rows those stages would have
written, in the states the gate cares about, with payloads that validate
against the real contracts — so the builder parses exactly what it would parse
in production.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CampaignPlan,
    CampaignPlanStatus,
    ContentGuideline,
    GuidelineMode,
    GuidelineStatus,
    Report,
    ResearchAcceptance,
    RuleSet,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
    SignOffMatrix,
)

#: A ruleset that satisfies CR-E4 in full.
ALL_CATEGORIES: tuple[str, ...] = (
    "asset_spec",
    "claim",
    "lexicon",
    "policy",
    "image",
    "disclosure",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _run(
    ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID, stage: RunStage, **extra: Any
) -> Run:
    return Run(
        workspace_id=ws,
        project_id=project_id,
        triggered_by=actor,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=stage,
        **extra,
    )


async def seed_plan(
    db: AsyncSession,
    ws: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    status: CampaignPlanStatus = CampaignPlanStatus.FROZEN,
    version: int = 1,
    schema_version: str = "1.0",
    source_superseded: bool = False,
) -> CampaignPlan:
    """Accepted research → a plan run → a plan in `status`, with a real payload."""
    research = _run(ws, project_id, actor, RunStage.RESEARCH)
    db.add(research)
    await db.flush()
    report = Report(
        run_id=research.id,
        schema_version="1.0",
        markdown="# report",
        payload={
            "schema_version": "1.0",
            "project_id": str(project_id),
            "run_id": str(research.id),
            "generated_at": _now().isoformat(),
            "executive_summary": "Chemical distributors search for SDS compliance software.",
            "launch_readiness": "go",
            "competitive_landscape": {
                "recommended_claim": "SDS updates in 24 hours",
                "message_clusters": [{"theme": "compliance", "frequency": 3}],
            },
        },
    )
    db.add(report)
    await db.flush()
    acceptance = ResearchAcceptance(
        workspace_id=ws,
        project_id=project_id,
        run_id=research.id,
        report_id=report.id,
        accepted_by=actor,
        launch_readiness_at_acceptance="go",
    )
    db.add(acceptance)
    await db.flush()
    plan_run = _run(ws, project_id, actor, RunStage.PLAN, source_run_id=research.id)
    db.add(plan_run)
    await db.flush()
    plan = CampaignPlan(
        workspace_id=ws,
        project_id=project_id,
        plan_run_id=plan_run.id,
        acceptance_id=acceptance.id,
        schema_version=schema_version,
        version=version
        if status in {CampaignPlanStatus.FROZEN, CampaignPlanStatus.SUPERSEDED}
        else 0,
        status=status,
        source_superseded=source_superseded,
        frozen_at=_now() if status is CampaignPlanStatus.FROZEN else None,
        payload={
            "schema_version": "1.0",
            "project_id": str(project_id),
            "plan_run_id": str(plan_run.id),
            "version": version,
            "generated_at": _now().isoformat(),
            "source": {
                "research_run_id": str(research.id),
                "report_id": str(report.id),
                "acceptance_id": str(acceptance.id),
                "accepted_by": str(actor),
                "accepted_at": _now().isoformat(),
            },
            "objectives": {"qualified_lead": {"required_signals": ["company email"]}},
            "account_structure": {
                "campaigns": [
                    {
                        "name": "Search - SDS - US",
                        "campaign_ref": "c-sds-us",
                        "ad_groups": [
                            {
                                "name": "sds software",
                                "theme": "SDS management",
                                "landing_url": "https://example.com/sds",
                                "primary_message": "Keep every SDS current",
                            }
                        ],
                    },
                    {"name": "Search - Brand", "campaign_ref": "c-brand"},
                ]
            },
            "open_dependencies": [{"task": "Install the conversion tag", "blocking": True}],
        },
    )
    db.add(plan)
    await db.flush()
    return plan


def guideline_payload(guideline: ContentGuideline) -> dict[str, Any]:
    """A published rulebook with the sections the projection reads filled in."""
    return {
        "schema_version": "1.0",
        "project_id": str(guideline.project_id),
        "guideline_run_id": str(guideline.guideline_run_id),
        "guideline_id": str(guideline.id),
        "version_major": guideline.version_major,
        "version_minor": 0,
        "generated_at": _now().isoformat(),
        "status": "published",
        "executive_summary": f"Rulebook v{guideline.version_major}.",
        "brand_rules": {
            "voice": {
                "voice_words": ["plain", f"v{guideline.version_major}"],
                "definition_per_word": {"plain": "no jargon"},
                "register": {"formality": "neutral"},
            },
            "lexicon": {
                "always": [{"term": "safety data sheet"}],
                "never": [{"term": "guaranteed compliance", "reason": "unprovable"}],
            },
            "visual_identity": {"colour": {"primary": "brand-orange"}, "imagery": {"stock": "no"}},
        },
        "policy_profile": {
            "competitor_mentions": {"allowed": False, "zeta": 1, "alpha": 2},
            "personalization": {"forbidden_implications": ["health status"]},
            "disclosure_rules": [
                {"disclosure_id": "ai", "required_text": "Made with AI", "placement": "suffix"}
            ],
        },
    }


async def seed_published(
    db: AsyncSession,
    ws: uuid.UUID,
    project_id: uuid.UUID,
    actor: uuid.UUID,
    *,
    major: int = 1,
    categories: tuple[str, ...] = ALL_CATEGORIES,
    ruleset_schema: str = "1.0",
    signature_stale: bool = False,
) -> tuple[ContentGuideline, RuleSet]:
    """A published guideline and the ruleset publish would have minted with it.

    Inserted `ready_to_publish`, then flipped to `published` together with
    `ruleset_id` in one UPDATE — the same single statement `publish_guideline`
    uses, and the only order trigger `content_guideline_published` allows.
    """
    run = _run(ws, project_id, actor, RunStage.GUIDELINE, bindings={})
    db.add(run)
    await db.flush()
    guideline = ContentGuideline(
        workspace_id=ws,
        project_id=project_id,
        guideline_run_id=run.id,
        schema_version="1.0",
        version_major=major,
        version_minor=0,
        status=GuidelineStatus.READY_TO_PUBLISH,
        mode=GuidelineMode.STANDALONE,
        bindings={},
        unbound_inputs=[],
        markdown="# rulebook",
        signature_stale=signature_stale,
    )
    db.add(guideline)
    await db.flush()
    guideline.payload = guideline_payload(guideline)
    await db.flush()
    digest = hashlib.sha256(f"{guideline.id}".encode()).hexdigest()
    ruleset = RuleSet(
        workspace_id=ws,
        project_id=project_id,
        guideline_id=guideline.id,
        ruleset_version=f"{major}.0+{digest[:8]}",
        compiled={
            "schema_version": ruleset_schema,
            "rules": [
                {"rule_id": f"{category}.one.v1", "category": category} for category in categories
            ],
        },
        compiler_version="test",
        constants_version="test",
        rule_count=len(categories),
        hash=digest,
    )
    db.add(ruleset)
    await db.flush()
    guideline.status = GuidelineStatus.PUBLISHED
    guideline.ruleset_id = ruleset.id
    guideline.published_at = _now()
    guideline.published_by = actor
    await db.flush()
    return guideline, ruleset


async def seed_signoff(
    db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, owner: uuid.UUID
) -> SignOffMatrix:
    matrix = SignOffMatrix(
        workspace_id=ws,
        project_id=project_id,
        brand_owner_id=owner,
        legal_owner_id=owner,
        performance_owner_id=owner,
        set_by=owner,
    )
    db.add(matrix)
    await db.flush()
    return matrix


async def seed_both(
    db: AsyncSession, ws: uuid.UUID, project_id: uuid.UUID, actor: uuid.UUID
) -> tuple[CampaignPlan, ContentGuideline, RuleSet]:
    """The eligible fixture: a frozen plan, a published ruleset, a sign-off."""
    plan = await seed_plan(db, ws, project_id, actor)
    guideline, ruleset = await seed_published(db, ws, project_id, actor)
    await seed_signoff(db, ws, project_id, actor)
    await db.commit()
    return plan, guideline, ruleset


TEXT_ONLY: dict[str, Any] = {"images": False, "video": False, "concepts_per_campaign": 2}
