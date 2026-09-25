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
    campaign_type: str | None = None,
    keywords: list[dict[str, Any]] | None = None,
    extra_campaigns: list[dict[str, Any]] | None = None,
    channel_slate: dict[str, Any] | None = None,
) -> CampaignPlan:
    """Accepted research → a plan run → a plan in `status`, with a real payload.

    `extra_campaigns` are appended to the two Search ones; `channel_slate` is
    2.3.1's section as the frozen plan carries it (absent means no slate).
    """
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
            "objectives": {
                "north_star_metric": "qualified leads",
                "campaign_objectives": [
                    {
                        "campaign_ref": "c-sds-us",
                        "objective": "lead_gen",
                        "primary_kpi": "cost per qualified lead",
                    }
                ],
                "qualified_lead": {"required_signals": ["company email"]},
            },
            "account_structure": {
                "campaigns": [
                    {
                        "name": "Search - SDS - US",
                        "campaign_ref": "c-sds-us",
                        **({"type": campaign_type} if campaign_type else {}),
                        "ad_groups": [
                            {
                                "name": "sds software",
                                "theme": "SDS management",
                                "landing_url": "https://example.com/sds",
                                "primary_message": "Keep every SDS current",
                                **({"keywords": keywords} if keywords else {}),
                            }
                        ],
                    },
                    {
                        "name": "Search - Brand",
                        "campaign_ref": "c-brand",
                        **({"type": campaign_type} if campaign_type else {}),
                    },
                    *(extra_campaigns or []),
                ]
            },
            **({"channel_slate": channel_slate} if channel_slate is not None else {}),
            "open_dependencies": [{"task": "Install the conversion tag", "blocking": True}],
        },
    )
    db.add(plan)
    await db.flush()
    return plan


def guideline_payload(
    guideline: ContentGuideline, *, logo_rules: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A published rulebook with the sections the projection reads filled in.

    `logo_rules` is 3.1.3's `logo` block (clear space, minimum width) — what
    4.4.3 composites a registered logo by."""
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
            "visual_identity": {
                "colour": {"primary": "brand-orange"},
                "imagery": {"stock": "no"},
                **({"logo": logo_rules} if logo_rules is not None else {}),
            },
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
    asset_specs: dict[str, Any] | None = None,
    extra_rules: tuple[Any, ...] = (),
    logo_templates: tuple[dict[str, Any], ...] = (),
    logo_rules: dict[str, Any] | None = None,
    detectors: tuple[Any, ...] = (),
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
    guideline.payload = guideline_payload(guideline, logo_rules=logo_rules)
    await db.flush()
    digest = hashlib.sha256(f"{guideline.id}".encode()).hexdigest()
    ruleset = RuleSet(
        workspace_id=ws,
        project_id=project_id,
        guideline_id=guideline.id,
        ruleset_version=f"{major}.0+{digest[:8]}",
        compiled=compiled_ruleset(
            ruleset_version=f"{major}.0+{digest[:8]}",
            project_id=project_id,
            guideline_id=guideline.id,
            digest=digest,
            categories=categories,
            asset_specs=asset_specs,
            schema_version=ruleset_schema,
            extra_rules=extra_rules,
            logo_templates=logo_templates,
            detectors=detectors,
        ),
        compiler_version="test",
        constants_version="test",
        rule_count=len(categories) + len(extra_rules),
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


#: The licensed claim `compiled_ruleset` carries, and one it does not license.
LICENSED_CLAIM_ID = uuid.UUID("5f2504e0-4f89-11d3-9a0c-0305e82c3401")
DRAFT_CLAIM_ID = uuid.UUID("5f2504e0-4f89-11d3-9a0c-0305e82c3402")


def compiled_ruleset(
    *,
    ruleset_version: str,
    project_id: uuid.UUID,
    guideline_id: uuid.UUID,
    digest: str,
    categories: tuple[str, ...] = ALL_CATEGORIES,
    asset_specs: dict[str, Any] | None = None,
    schema_version: str = "1.0",
    extra_rules: tuple[Any, ...] = (),
    logo_templates: tuple[dict[str, Any], ...] = (),
    detectors: tuple[Any, ...] = (),
) -> dict[str, Any]:
    """A `RuleSet` the pinned linter can load (Stage 04's `lint_adapter`).

    One real rule per category — a banned term, so it compiles and can fire —
    with the `{category}.one.v1` ids eligibility reads, plus one licensed and
    one draft claim for the brief's proof points. A schema version other than
    1.0 is written raw: the skew test needs a ruleset no code can parse.
    """
    from agent.guardrails.matchers.lexicon import banned_term
    from agent.schemas.guardrails import Authority, ClaimRef, Rule, RuleSet

    authority = Authority(source="brand", reference="rulebook", reviewed_at=_now().date())
    rules = [
        Rule(
            rule_id=f"{category}.one.v1",
            category=category,  # type: ignore[arg-type]
            **banned_term(
                ("guaranteed compliance",), authority=authority, message="Never promise it."
            ).model_dump(exclude={"rule_id", "category"}),
        )
        for category in categories
    ]
    claims = [
        ClaimRef(
            claim_id=LICENSED_CLAIM_ID,
            normalized_text="sds updates within 24 hours",
            surface_forms=("SDS updates within 24 hours",),
            status="approved",
            signature_id=uuid.UUID("5f2504e0-4f89-11d3-9a0c-0305e82c3403"),
        ),
        ClaimRef(claim_id=DRAFT_CLAIM_ID, normalized_text="the fastest sds tool", status="draft"),
    ]
    compiled = RuleSet(
        ruleset_version=ruleset_version,
        project_id=project_id,
        guideline_id=guideline_id,
        compiler_version="test",
        constants_version="test",
        compiled_at=_now(),
        rules=(*rules, *extra_rules),
        claims_index=tuple(claims),
        detectors=detectors,
        **({"asset_specs": {"specs": asset_specs}} if asset_specs else {}),  # type: ignore[arg-type]
        logo_templates=logo_templates,  # type: ignore[arg-type]
        hash=digest,
    ).model_dump(mode="json")
    compiled["schema_version"] = schema_version
    return compiled


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
