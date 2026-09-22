"""PR2 for a plan run: zero completed nodes re-execute after a crash.

`test_run_resume.py` holds this for Stage 01 and the executor is shared (law
19), so the obvious position is that Stage 02 gets it for free. It does not,
and the reason is specific: a plan run writes `PlanCalc` rows under a unique
constraint on `(plan_run_id, formula_id, inputs_hash)`. A resumed node that
re-ran a formula it had already recorded would not merely cost money twice — it
would hit that constraint and fail the run, which is a different failure in a
different place with a 500 at the end of it.

**The crash is reproduced as a state, not as an exception.** Stage 01's version
kills the HTTP transport mid-DAG, which works there because its early waves are
one node wide. Stage 02's wave 2 is four nodes running concurrently, so a
`BaseException` from the transport lands in three coroutines that are still
mid-flight and what propagates out of the task group is not what went in — the
first attempt at this test watched the executor retry a killed node twice and
then fail the run on an integrity error, which measures the harness rather than
the product.

So: run the plan until the gates halt it, put the run row back to `running` —
which is exactly the state a killed worker leaves behind, and exactly what the
startup reaper finds — and execute again. What is under test is the
checkpoint, and the checkpoint does not care how the process died.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from agent.db.models import Approval, NodeRun, NodeRunStatus, PlanCalc, Run, RunStatus
from tests.integration.conftest import ApiClient
from tests.integration.runs_support import by_output_model, execute
from tests.openrouter_fake import FakeOpenRouter

pytestmark = pytest.mark.anyio


async def node_rows(db: AsyncSession, run_id: uuid.UUID) -> list[NodeRun]:
    result = await db.execute(
        sa.select(NodeRun)
        .where(NodeRun.run_id == run_id)
        .order_by(NodeRun.node_id, NodeRun.attempt)
    )
    return list(result.scalars().all())


async def calc_keys(db: AsyncSession, run_id: uuid.UUID) -> list[tuple[str, str]]:
    result = await db.execute(
        sa.select(PlanCalc.formula_id, PlanCalc.inputs_hash).where(PlanCalc.plan_run_id == run_id)
    )
    return [(formula, inputs) for formula, inputs in result.all()]


async def approvals(db: AsyncSession, run_id: uuid.UUID) -> list[Approval]:
    result = await db.execute(sa.select(Approval).where(Approval.run_id == run_id))
    return list(result.scalars().all())


async def _crash(db: AsyncSession, run_id: uuid.UUID) -> None:
    """What a killed worker leaves behind: completed nodes, and a `running` row.

    Written with a bare UPDATE rather than through the ORM: the point is that
    nothing tidied up, so nothing may be flushed from a session that knows
    better.
    """
    await db.execute(sa.update(Run).where(Run.id == run_id).values(status=RunStatus.RUNNING))
    await db.commit()
    db.expire_all()


async def test_a_plan_run_killed_mid_dag_re_executes_nothing_it_finished(
    admin: ApiClient, accepted: dict[str, Any], db: AsyncSession
) -> None:
    """PR2, and the `PlanCalc` constraint that makes it more than a cost rule."""
    from tests.integration.plan_gates import run_to_g1

    fake = FakeOpenRouter()
    plan_run_id, script = await run_to_g1(admin, accepted, fake)

    db.expire_all()
    before = await node_rows(db, plan_run_id)
    done = [row for row in before if row.status is NodeRunStatus.SUCCEEDED]
    assert len(done) >= 4, f"only {len(done)} nodes finished, so this asserts very little"
    finished = {row.node_id: row.finished_at for row in done}
    calcs_before = await calc_keys(db, plan_run_id)
    assert calcs_before, "no PlanCalc row was written, so the reuse claim is untested"

    await _crash(db, plan_run_id)

    # A *fresh* provider, so its request count is exactly what the resume ran.
    resumed = FakeOpenRouter()
    by_output_model(resumed, script)
    result = await execute(plan_run_id, resumed)
    assert result.error is None, result.error

    db.expire_all()
    after = await node_rows(db, plan_run_id)

    # 1. No second attempt of anything that had already succeeded.
    for node_id, at in finished.items():
        attempts = [row for row in after if row.node_id == node_id]
        assert len(attempts) == 1, f"{node_id} was re-executed on resume"
        assert attempts[0].status is NodeRunStatus.SUCCEEDED
        assert attempts[0].finished_at == at

    # 2. Nothing was paid for twice.
    assert len(resumed.requests) == result.nodes_executed

    # 3. The calculations the first pass recorded are still single rows. A
    #    recomputation would either duplicate them or raise on the unique
    #    constraint; both are failures and neither would be silent.
    keys = await calc_keys(db, plan_run_id)
    assert len(keys) == len(set(keys)), "a formula was recorded twice for one run"
    assert set(calcs_before) <= set(keys)


async def test_a_gate_already_open_is_not_asked_a_second_time(
    admin: ApiClient, accepted: dict[str, Any], db: AsyncSession
) -> None:
    """§18: "gates already decided are not re-asked".

    The expensive version of this failure is not the re-ask — it is that the
    approval row *is* the record of who agreed to spend the money, and a second
    row for the same gate makes "who agreed to this" ambiguous at exactly the
    moment §17 PS4 says it must not be.
    """
    from tests.integration.plan_gates import run_to_g1

    fake = FakeOpenRouter()
    plan_run_id, script = await run_to_g1(admin, accepted, fake)

    db.expire_all()
    opened = await approvals(db, plan_run_id)
    assert opened, "the run did not halt on a gate, so this asserts nothing"
    before = sorted((row.gate_key or "", str(row.id)) for row in opened)

    await _crash(db, plan_run_id)
    resumed = FakeOpenRouter()
    by_output_model(resumed, script)
    await execute(plan_run_id, resumed)

    db.expire_all()
    again = await approvals(db, plan_run_id)
    assert sorted((row.gate_key or "", str(row.id)) for row in again) == before


async def test_the_resumed_run_halts_on_the_same_gates_rather_than_finishing(
    admin: ApiClient, accepted: dict[str, Any], db: AsyncSession
) -> None:
    """The control for the two above.

    If a resume executed nothing *and* the run came back `succeeded`, the first
    test would pass while proving that resumption is broken in the other
    direction. The run must come back where it was: waiting on a person.
    """
    from tests.integration.plan_gates import run_to_g1

    fake = FakeOpenRouter()
    plan_run_id, script = await run_to_g1(admin, accepted, fake)
    await _crash(db, plan_run_id)

    resumed = FakeOpenRouter()
    by_output_model(resumed, script)
    result = await execute(plan_run_id, resumed)
    assert result.status is RunStatus.AWAITING_APPROVAL
    assert result.nodes_executed == 0, "a resume re-ran work that was already done"
    assert len(resumed.requests) == 0, "a resume paid a model for a node that had finished"
