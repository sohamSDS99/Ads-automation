"""The only door from a plan node to `agent/calc/` (Stage 02 PRD §8.1, §9.1).

Global law 14 has two halves. S2-P1 built the half `calc/` can enforce on its
own: a formula cannot run unregistered, cannot mis-state its own identity, and
cannot be persisted without its inputs hash. This module is the other half —
the half that is about *nodes*:

* **A node may only call the formulas it declared.** `NodeSpec.calc` is an
  allow-list, and `run()` refuses anything outside it. A node that grows a new
  calculation has to say so in its spec, where a reviewer reads it, rather than
  in the middle of a `reason()` body where nobody does.
* **A node never touches `derived.py` itself.** `run()` computes, persists and
  cites in one call, so "I calculated it but forgot to record it" is not a
  reachable state. The `Evidence` row comes back with the result, which is what
  lets `gather()` return it and the executor check the citation against it.
* **Constants arrive from the run, not from the node.** A node that loaded
  `planning_constants.yaml` itself would silently ignore the project's
  overrides, and the plan would be built on different thresholds from the ones
  `Run.input_hash` claims.

What this module deliberately does **not** do is decide anything about the
arithmetic. It looks a formula up by id and calls it. Every judgement about
what the numbers mean lives in `calc/`, where it is unit-tested against
hand-checked fixtures.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.calc.derived import DerivedWriter
from agent.calc.registry import FORMULAS, CalcResult
from agent.db.models import Evidence
from agent.evidence.store import EvidenceStore
from agent.planning.constants import PlanningConstants

if TYPE_CHECKING:  # pragma: no cover - avoids a cycle with nodes.base
    from agent.nodes.base import NodeSpec, PlanContext
    from agent.schemas.plan_input import PlanInput

log = structlog.get_logger(__name__)


class CalcNotPermitted(RuntimeError):
    """A node called a formula its `NodeSpec.calc` does not list.

    A `RuntimeError` rather than a validation error because it is a programming
    mistake, not bad data: the fix is one line in the node's spec, and the
    message says which line.
    """

    def __init__(self, node_id: str, formula_id: str, permitted: tuple[str, ...]) -> None:
        allowed = ", ".join(permitted) if permitted else "nothing — its `calc` list is empty"
        super().__init__(
            f"node {node_id} called {formula_id!r}, which is not in its NodeSpec.calc. "
            f"It may call: {allowed}."
        )
        self.node_id = node_id
        self.formula_id = formula_id
        self.permitted = permitted


@dataclass(frozen=True, slots=True)
class Calculation:
    """One calculation, its `PlanCalc` row written and its citation minted.

    Carries the `Evidence` row rather than only its id so that `gather()` can
    return it. The executor checks `calc_evidence_ids` against the `derived`
    rows the node gathered, so a calculation the node never handed back is a
    calculation it cannot cite — which is the property that makes the check
    mean something.
    """

    result: CalcResult
    evidence: Evidence

    @property
    def id(self) -> uuid.UUID:
        """What the node puts in `calc_evidence_ids`."""
        return self.evidence.id

    @property
    def value(self) -> dict[str, Any]:
        """The formula's `result` payload. Numbers a node may copy, never compute."""
        return self.result.result

    @property
    def summary(self) -> str:
        """The one-line human rendering, for a prompt or a progress line."""
        return self.result.summary


class PlanCalcRunner:
    """The arithmetic layer, scoped to one node of one plan run."""

    def __init__(
        self,
        *,
        node_id: str,
        permitted: tuple[str, ...],
        writer: DerivedWriter,
        constants: PlanningConstants,
        session: AsyncSession,
    ) -> None:
        self.node_id = node_id
        self.permitted = permitted
        self.writer = writer
        self.constants = constants
        self.session = session
        #: Every calculation this node has made, in call order. The node hands
        #: these to `gather()`; nothing else reads it.
        self.made: list[Calculation] = []

    async def run(self, formula_id: str, *args: Any, **kwargs: Any) -> Calculation:
        """Compute, persist and cite. The only way a node produces a number.

        `constants` is injected unless the caller passed its own — a node that
        wants the run's constants (all of them do) should not have to remember
        to thread them through, and a node that passes something else is doing
        so visibly.
        """
        if formula_id not in self.permitted:
            raise CalcNotPermitted(self.node_id, formula_id, self.permitted)
        spec = FORMULAS.get(formula_id)
        if spec is None:  # pragma: no cover — NodeSpec validates the allow-list at import
            raise CalcNotPermitted(self.node_id, formula_id, self.permitted)

        kwargs.setdefault("constants", self.constants)
        result = spec.fn(*args, **kwargs)
        evidence_id = await self.writer.record(result, node_id=self.node_id)
        evidence = await self.session.get(Evidence, evidence_id)
        if evidence is None:  # pragma: no cover — just written in this transaction
            raise RuntimeError(
                f"{formula_id}: evidence {evidence_id} was recorded and cannot be read back"
            )

        calculation = Calculation(result=result, evidence=evidence)
        self.made.append(calculation)
        log.info(
            "plan.calc",
            node_id=self.node_id,
            formula_id=formula_id,
            calc_version=result.calc_version,
            evidence_id=str(evidence_id),
            excluded=len(result.excluded),
        )
        return calculation

    @property
    def evidence(self) -> list[Evidence]:
        """Every `derived` row this node produced, for `gather()` to return."""
        return [item.evidence for item in self.made]


@dataclass(frozen=True, slots=True)
class PlanResources:
    """What one plan run knows, independent of which node is executing.

    Deliberately holds no session. A wave runs up to four nodes at once and
    each has its own `AsyncSession` (`NodeScope`), so a writer built once per
    run would be shared across concurrent tasks — which `AsyncSession` does
    not support, and which P1 never hit only because its DAG was one node
    wide. The session arrives per node, at `context_for`.
    """

    input: PlanInput
    constants: PlanningConstants

    def context_for(
        self,
        spec: NodeSpec,
        *,
        session: AsyncSession,
        store: EvidenceStore,
        project_id: uuid.UUID,
        plan_run_id: uuid.UUID,
    ) -> PlanContext:
        """The `ctx.plan` one node sees, bound to that node's own session."""
        from agent.nodes.base import PlanContext

        return PlanContext(
            input=self.input,
            calc=PlanCalcRunner(
                node_id=spec.id,
                permitted=spec.calc,
                writer=DerivedWriter(
                    session, store=store, project_id=project_id, plan_run_id=plan_run_id
                ),
                constants=self.constants,
                session=session,
            ),
            constants=self.constants,
        )
