"""One plan run, all twenty nodes, four gates — and the critique over what it wrote.

§17 PQ3 says golden `PlanInput` fixtures produce plans that pass every §11
assertion. `tests/eval/test_plan_eval.py` holds that over a **deterministic
stand-in** for the DAG, because no model may be called on every run; and until
S2-P7 the furthest any integration harness drove the real thing was gate G3.
So the most expensive claim in the phase rested entirely on a fixture builder
being right about what the nodes produce.

This closes that. The real executor, the real calc engine, the real assembler,
the real critique, a real Postgres, four gates decided by a real approver —
scripted only where a model would otherwise speak, and scripted with answers
that carry no figures at all.

**It asserts on the stored payload, not on the model the run held.** The worker
validates `campaign_plan.payload` out of JSONB, and a field that survives in
memory and dies in JSON is a class of defect this project has already shipped
once. So the plan is re-validated through `CampaignPlan` from the row and the
checks run over *that*.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import CampaignPlan, CampaignPlanStatus, PlanCalc
from agent.export.plan_contract import CampaignPlan as PlanContract
from agent.planning import critique
from tests.integration.conftest import ApiClient
from tests.integration.plan_gates import run_whole_dag
from tests.openrouter_fake import FakeOpenRouter

pytestmark = pytest.mark.anyio


async def _stored(db: AsyncSession, plan_run_id: uuid.UUID) -> CampaignPlan:
    db.expire_all()
    return (
        await db.execute(sa.select(CampaignPlan).where(CampaignPlan.plan_run_id == plan_run_id))
    ).scalar_one()


async def test_a_whole_plan_run_produces_a_plan_the_critique_accepts(
    admin: ApiClient, accepted: dict[str, Any], db: AsyncSession
) -> None:
    """PQ3, against the product rather than against a fixture of it."""
    fake = FakeOpenRouter()
    plan_run_id = await run_whole_dag(admin, accepted, fake)

    row = await _stored(db, plan_run_id)
    assert row.status in {
        CampaignPlanStatus.READY_TO_FREEZE,
        CampaignPlanStatus.DRAFT,
    }, f"the run finished but the plan is {row.status.value}"

    plan = PlanContract.model_validate(row.payload)
    issues = critique.run_checks(plan)
    blocking = [issue for issue in issues if issue.severity == "blocking"]
    assert blocking == [], "\n".join(f"{i.check}: {i.finding}" for i in blocking)


async def test_the_run_writes_a_plan_whose_every_figure_has_a_calculation(
    admin: ApiClient, accepted: dict[str, Any], db: AsyncSession
) -> None:
    """PT1 end to end: every `Number` in the stored payload resolves to a row.

    The unit half asserts that `Number.calc_evidence_id` is required, which is
    a type. This asserts the ids in a real payload are ids of `PlanCalc` rows
    this run actually wrote — the thing a reader clicking a figure needs.
    """
    fake = FakeOpenRouter()
    plan_run_id = await run_whole_dag(admin, accepted, fake)

    row = await _stored(db, plan_run_id)
    plan = PlanContract.model_validate(row.payload)
    numbers = plan.numbers()
    assert numbers, "the plan states no traceable figure at all"

    written = {
        evidence_id
        for (evidence_id,) in (
            await db.execute(
                sa.select(PlanCalc.evidence_id).where(PlanCalc.plan_run_id == plan_run_id)
            )
        ).all()
    }
    assert written, "the run wrote no PlanCalc rows"
    unresolved = sorted(
        {
            number.label or str(number.value)
            for number in numbers
            if number.calc_evidence_id not in written
        }
    )
    assert unresolved == [], f"figures citing a calculation this run never wrote: {unresolved}"


async def test_the_plan_is_freezable_once_every_gate_is_approved(
    admin: ApiClient, accepted: dict[str, Any], db: AsyncSession
) -> None:
    """The end of §12.2's state machine, reached rather than seeded.

    Every freeze test in the suite builds its plan row by hand. This one is the
    plan the DAG produced, which is the only version of it that proves the
    gates, the critique and the freeze agree about what "ready" means.
    """
    fake = FakeOpenRouter()
    plan_run_id = await run_whole_dag(admin, accepted, fake)

    response = await admin.post(f"/plans/{plan_run_id}/freeze", json={"confirm_version": 1})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["status"] == CampaignPlanStatus.FROZEN.value

    row = await _stored(db, plan_run_id)
    assert row.status is CampaignPlanStatus.FROZEN
    assert row.frozen_at is not None
    assert len(row.frozen_approval_ids or []) == 4, "the four gates were not sealed onto the plan"


async def test_pf2_a_decided_gate_requeues_the_run_inside_the_same_request(
    admin: ApiClient, accepted: dict[str, Any], db: AsyncSession
) -> None:
    """§17 PF2: five seconds from a gate approval to the branch resuming.

    Five seconds is a budget only if nothing is polling, and the way to show
    that is not a stopwatch — a stopwatch on a laptop measures the laptop. It
    is that the decision response already reports the run as `queued`: the
    resume happened **inside the request**, before the approver's browser got
    an answer, so there is no interval to tune and no cron to wait for.

    The first version of this test grepped `routes_approvals` for the strings
    `_resume(` and `enqueue_run`, which is the same defect the PF4 guard had:
    moving resumption onto a poller while keeping the call would pass it.
    """
    from tests.integration.plan_gates import run_to_g1

    fake = FakeOpenRouter()
    plan_run_id, _ = await run_to_g1(admin, accepted, fake)

    inbox = (await admin.get(f"/approvals?run_id={plan_run_id}")).json()["items"]
    gate = next(item for item in inbox if item["gate_key"] == "G1")
    decided = await admin.post(f"/approvals/{gate['id']}", json={"decision": "approve"})

    assert decided.status_code == 200, decided.text
    body = decided.json()
    assert body["resumed"] is True, "deciding a gate no longer resumes the run"
    assert body["run_status"] == "queued", (
        f"the run was {body['run_status']} when the decision returned; PF2's budget "
        "assumes the resume is inside the request, not on a poller"
    )

    # ...and a *rejection* resumes nothing: the branch is dead and §18 says the
    # plan is blocked, not retried. G2 is the run's other open gate, decided on
    # the same run — starting a second plan run here is refused by the project
    # lock (E4), correctly, and that refusal is what the first draft of this
    # test tripped over.
    other = next(item for item in inbox if item["gate_key"] == "G2")
    rejected = await admin.post(
        f"/approvals/{other['id']}",
        json={"decision": "reject", "note": "That is not our lead definition."},
    )
    assert rejected.status_code == 200, rejected.text
    assert rejected.json()["resumed"] is False, "a rejected gate must resume nothing"
