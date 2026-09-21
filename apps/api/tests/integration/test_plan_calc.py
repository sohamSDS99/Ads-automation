"""S2-P1 exit criterion: `PlanCalc` dedupes on (plan_run_id, formula_id, inputs_hash).

Against real Postgres, because the dedupe *is* a unique constraint. A test that
only exercised `DerivedWriter`'s Python would pass just as happily if migration
0013 had shipped without the constraint, which is the one failure mode that
matters: two `derived` Evidence rows saying the same thing, and a plan whose
numbers resolve to whichever of them was read first.
"""

from __future__ import annotations

import uuid
from typing import Any

import pandas as pd
import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from agent.calc import economics
from agent.calc.derived import DerivedWriter
from agent.calc.registry import CalcResult, inputs_hash
from agent.db.models import Evidence, EvidenceSource, PlanCalc, Run, RunStatus, RunTrigger
from agent.evidence.store import EvidenceStore
from agent.planning.constants import load_planning_constants

CONSTANTS = load_planning_constants()

SEGMENTS = pd.DataFrame(
    [
        {
            "segment": "enterprise",
            "acv_usd": 60_000,
            "gross_margin_pct": 80,
            "lead_to_won_pct": 12,
            "deals": 10,
        }
    ]
)


@pytest.fixture
async def plan_run(db: AsyncSession, project: Any) -> Run:
    run = Run(
        workspace_id=project.workspace_id,
        project_id=project.id,
        triggered_by=None,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.RUNNING,
    )
    db.add(run)
    await db.commit()
    await db.refresh(run)
    return run


def writer(db: AsyncSession, project: Any, plan_run: Run) -> DerivedWriter:
    return DerivedWriter(
        db,
        store=EvidenceStore(db, project.workspace_id),
        project_id=project.id,
        plan_run_id=plan_run.id,
    )


async def count(db: AsyncSession, table: Any) -> int:
    return int((await db.execute(sa.select(sa.func.count()).select_from(table))).scalar_one())


async def test_a_calculation_writes_one_plan_calc_row_and_one_derived_evidence_row(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    result = economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS)
    evidence_id = await writer(db, project, plan_run).record(result, node_id="2.1.2")
    await db.commit()

    assert await count(db, PlanCalc) == 1
    row = (await db.execute(sa.select(PlanCalc))).scalar_one()
    assert row.plan_run_id == plan_run.id
    assert row.node_id == "2.1.2"
    assert row.formula_id == "economics.max_cpa_v1"
    assert row.calc_version == f"calc/1.0+constants/{CONSTANTS.version}"
    assert row.inputs_hash == result.inputs_hash
    assert row.evidence_id == evidence_id
    assert row.result["blended"]["max_cpl_usd"] == 1_920

    evidence = (await db.execute(sa.select(Evidence))).scalar_one()
    assert evidence.id == evidence_id
    assert evidence.source == EvidenceSource.DERIVED
    assert evidence.kind == "calc_economics"
    assert evidence.run_id == plan_run.id
    assert evidence.payload["formula_id"] == "economics.max_cpa_v1"
    # PRD §7.3: searchable in the Evidence Explorer alongside everything else.
    assert "Max CPL $1,920.00" in (evidence.content_text or "")
    assert evidence.embedding is not None


async def test_the_same_calculation_twice_is_one_row_and_one_evidence_id(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    subject = writer(db, project, plan_run)
    result = economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS)

    first = await subject.record(result, node_id="2.1.2")
    second = await subject.record(result, node_id="2.1.2")
    await db.commit()

    assert first == second
    assert await count(db, PlanCalc) == 1
    assert await count(db, Evidence) == 1


async def test_two_nodes_asking_for_one_calculation_cite_the_same_evidence(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    """The lookup is not keyed on node_id, so the second node reuses the first's row."""
    subject = writer(db, project, plan_run)
    result = economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS)

    first = await subject.record(result, node_id="2.1.2")
    second = await subject.record(result, node_id="2.2.3")
    await db.commit()

    assert first == second
    assert await count(db, PlanCalc) == 1
    assert (await db.execute(sa.select(PlanCalc.node_id))).scalar_one() == "2.1.2"


async def test_different_inputs_are_two_rows(db: AsyncSession, project: Any, plan_run: Run) -> None:
    subject = writer(db, project, plan_run)
    other = SEGMENTS.assign(acv_usd=90_000)

    first = await subject.record(
        economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS), node_id="2.1.2"
    )
    second = await subject.record(economics.max_cpa_v1(other, constants=CONSTANTS), node_id="2.1.2")
    await db.commit()

    assert first != second
    assert await count(db, PlanCalc) == 2
    assert await count(db, Evidence) == 2


async def test_a_different_formula_on_the_same_inputs_is_its_own_row(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    subject = writer(db, project, plan_run)
    ceiling = economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS)
    payback_input = SEGMENTS.assign(
        contract_term_months=12,
        max_cpa_won_usd=ceiling.result["by_segment"][0]["max_cpa_won_usd"],
    )

    await subject.record(ceiling, node_id="2.1.2")
    await subject.record(economics.payback_v1(payback_input, constants=CONSTANTS), node_id="2.1.2")
    await db.commit()

    assert await count(db, PlanCalc) == 2
    formulas = (
        (await db.execute(sa.select(PlanCalc.formula_id).order_by(PlanCalc.formula_id)))
        .scalars()
        .all()
    )
    assert list(formulas) == ["economics.max_cpa_v1", "economics.payback_v1"]


async def test_a_project_override_of_a_constant_is_a_different_calculation(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    """Otherwise two plans would claim identical provenance for different numbers."""
    subject = writer(db, project, plan_run)
    overridden = CONSTANTS.merged({"economics.target_cac_ratio": 4.0})

    await subject.record(economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS), node_id="2.1.2")
    await subject.record(economics.max_cpa_v1(SEGMENTS, constants=overridden), node_id="2.1.2")
    await db.commit()

    assert await count(db, PlanCalc) == 2
    versions = set((await db.execute(sa.select(PlanCalc.calc_version))).scalars().all())
    assert len(versions) == 2
    assert any("+ovr." in version for version in versions)


async def test_the_same_calculation_in_a_second_plan_run_gets_its_own_row(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    """The dedupe is per run, so a re-plan records its own figure.

    The `derived` Evidence row is reused, because evidence is deduped per
    project by content hash and the calculation genuinely is the same fact. What
    must not be shared is the `PlanCalc` row: it is how plan version 2 shows its
    own arithmetic even when nothing changed.
    """
    second = Run(
        workspace_id=project.workspace_id,
        project_id=project.id,
        triggered_by=None,
        trigger=RunTrigger.MANUAL,
        status=RunStatus.RUNNING,
    )
    db.add(second)
    await db.commit()
    await db.refresh(second)

    result = economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS)
    first_id = await writer(db, project, plan_run).record(result, node_id="2.1.2")
    second_id = await writer(db, project, second).record(result, node_id="2.1.2")
    await db.commit()

    assert first_id == second_id
    assert await count(db, Evidence) == 1
    assert await count(db, PlanCalc) == 2


async def test_the_unique_constraint_rejects_a_hand_crafted_duplicate(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    """The constraint itself, not the writer's politeness about it."""
    result = economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS)
    evidence_id = await writer(db, project, plan_run).record(result, node_id="2.1.2")
    await db.commit()

    db.add(
        PlanCalc(
            plan_run_id=plan_run.id,
            node_id="2.9.9",
            formula_id=result.formula_id,
            calc_version=result.calc_version,
            inputs=result.inputs,
            inputs_hash=result.inputs_hash,
            result=result.result,
            evidence_id=evidence_id,
        )
    )
    with pytest.raises(IntegrityError, match="uq_plan_calc_run_formula_inputs"):
        await db.commit()
    await db.rollback()


async def test_pruning_the_evidence_keeps_the_calculation_and_re_mints_the_citation(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    """`evidence_id` is `ON DELETE SET NULL`, so the arithmetic outlives its citation."""
    result = economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS)
    subject = writer(db, project, plan_run)
    first = await subject.record(result, node_id="2.1.2")
    await db.commit()

    await db.execute(sa.delete(Evidence).where(Evidence.id == first))
    await db.commit()
    row = (await db.execute(sa.select(PlanCalc))).scalar_one()
    assert row.evidence_id is None
    assert row.result["blended"]["max_cpl_usd"] == 1_920  # the number survived

    second = await subject.record(result, node_id="2.1.2")
    await db.commit()

    assert second != first
    assert await count(db, PlanCalc) == 1  # recited, not rewritten
    refreshed = (await db.execute(sa.select(PlanCalc))).scalar_one()
    assert refreshed.id == row.id
    assert refreshed.evidence_id == second


async def test_a_plan_calc_row_cannot_point_at_a_run_that_does_not_exist(
    db: AsyncSession, project: Any
) -> None:
    db.add(
        PlanCalc(
            plan_run_id=uuid.uuid4(),
            node_id="2.1.2",
            formula_id="economics.max_cpa_v1",
            calc_version="calc/1.0+constants/test",
            inputs={},
            inputs_hash=inputs_hash({}),
            result={},
            evidence_id=uuid.uuid4(),
        )
    )
    with pytest.raises(IntegrityError):
        await db.commit()
    await db.rollback()


async def test_a_calculation_from_another_workspace_is_refused(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    """`EvidenceStore` resolves the project through the workspace on every call."""
    from agent.evidence.store import EvidenceScopeError

    stranger = DerivedWriter(
        db,
        store=EvidenceStore(db, uuid.uuid4()),
        project_id=project.id,
        plan_run_id=plan_run.id,
    )
    with pytest.raises(EvidenceScopeError):
        await stranger.record(economics.max_cpa_v1(SEGMENTS, constants=CONSTANTS), node_id="2.1.2")
    await db.rollback()


async def test_every_registered_formula_can_be_persisted_and_read_back(
    db: AsyncSession, project: Any, plan_run: Run
) -> None:
    """A round trip through JSONB for each of the nine, so no result shape is unstorable."""
    from tests.calc_support import every_formula_result

    subject = writer(db, project, plan_run)
    results: list[CalcResult] = every_formula_result()
    assert len(results) == 9

    for result in results:
        await subject.record(result, node_id="2.6.1")
    await db.commit()

    assert await count(db, PlanCalc) == 9
    rows = (await db.execute(sa.select(PlanCalc))).scalars().all()
    by_formula = {row.formula_id: row for row in rows}
    for result in results:
        stored = by_formula[result.formula_id]
        assert stored.result == result.result, result.formula_id
        assert stored.inputs == result.inputs, result.formula_id
