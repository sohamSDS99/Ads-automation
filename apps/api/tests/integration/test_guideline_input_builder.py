"""Assembling `GuidelineInput` (PRD §4.5, §23 deliverable 5).

The theme of every test here: a binding that cannot be resolved is **dropped**,
never fatal. Stage 02's builder raises `PlanInputError` and the route turns it
into a 422, because a plan without its research is not a plan. Stage 03 has no
such dependency, so the same situation is a narrower scope and a line in
`unbound_inputs`. An implementation that raises has rebuilt the handshake.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import (
    CampaignPlan,
    CampaignPlanStatus,
    GuidelineMode,
    ResearchAcceptance,
    Run,
    RunStage,
    RunStatus,
    RunTrigger,
)
from agent.orchestrator.guideline_input import build_guideline_input
from agent.schemas.guideline_input import GuidelineBindings

pytestmark = pytest.mark.asyncio


async def _acceptance(db: AsyncSession, project_id: uuid.UUID) -> ResearchAcceptance:
    import sqlalchemy as sa

    return (
        await db.execute(
            sa.select(ResearchAcceptance).where(
                ResearchAcceptance.project_id == project_id,
                ResearchAcceptance.superseded_by.is_(None),
            )
        )
    ).scalar_one()


# ---------------------------------------------------------------------------
# the cold path
# ---------------------------------------------------------------------------


async def test_a_bare_project_builds_a_standalone_input(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """No research, no plan, no acceptance. This must produce an object."""
    built, digest = await build_guideline_input(
        db, project_id, GuidelineBindings(), workspace_id=workspace_id
    )
    assert built.mode is GuidelineMode.STANDALONE
    assert built.compliance_guardrails is None
    assert built.channel_slate is None
    assert len(digest) == 64
    assert digest == built.content_hash()


async def test_an_unbound_run_says_what_it_did_not_get(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """§4.3 rule 2: the rulebook must be able to show what it was built without."""
    built, _ = await build_guideline_input(
        db, project_id, GuidelineBindings(), workspace_id=workspace_id
    )
    assert "research" in built.unbound_inputs
    assert "plan" in built.unbound_inputs


# ---------------------------------------------------------------------------
# bindings that resolve
# ---------------------------------------------------------------------------


async def test_a_research_binding_brings_the_guardrails(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, accepted: dict[str, Any]
) -> None:
    acceptance = await _acceptance(db, project_id)
    built, _ = await build_guideline_input(
        db,
        project_id,
        GuidelineBindings(research_run_id=acceptance.run_id),
        workspace_id=workspace_id,
    )
    assert built.mode is GuidelineMode.RESEARCH_LINKED
    assert built.compliance_guardrails is not None
    assert "research" not in built.unbound_inputs


# ---------------------------------------------------------------------------
# bindings that do not — every one of these must NOT raise
# ---------------------------------------------------------------------------


async def test_an_unknown_research_run_is_dropped_not_fatal(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    built, _ = await build_guideline_input(
        db,
        project_id,
        GuidelineBindings(research_run_id=uuid.uuid4()),
        workspace_id=workspace_id,
    )
    assert built.mode is GuidelineMode.STANDALONE
    assert built.bindings.research_run_id is None
    assert any("research" in item for item in built.unbound_inputs)


async def test_a_skewed_research_schema_drops_the_binding(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    accepted: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The deliberate deviation from Stage 02 §4.3 rule 5, which is a 422.

    Stage 03 does not need the research, so an unreadable version costs it
    scope and nothing else.
    """
    from agent.config import get_settings

    monkeypatch.setenv("GUIDELINE_SUPPORTED_RESEARCH_SCHEMAS", '["9.9"]')
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings, "cache_clear", get_settings.cache_clear)
    acceptance = await _acceptance(db, project_id)
    built, _ = await build_guideline_input(
        db,
        project_id,
        GuidelineBindings(research_run_id=acceptance.run_id),
        workspace_id=workspace_id,
    )
    get_settings.cache_clear()
    assert built.mode is GuidelineMode.STANDALONE
    assert any("schema" in item for item in built.unbound_inputs)


async def test_a_research_run_from_another_project_is_dropped(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    second_project_id: uuid.UUID,
    admin_user: Any,
) -> None:
    """A binding is not an authorization bypass.

    Resolution is scoped to the project *and* the workspace, so a caller who
    guesses another project's run id gets a standalone run rather than that
    project's research folded into their rulebook.
    """
    foreign = Run(
        workspace_id=workspace_id,
        project_id=second_project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.RESEARCH,
    )
    db.add(foreign)
    await db.flush()

    built, _ = await build_guideline_input(
        db,
        project_id,
        GuidelineBindings(research_run_id=foreign.id),
        workspace_id=workspace_id,
    )
    assert built.mode is GuidelineMode.STANDALONE
    assert built.bindings.research_run_id is None


async def test_an_unfrozen_plan_is_not_a_binding(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    accepted: dict[str, Any],
    admin_user: Any,
) -> None:
    """§4.2 names a *frozen* plan. A draft is still moving."""
    acceptance = await _acceptance(db, project_id)
    plan_run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.PLAN,
        source_run_id=acceptance.run_id,
    )
    db.add(plan_run)
    await db.flush()
    draft = CampaignPlan(
        workspace_id=workspace_id,
        project_id=project_id,
        plan_run_id=plan_run.id,
        acceptance_id=acceptance.id,
        schema_version="1.0",
        version=0,
        status=CampaignPlanStatus.DRAFT,
        payload={},
        markdown="",
    )
    db.add(draft)
    await db.flush()

    built, _ = await build_guideline_input(
        db, project_id, GuidelineBindings(plan_id=draft.id), workspace_id=workspace_id
    )
    assert built.mode is GuidelineMode.STANDALONE
    assert any("plan" in item for item in built.unbound_inputs)


async def test_a_frozen_plan_binds_and_brings_the_slate(
    db: AsyncSession,
    workspace_id: uuid.UUID,
    project_id: uuid.UUID,
    accepted: dict[str, Any],
    admin_user: Any,
) -> None:
    acceptance = await _acceptance(db, project_id)
    plan_run = Run(
        workspace_id=workspace_id,
        project_id=project_id,
        triggered_by=admin_user.id,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.SUCCEEDED,
        stage=RunStage.PLAN,
        source_run_id=acceptance.run_id,
    )
    db.add(plan_run)
    await db.flush()
    frozen = CampaignPlan(
        workspace_id=workspace_id,
        project_id=project_id,
        plan_run_id=plan_run.id,
        acceptance_id=acceptance.id,
        schema_version="1.0",
        version=1,
        status=CampaignPlanStatus.FROZEN,
        payload={"channel_slate": {"slate": [], "rejected": []}},
        markdown="# plan",
        frozen_at=datetime.now(UTC),
        frozen_by=admin_user.id,
    )
    db.add(frozen)
    await db.flush()

    built, _ = await build_guideline_input(
        db, project_id, GuidelineBindings(plan_id=frozen.id), workspace_id=workspace_id
    )
    assert built.mode is GuidelineMode.PLAN_LINKED
    assert built.channel_slate is not None
    assert built.bindings.plan_version == 1


async def test_the_builder_never_raises_on_a_bad_binding(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID
) -> None:
    """The invariant, stated once on its own.

    Every unresolvable combination at once. If this raises, some later phase
    will discover that the cold path is not actually cold.
    """
    built, _ = await build_guideline_input(
        db,
        project_id,
        GuidelineBindings(
            research_run_id=uuid.uuid4(),
            acceptance_id=uuid.uuid4(),
            plan_id=uuid.uuid4(),
            plan_version=17,
        ),
        workspace_id=workspace_id,
    )
    assert built.mode is GuidelineMode.STANDALONE


async def test_a_binding_is_dropped_when_the_workspace_does_not_match(
    db: AsyncSession, workspace_id: uuid.UUID, project_id: uuid.UUID, accepted: dict[str, Any]
) -> None:
    """The tenancy boundary, asserted on the binding path specifically.

    `bindings` is the only place in Stage 03 where a caller hands the server a
    primary key and the server goes and reads the row behind it. Every other
    input is a path parameter the route has already scoped, so this is the one
    call worth proving cannot be walked across a tenant boundary.

    Driven by calling the builder with a workspace that is not the one holding
    the acceptance, rather than by standing up a second tenant: the assertion
    is about the `workspace_id` predicate in `_research`, and a second
    `workspace` row only adds TRUNCATE contention to the same claim. A real
    acceptance exists here — so if the predicate were dropped, this resolves
    and the test fails.
    """
    acceptance = await _acceptance(db, project_id)
    assert acceptance.workspace_id == workspace_id, "the fixture must really be bound"

    built, _ = await build_guideline_input(
        db,
        project_id,
        GuidelineBindings(research_run_id=acceptance.run_id),
        workspace_id=uuid.uuid4(),
    )
    assert built.mode is GuidelineMode.STANDALONE
    assert built.bindings.research_run_id is None
    # And nothing of theirs leaked in under another name.
    assert built.compliance_guardrails is None
    assert built.competitor_creative is None
    assert built.differentiation_claim is None
