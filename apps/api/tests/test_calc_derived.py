"""`calc/derived.py` — the only writer of `PlanCalc` and `derived` Evidence.

No database here. What is being tested is the *decision* the writer makes — reuse
or write, and what SQL it emits when it writes — and that is all visible from the
statements it hands the session. The dedupe itself, which is a unique constraint,
is proven against real Postgres in `tests/integration/test_plan_calc.py`, because
a constraint that is only asserted in Python is a constraint that does not exist.
"""

from __future__ import annotations

import uuid
from typing import Any, cast

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession

from agent.calc.derived import INPUTS_CONSTRAINT, DerivedWriter
from agent.calc.registry import CalcResult, inputs_hash
from agent.db.models import EvidenceSource, PlanCalc
from agent.evidence.store import EvidenceStore, StoreResult

PROJECT_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
PLAN_RUN_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
EVIDENCE_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")


#: What a `plan_calc` lookup returns: the row id and its citation, or nothing.
#: Scripted as a tuple because that is what `Result.one_or_none()` hands back,
#: and the writer has to be able to tell "no row" from "row, citation pruned".
PLAN_CALC_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
FOUND = (PLAN_CALC_ID, EVIDENCE_ID)
FOUND_WITHOUT_EVIDENCE = (PLAN_CALC_ID, None)
MISSING = None


class FakeResult:
    def __init__(self, value: Any) -> None:
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def one_or_none(self) -> Any:
        return self._value


class FakeSession:
    """Records what it was asked to execute and replays scripted answers."""

    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.statements: list[Any] = []

    async def execute(self, statement: Any) -> FakeResult:
        self.statements.append(statement)
        return FakeResult(self.responses.pop(0) if self.responses else None)


class FakeStore:
    def __init__(self, evidence_ids: list[uuid.UUID] | None = None) -> None:
        self.evidence_ids = [EVIDENCE_ID] if evidence_ids is None else evidence_ids
        self.calls: list[dict[str, Any]] = []

    async def write(
        self,
        drafts: list[Any],
        *,
        project_id: uuid.UUID,
        run_id: uuid.UUID | None = None,
        embed: bool = True,
    ) -> StoreResult:
        self.calls.append(
            {"drafts": drafts, "project_id": project_id, "run_id": run_id, "embed": embed}
        )
        return StoreResult(evidence_ids=list(self.evidence_ids), inserted=len(self.evidence_ids))


def writer(session: FakeSession, store: FakeStore) -> DerivedWriter:
    return DerivedWriter(
        cast(AsyncSession, session),
        store=cast(EvidenceStore, store),
        project_id=PROJECT_ID,
        plan_run_id=PLAN_RUN_ID,
    )


def sample(value: float = 1.0) -> CalcResult:
    """A stand-in result, built directly rather than through `@formula`.

    Registering a `testing.*` formula would leave it in `FORMULAS` for the whole
    session — pytest imports every module at collection — and
    `test_calc_registry.py` asserts that the registry holds exactly the nine
    formulas the PRD names. `CalcResult` is a plain frozen dataclass, so the
    writer can be tested without touching the registry at all.
    """
    inputs = {"value": value}
    return CalcResult(
        formula_id="testing.derived_v1",
        calc_version="calc/1.0+constants/2026.09.1",
        kind="calc_economics",
        inputs=inputs,
        inputs_hash=inputs_hash(inputs),
        result={"doubled": value * 2},
        summary=f"doubled {value} to {value * 2}",
    )


def compiled(statement: Any) -> tuple[str, dict[str, Any]]:
    rendered = statement.compile(dialect=postgresql.dialect())
    return str(rendered), dict(rendered.params)


async def test_a_new_calculation_writes_evidence_then_the_plan_calc_row() -> None:
    session = FakeSession(MISSING, EVIDENCE_ID)
    store = FakeStore()
    result = sample(21)

    returned = await writer(session, store).record(result, node_id="2.1.2")

    assert returned == EVIDENCE_ID
    assert len(store.calls) == 1
    assert len(session.statements) == 2  # the lookup, then the insert


async def test_the_evidence_row_is_a_derived_row_carrying_the_whole_calculation() -> None:
    """PRD §7.3: payload is the calculation, content_text is a line a person reads."""
    session = FakeSession(MISSING, EVIDENCE_ID)
    store = FakeStore()
    result = sample(21)

    await writer(session, store).record(result, node_id="2.1.2")

    draft = store.calls[0]["drafts"][0]
    assert draft.source == EvidenceSource.DERIVED
    assert draft.kind == "calc_economics"
    assert draft.payload == {
        "formula_id": "testing.derived_v1",
        "calc_version": "calc/1.0+constants/2026.09.1",
        "inputs_hash": result.inputs_hash,
        "inputs": {"value": 21},
        "result": {"doubled": 42},
    }
    assert draft.content_text == "doubled 21 to 42"
    assert store.calls[0]["project_id"] == PROJECT_ID
    assert store.calls[0]["run_id"] == PLAN_RUN_ID


async def test_the_insert_is_conflict_tolerant_and_reads_the_winner_back() -> None:
    """Two nodes in one wave can call the same formula on the same inputs."""
    session = FakeSession(MISSING, EVIDENCE_ID)
    await writer(session, FakeStore()).record(sample(), node_id="2.1.2")

    sql, params = compiled(session.statements[1])
    assert "INSERT INTO plan_calc" in sql
    assert "ON CONFLICT ON CONSTRAINT uq_plan_calc_run_formula_inputs DO NOTHING" in sql
    assert "RETURNING plan_calc.evidence_id" in sql
    assert params["formula_id"] == "testing.derived_v1"
    assert params["node_id"] == "2.1.2"
    assert params["calc_version"] == "calc/1.0+constants/2026.09.1"
    assert params["inputs_hash"] == sample().inputs_hash
    assert params["plan_run_id"] == PLAN_RUN_ID
    assert params["evidence_id"] == EVIDENCE_ID


async def test_the_lookup_is_keyed_on_exactly_the_unique_constraint() -> None:
    session = FakeSession(FOUND)
    await writer(session, FakeStore()).record(sample(), node_id="2.1.2")

    sql, params = compiled(session.statements[0])
    assert "SELECT plan_calc.id, plan_calc.evidence_id" in sql
    assert "plan_calc.plan_run_id = " in sql
    assert "plan_calc.formula_id = " in sql
    assert "plan_calc.inputs_hash = " in sql
    # node_id is deliberately absent: the same calculation asked for by two
    # nodes is one calculation, and both should cite the same evidence.
    assert "plan_calc.node_id" not in sql
    assert set(params.values()) >= {PLAN_RUN_ID, "testing.derived_v1", sample().inputs_hash}


async def test_an_already_recorded_calculation_is_reused_and_writes_nothing() -> None:
    session = FakeSession(FOUND)
    store = FakeStore()

    returned = await writer(session, store).record(sample(), node_id="2.1.2")

    assert returned == EVIDENCE_ID
    assert store.calls == []
    assert len(session.statements) == 1


async def test_a_second_node_asking_for_the_same_calculation_cites_the_same_evidence() -> None:
    session = FakeSession(MISSING, EVIDENCE_ID, FOUND)
    store = FakeStore()
    subject = writer(session, store)

    first = await subject.record(sample(7), node_id="2.1.2")
    second = await subject.record(sample(7), node_id="2.2.3")

    assert first == second == EVIDENCE_ID
    assert len(store.calls) == 1


async def test_different_inputs_are_different_calculations() -> None:
    assert sample(1).inputs_hash != sample(2).inputs_hash


async def test_losing_the_insert_race_returns_the_winners_evidence_id() -> None:
    other = uuid.uuid4()
    # lookup: nothing -> insert: conflicted (None) -> re-lookup: the winner
    session = FakeSession(MISSING, None, (PLAN_CALC_ID, other))

    returned = await writer(session, FakeStore()).record(sample(), node_id="2.1.2")

    assert returned == other
    assert len(session.statements) == 3


async def test_a_conflict_with_nothing_to_read_back_raises_rather_than_returning_none() -> None:
    session = FakeSession(MISSING, None, MISSING)
    with pytest.raises(RuntimeError, match="conflicted but no row could be read back"):
        await writer(session, FakeStore()).record(sample(), node_id="2.1.2")


async def test_an_evidence_store_that_writes_nothing_raises() -> None:
    session = FakeSession(MISSING)
    with pytest.raises(RuntimeError, match="the evidence store wrote nothing"):
        await writer(session, FakeStore(evidence_ids=[])).record(sample(), node_id="2.1.2")


async def test_record_all_keeps_the_order_the_node_asked_in() -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    session = FakeSession(MISSING, first, MISSING, second)
    store = FakeStore(evidence_ids=[first])

    returned = await writer(session, store).record_all("2.1.2", [sample(1), sample(2)])

    assert returned == [first, second]
    assert len(store.calls) == 2


async def test_nothing_is_committed_so_the_node_owns_the_transaction() -> None:
    """A node's calculations and its NodeRun row land or roll back together."""
    session = FakeSession(MISSING, EVIDENCE_ID)
    await writer(session, FakeStore()).record(sample(), node_id="2.1.2")
    assert not hasattr(session, "committed")


async def test_a_calculation_whose_evidence_was_pruned_is_recited_not_rewritten() -> None:
    """`evidence_id` is nullable, so the row outlives the Evidence it produced.

    Writing a second `plan_calc` row would violate the unique constraint; doing
    nothing would hand the node a citation of None. The row keeps its place and
    gets a fresh citation, because the formula, the inputs and the constants
    version are the provenance and none of them changed.
    """
    session = FakeSession(FOUND_WITHOUT_EVIDENCE)
    store = FakeStore()

    returned = await writer(session, store).record(sample(), node_id="2.1.2")

    assert returned == EVIDENCE_ID
    assert len(store.calls) == 1
    sql, params = compiled(session.statements[1])
    assert sql.startswith("UPDATE plan_calc SET evidence_id=")
    assert params["id_1"] == PLAN_CALC_ID
    assert params["evidence_id"] == EVIDENCE_ID


def test_the_unique_constraint_the_dedupe_depends_on_exists_on_the_model() -> None:
    """Named, because `derived.py` targets it by name in `ON CONFLICT`.

    `INPUTS_CONSTRAINT` and the migration have to agree; if they drift, the
    insert degrades to one two concurrent nodes can both win.
    """
    constraint = next(
        item
        for item in PlanCalc.__table__.constraints
        if getattr(item, "name", None) == INPUTS_CONSTRAINT
    )
    assert [column.name for column in constraint.columns] == [
        "plan_run_id",
        "formula_id",
        "inputs_hash",
    ]


def test_the_row_keeps_its_provenance_when_its_citation_is_deleted() -> None:
    """Pruning an Evidence row must not delete the arithmetic behind a number."""
    assert PlanCalc.__table__.c.evidence_id.nullable is True
    assert PlanCalc.__table__.c.plan_run_id.nullable is False
    keys = {
        column.name: {(key.target_fullname, key.ondelete) for key in column.foreign_keys}
        for column in PlanCalc.__table__.columns
        if column.foreign_keys
    }
    assert keys == {
        "plan_run_id": {("run.id", "CASCADE")},
        "evidence_id": {("evidence.id", "SET NULL")},
    }
