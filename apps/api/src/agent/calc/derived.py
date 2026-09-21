"""The only writer of `PlanCalc` and of `derived` Evidence (PRD §9.1 item 3).

Everything else in `agent/calc/` is pure arithmetic with no idea a database
exists — `scripts/check_calc_isolation.py` fails the build if that stops being
true. This module is the single seam between the two, and it does exactly one
thing: given a `CalcResult`, make it citable.

    evidence_id = await writer.record(result, node_id="2.1.2")

That id is what a plan node puts in `calc_evidence_ids`, and it is how a reader
of the finished plan gets from a figure on a page back to the formula, the
inputs, the constants version and the row that produced it.

**Reuse, not recomputation.** `UNIQUE(plan_run_id, formula_id, inputs_hash)`
means the same formula over the same inputs inside one plan run resolves to the
row that already exists. A node that runs twice — a retry, a repair pass, a
resumed run after a worker restart — cites the same evidence the first attempt
did instead of filling the Evidence Explorer with identical rows.

**The transaction belongs to the caller.** Nothing here commits, exactly as
`EvidenceStore` does not, so a node's calculations and its `NodeRun` row land or
roll back together.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from agent.calc.registry import CalcResult
from agent.db.models import EvidenceSource, PlanCalc
from agent.evidence.normalize import EvidenceDraft
from agent.evidence.store import EvidenceStore

log = structlog.get_logger(__name__)


class DerivedWriter:
    """Persists calculations for one plan run and hands back citable ids."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        store: EvidenceStore,
        project_id: uuid.UUID,
        plan_run_id: uuid.UUID,
    ) -> None:
        self.session = session
        self.store = store
        self.project_id = project_id
        self.plan_run_id = plan_run_id

    async def record(self, result: CalcResult, *, node_id: str) -> uuid.UUID:
        """Write `result` if this plan run has not already computed it, and cite it."""
        existing = await self._lookup(result)
        if existing is not None:
            log.info(
                "calc.reused",
                plan_run_id=str(self.plan_run_id),
                node_id=node_id,
                formula_id=result.formula_id,
                evidence_id=str(existing),
            )
            return existing

        draft = EvidenceDraft(
            source=EvidenceSource.DERIVED,
            kind=result.kind,
            payload=result.payload(),
            # PRD §7.3: a one-line human rendering, so a calculation is findable
            # in the Evidence Explorer next to the facts it was computed from.
            content_text=result.summary,
        )
        written = await self.store.write(
            [draft], project_id=self.project_id, run_id=self.plan_run_id
        )
        if not written.evidence_ids:  # pragma: no cover - the store always returns one
            raise RuntimeError(f"{result.formula_id}: the evidence store wrote nothing")
        evidence_id = written.evidence_ids[0]

        # `ON CONFLICT DO NOTHING` rather than a pre-flight check: two nodes in
        # the same wave can call the same formula on the same inputs, and the
        # loser should learn the winner's id instead of raising.
        statement = (
            pg_insert(PlanCalc)
            .values(
                id=uuid.uuid4(),
                plan_run_id=self.plan_run_id,
                node_id=node_id,
                formula_id=result.formula_id,
                calc_version=result.calc_version,
                inputs=result.inputs,
                inputs_hash=result.inputs_hash,
                result=result.result,
                evidence_id=evidence_id,
            )
            .on_conflict_do_nothing(constraint="uq_plan_calc_inputs")
            .returning(PlanCalc.evidence_id)
        )
        inserted = (await self.session.execute(statement)).scalar_one_or_none()
        if inserted is None:
            raced = await self._lookup(result)
            if raced is None:  # pragma: no cover - only reachable if the row vanished
                raise RuntimeError(
                    f"{result.formula_id}: the insert conflicted but no row could be read back"
                )
            return raced

        log.info(
            "calc.recorded",
            plan_run_id=str(self.plan_run_id),
            node_id=node_id,
            formula_id=result.formula_id,
            calc_version=result.calc_version,
            evidence_id=str(evidence_id),
            excluded=len(result.excluded),
        )
        return inserted

    async def record_all(self, node_id: str, results: list[CalcResult]) -> list[uuid.UUID]:
        """`record` over several results, in order. Convenience for a node's output."""
        return [await self.record(result, node_id=node_id) for result in results]

    async def _lookup(self, result: CalcResult) -> uuid.UUID | None:
        found = await self.session.execute(
            sa.select(PlanCalc.evidence_id).where(
                PlanCalc.plan_run_id == self.plan_run_id,
                PlanCalc.formula_id == result.formula_id,
                PlanCalc.inputs_hash == result.inputs_hash,
            )
        )
        return found.scalar_one_or_none()
