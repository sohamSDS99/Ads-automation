"""The executor (PRD §7.2).

One entry point, `RunExecutor.execute(run_id)`, driven by the arq worker. It is
written to be re-entrant: calling it again after a crash, a cancel or a
`retry-failed` picks up from the last checkpoint and re-executes nothing that
already succeeded (PRD §15 NF3).

The loop per PRD §7.2:

* topological **wavefront**, `Semaphore(4)` inside a wave;
* **3 attempts** per node, `2^n · 1.5s` backoff with jitter, and one **repair
  pass** inside the gateway before an attempt is counted;
* a **checkpoint** — one committed `NodeRun` — before anything else happens;
* **cancel** checked between nodes, via the Redis flag;
* **budget** enforced after every node, aborting the rest of the run.

A node that fails after its attempts takes its own branch down (its descendants
are recorded `skipped`) and leaves every independent branch running.

A **gate** node stops its branch without failing it: its output becomes an
`Approval` for a human, its `NodeRun` waits in `awaiting_approval`, and its
descendants are left untouched — no `skipped` rows, because they are not
skipped, they are waiting. When every other branch has finished, the pass ends
by asking `approvals.park()` whether the gate is still open; if it was decided
while the last waves ran, the loop simply goes round again and picks it up.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import random
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
import structlog
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.config import Settings, get_settings
from agent.connectors import assert_read_only
from agent.credentials import MissingCredential, resolve_values
from agent.db.models import (
    Approval,
    Base,
    CredentialKind,
    Evidence,
    HumanTaskBlocking,
    NodeRun,
    NodeRunStatus,
    Project,
    Run,
    RunStage,
    RunStatus,
    Workspace,
)
from agent.db.session import get_sessionmaker
from agent.evidence.store import EvidenceStore
from agent.guidelines import tasks as human_tasks
from agent.llm.gateway import LLMAuthError, LLMGateway, build_gateway
from agent.llm.ledger import BudgetExceeded, RunLedger
from agent.llm.router import ModelRouter
from agent.nodes.base import (
    CreativeResources,
    Node,
    NodeContractError,
    NodeSpec,
    NodeTelemetry,
    PlanContext,
    RunContext,
    collect_calc_evidence_ids,
    collect_evidence_ids,
    derived_ids,
)
from agent.notify.email import send_approval_request
from agent.orchestrator import approvals, creative_run
from agent.orchestrator.budget import resolve_cost_cap
from agent.orchestrator.dag import Dag, get_dag
from agent.orchestrator.events import EventType, RunEventStream
from agent.orchestrator.heartbeat import RunHeartbeat
from agent.orchestrator.plan_calc import PlanResources
from agent.orchestrator.plan_input import PlanInputError, build_plan_input_for_run, constants_for
from agent.orchestrator.registry import NodeRegistry, get_registry
from agent.orchestrator.state import TERMINAL_STATUSES, CancelFlag, RunLock, RunStore, utcnow
from agent.planning.constants import ConstantsError

log = structlog.get_logger(__name__)

#: `NodeContractError` is re-exported rather than declared: it moved to
#: `nodes/base.py` when `RunContext` gained a reason to raise it, and
#: everything that imported it from here still can. One class, one message
#: style, two import paths.
__all__ = [
    "ExecutionResult",
    "NodeContractError",
    "NodeFailed",
    "NodeHalted",
    "RunCancelled",
    "RunExecutor",
    "gate_wanted",
]

#: Independent nodes inside one wave (PRD §7.2 item 2).
WAVE_CONCURRENCY = 4

#: Attempts per node, including the first (PRD §7.2 item 3).
MAX_NODE_ATTEMPTS = 3

#: `2^n * 1.5s`, jittered.
BACKOFF_BASE_SECONDS = 1.5

#: How many times one `execute()` call re-enters the wave loop after a gate was
#: decided mid-pass. Each pass strictly advances — a decided gate is `succeeded`
#: and never re-opens — so this is a backstop against a bug, not a real limit.
MAX_GATE_PASSES = 20


class RunCancelled(RuntimeError):
    """The cancel flag was set (PRD §7.2 item 6)."""


def gate_wanted(node: Node, ctx: RunContext, output: BaseModel) -> bool:
    """Does this node have a question for a human on *this* run?

    Every gate before Stage 03 answered "yes, always", and the executor asked
    `spec.gate` directly. 3.5.1 is the first that can legitimately have nothing
    to ask: when a current `SignOffMatrix` already names the three owners, the
    answer is on file and halting would mean asking somebody to re-approve
    their own unchanged decision (PRD §11).

    A free function, and it takes the node rather than living on it, so the
    decision can be tested without a database, a Redis, a worker and a model —
    the integration suite proves it is wired in, and the unit suite proves it
    decides correctly.

    The node is handed its validated **output model**, not the JSON payload:
    it answers from what it just produced, with its own types, rather than
    re-reading the world and possibly disagreeing with the row it is about to
    checkpoint.
    """
    spec = node.spec
    if not spec.gate:
        return False
    if not spec.gate_conditional:
        return True
    # `gate_conditional` without `gate_required()` is refused by the registry at
    # import time, so this attribute is present by the time a run reaches here.
    return bool(node.gate_required(ctx, output))  # type: ignore[attr-defined]


def task_wanted(node: Node, ctx: RunContext, output: BaseModel) -> bool:
    """Does this person-task node have somebody to ask on *this* run?

    The twin of `gate_wanted`, and a separate function for the reason §8.4
    gives: an approval and a person-task are different primitives. Folding them
    together here would be the first place the distinction started to blur.

    H1 always has somebody to ask — reaching 3.2.3 means there are claims and
    they need signing. H2 does not: §11 requires 3.3.2 to emit
    `status='not_required'` and create **no task** when 3.3.1 found nothing
    requiring verification. Without this, `_open_human_task` would raise on the
    missing `assignee_id` and fail a run whose correct outcome is "nothing to
    do here", which is the opposite of what the phase asks for.
    """
    spec = node.spec
    if not spec.human_task_key:
        return False
    if not spec.human_task_conditional:
        return True
    # `human_task_conditional` without `task_required()` is refused by the
    # registry at import time, so the attribute is present by the time a run
    # reaches here.
    return bool(node.task_required(ctx, output))  # type: ignore[attr-defined]


class NodeHalted(RuntimeError):
    """A gate node produced its proposal and is waiting for a human (PRD §7.2.5).

    Not a failure, and deliberately not a return value: it has to unwind the
    same `_attempt_node` path a failure does so no checkpoint or event is
    written after it.
    """

    def __init__(self, node_id: str, approval_id: uuid.UUID) -> None:
        super().__init__(f"node {node_id} is awaiting approval {approval_id}")
        self.node_id = node_id
        self.approval_id = approval_id


class NodeFailed(RuntimeError):
    """A node exhausted its attempts."""

    def __init__(self, node_id: str, error: dict[str, Any]) -> None:
        super().__init__(f"node {node_id} failed: {error.get('message')}")
        self.node_id = node_id
        self.error = error


@dataclass(slots=True)
class NodeScope:
    """One node's own database session, and the rows it reaches through it.

    A wave runs up to `WAVE_CONCURRENCY` nodes at once and each of them
    checkpoints as it goes, so they cannot share a session: `AsyncSession` is
    not safe for concurrent use, and two `commit()` calls overlapping raise
    `IllegalStateChangeError` mid-run. P1 never saw this because its DAG was two
    waves of one node; the real DAG has a wave of six.

    `run` and `project` are re-read into this session rather than passed across
    from the executor's, so nothing in a node's path touches an object owned by
    another task's transaction.
    """

    db: AsyncSession
    store: RunStore
    run: Run
    project: Project


@dataclass(frozen=True, slots=True)
class NodeOutcome:
    """What one node did inside a wave.

    Three states, not two: succeeded (`output` is set), failed (`output` is
    None), and halted on a gate. Collapsing the third into either of the others
    is what would make a gate either fail its branch or silently pass it.
    """

    node_id: str
    output: dict[str, Any] | None = None
    halted: bool = False


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What the worker reports back."""

    run_id: uuid.UUID
    status: RunStatus
    cost_usd: Decimal
    nodes_executed: int
    error: dict[str, Any] | None = None
    #: Gate nodes this pass left waiting on a human.
    awaiting: tuple[str, ...] = ()


class RunExecutor:
    """Executes one run. Built per job, never shared between runs."""

    def __init__(
        self,
        *,
        db: AsyncSession,
        redis: Redis,
        settings: Settings | None = None,
        registry: NodeRegistry | None = None,
        dag: Dag | None = None,
        gateway: LLMGateway | None = None,
        http_client: httpx.AsyncClient | None = None,
        backoff_base: float = BACKOFF_BASE_SECONDS,
        max_attempts: int = MAX_NODE_ATTEMPTS,
        sessions: async_sessionmaker[AsyncSession] | None = None,
    ) -> None:
        self.db = db
        self._sessions = sessions or get_sessionmaker()
        self.redis = redis
        self.settings = settings or get_settings()
        self.registry = registry or get_registry()
        # Resolved per run in `execute()`, from `Run.stage`: one executor drives
        # both pipelines and the graph is a property of the run, not of the
        # process. An explicit `dag=` still wins, which is how tests pin one.
        self._dag_override = dag
        self.dag = dag or get_dag(RunStage.RESEARCH)
        self.store = RunStore(db)
        # Re-bound to the run's stage in `execute()`; a plan run and a
        # research run on one project hold different keys.
        self.lock = RunLock(redis)
        self.cancel = CancelFlag(redis)
        # Resolved per run in `execute()`, like `dag` and `lock`. None on a
        # research run; a plan run cannot start without it, so a plan node
        # never sees the None.
        self._plan: PlanResources | None = None
        #: The creative twin of `_plan` (Stage 04 PRD §4.3): resolved once per
        #: creative run in `execute()`, None on every other stage.
        self._creative: CreativeResources | None = None
        self._gateway = gateway
        self._http_client = http_client
        self._backoff_base = backoff_base
        self._max_attempts = max_attempts
        self._executed = 0

    # -- entry point -------------------------------------------------------

    async def execute(self, run_id: uuid.UUID) -> ExecutionResult:
        loaded = await self.store.load(run_id)
        if loaded is None:
            log.warning("run.missing", run_id=str(run_id))
            return ExecutionResult(run_id, RunStatus.FAILED, Decimal(0), 0, {"code": "not_found"})
        run, project = loaded
        if self._dag_override is None:
            self.dag = get_dag(run.stage)
        self.lock = RunLock(self.redis, run.stage)

        if run.status in TERMINAL_STATUSES:
            # arq redelivered a job for a run that already ended. Doing nothing
            # is the whole point of making this idempotent.
            log.info("run.already_final", run_id=str(run_id), status=run.status)
            return ExecutionResult(run_id, run.status, run.cost_usd, 0)

        events = RunEventStream(self.redis, run_id)
        ledger = RunLedger(
            cap_usd=await self._budget_cap(project, stage=run.stage),
            spent_usd=Decimal(run.cost_usd),
        )

        client: httpx.AsyncClient | None = None
        try:
            gateway, client = await self._build_gateway(run, project)
        except (MissingCredential, LLMAuthError) as exc:
            return await self._abort(
                run, events, ledger, RunStatus.FAILED, {"code": "credential", "message": str(exc)}
            )

        try:
            router = await self._build_router(run, project)
        except ValueError as exc:
            if client is not None and self._http_client is None:
                await client.aclose()
            return await self._abort(
                run, events, ledger, RunStatus.FAILED, {"code": "routing", "message": str(exc)}
            )

        if run.stage is RunStage.PLAN:
            try:
                self._plan = await self._plan_resources(run, project)
            except (PlanInputError, ConstantsError) as exc:
                # Law 13: Stage 02 reads Stage 01 only through `PlanInput`.
                # Without one there is nothing to plan from, and every node
                # would fail the same way three times over. Stop here, before
                # a token is spent.
                if client is not None and self._http_client is None:
                    await client.aclose()
                code = getattr(exc, "code", "planning_constants")
                return await self._abort(
                    run, events, ledger, RunStatus.FAILED, {"code": code, "message": str(exc)}
                )

        if run.stage is RunStage.CREATIVE:
            try:
                self._creative = await creative_run.load_resources(self.db, run, project)
            except creative_run.CreativeRunError as exc:
                # §4.3 rule 1: every creative node reads the pinned input and
                # lints against the pinned rules. Without both there is nothing
                # to write from, so stop before a token is spent.
                if client is not None and self._http_client is None:
                    await client.aclose()
                return await self._abort(
                    run, events, ledger, RunStatus.FAILED, {"code": exc.code, "message": str(exc)}
                )

        crashed = await self.store.fail_stale_running(run.id)
        if crashed:
            log.info("run.resumed", run_id=str(run_id), crashed_nodes=crashed)

        await self.store.mark_running(run)
        await events.publish(EventType.RUN_STATUS, run_id=str(run_id), status=RunStatus.RUNNING)

        status = RunStatus.SUCCEEDED
        error: dict[str, Any] | None = None
        awaiting: set[str] = set()
        try:
            # Held across the whole execution, including the gate-pass loop: a
            # run that is going round again is as alive as one in its first
            # wave, and the reaper must not be able to tell them apart.
            async with RunHeartbeat(self.redis, run_id):
                for _ in range(MAX_GATE_PASSES):
                    awaiting = await self._run_waves(
                        run=run,
                        project=project,
                        gateway=gateway,
                        router=router,
                        ledger=ledger,
                        events=events,
                    )
                    if not awaiting:
                        break
                    await self.store.rollup(run, ledger)
                    if await approvals.park(self.db, run):
                        status = RunStatus.AWAITING_APPROVAL
                        break
                    if await human_tasks.park(self.db, run):
                        # Checked after gates and kept separate: a run can hold
                        # both, and parking as `awaiting_approval` when what is
                        # actually outstanding is a named person's signature
                        # would send the wrong people the wrong reminder.
                        status = RunStatus.AWAITING_HUMAN_TASK
                        break
                    # Every gate that halted this pass was decided while the
                    # other branches were still running. Go round again and
                    # execute what those decisions unblocked.
                    log.info(
                        "run.gates_decided_mid_pass", run_id=str(run_id), nodes=sorted(awaiting)
                    )
        except RunCancelled:
            status, error = RunStatus.CANCELLED, {"code": "cancelled"}
        except BudgetExceeded as exc:
            status = RunStatus.FAILED
            error = {
                "code": "budget_exceeded",
                "message": str(exc),
                "spent_usd": str(exc.spent),
                "cap_usd": str(exc.cap),
            }
        except NodeFailed as exc:
            status, error = RunStatus.FAILED, exc.error
        except Exception as exc:  # noqa: BLE001 — a run must always reach a terminal state
            log.exception("run.unhandled", run_id=str(run_id))
            status = RunStatus.FAILED
            error = {"code": "internal", "message": str(exc)}
        finally:
            if client is not None and self._http_client is None:
                await client.aclose()

        if status in (RunStatus.AWAITING_APPROVAL, RunStatus.AWAITING_HUMAN_TASK):
            # Nothing is skipped and nothing is finished: the run is paused.
            # `park()` already committed the status inside the row lock that
            # orders it against a concurrent decision.
            await self.store.rollup(run, ledger)
            # The lock goes back, because a run can sit on a gate for days and
            # a project that cannot be launched for a week is worse than the
            # collision the lock exists to prevent. Resuming re-acquires it the
            # same way `retry-failed` does.
            await self.lock.release(run.project_id, run.id)
            log.info(
                "run.paused",
                run_id=str(run_id),
                awaiting=sorted(awaiting),
                cost_usd=str(run.cost_usd),
            )
            return ExecutionResult(
                run_id,
                status,
                run.cost_usd,
                self._executed,
                None,
                tuple(sorted(awaiting)),
            )

        if status is not RunStatus.SUCCEEDED:
            await self._skip_unreached(run, reason=str(error and error.get("code") or "aborted"))

        await self.store.rollup(run, ledger)
        await self.store.finish_run(run, status=status, error=error)
        # A gate on a run that will never resume is not a question anybody can
        # still answer. Closing it keeps the approver's inbox honest (PRD §16).
        expired = await approvals.expire_pending(self.db, run.id)
        await events.publish(
            EventType.RUN_COMPLETED,
            run_id=str(run_id),
            status=status,
            cost_usd=str(run.cost_usd),
            error=error,
        )
        await self.lock.release(run.project_id, run.id)
        await self.cancel.clear(run.id)
        log.info(
            "run.finished",
            run_id=str(run_id),
            status=status,
            cost_usd=str(run.cost_usd),
            nodes=self._executed,
            approvals_expired=expired,
        )
        return ExecutionResult(run_id, status, run.cost_usd, self._executed, error)

    # -- waves -------------------------------------------------------------

    async def _run_waves(
        self,
        *,
        run: Run,
        project: Project,
        gateway: LLMGateway,
        router: ModelRouter,
        ledger: RunLedger,
        events: RunEventStream,
    ) -> set[str]:
        """Execute what can run. Returns the gate nodes this pass left waiting."""
        selection = _selected_nodes(run)
        selected = self.dag.closure(selection) if selection else set(self.dag.node_ids)
        waves = self.dag.waves(selection)

        latest = await self.store.latest_by_node(run.id)
        outputs: dict[str, dict[str, Any]] = {
            node_id: node_run.output or {}
            for node_id, node_run in latest.items()
            if node_run.status is NodeRunStatus.SUCCEEDED
        }
        blocked: set[str] = set()
        # A gate from an earlier pass is still open: do not re-execute it (that
        # would spend a second model call and ask the same question twice) and
        # do not let its branch run.
        halted: set[str] = {
            node_id
            for node_id, node_run in latest.items()
            if node_run.status is NodeRunStatus.AWAITING_APPROVAL
        }
        waiting: set[str] = set()
        for node_id in halted:
            waiting.update(self.dag.descendants(node_id) & selected)
        scratch: dict[str, Any] = {}

        for wave in waves:
            await self._check_cancelled(run.id)
            pending = [
                node_id
                for node_id in wave
                if node_id not in outputs
                and node_id not in blocked
                and node_id not in halted
                and node_id not in waiting
            ]
            if not pending:
                continue

            semaphore = asyncio.Semaphore(WAVE_CONCURRENCY)
            results = await asyncio.gather(
                *(
                    self._run_guarded(
                        semaphore,
                        node_id,
                        run=run,
                        project=project,
                        gateway=gateway,
                        router=router,
                        ledger=ledger,
                        events=events,
                        outputs=outputs,
                        scratch=scratch,
                    )
                    for node_id in pending
                )
            )

            for outcome in results:
                if outcome.halted:
                    # Its branch waits, it is not skipped, and every other
                    # branch in this wave carries on (PRD §7.2 item 5).
                    halted.add(outcome.node_id)
                    waiting.update(self.dag.descendants(outcome.node_id) & selected)
                elif outcome.output is None:
                    blocked.add(outcome.node_id)
                    downstream = sorted(
                        (self.dag.descendants(outcome.node_id) & selected) - set(outputs)
                    )
                    blocked.update(downstream)
                    await self.store.record_skipped(
                        run_id=run.id,
                        node_ids=downstream,
                        reason=f"upstream node {outcome.node_id} failed",
                    )
                else:
                    outputs[outcome.node_id] = outcome.output

            await self.store.rollup(run, ledger)
            await self.lock.refresh(run.project_id, run.id)
            ledger.enforce()
            await self._check_cancelled(run.id)

        if blocked:
            first = sorted(blocked)[0]
            raise NodeFailed(first, {"code": "node_failed", "message": f"node {first} failed"})
        return halted

    async def _run_guarded(
        self,
        semaphore: asyncio.Semaphore,
        node_id: str,
        *,
        run: Run,
        project: Project,
        gateway: LLMGateway,
        router: ModelRouter,
        ledger: RunLedger,
        events: RunEventStream,
        outputs: dict[str, dict[str, Any]],
        scratch: dict[str, Any],
    ) -> NodeOutcome:
        """One node inside a wave. A failure is a `None` output, not an exception.

        Letting it raise would cancel the wave's other tasks mid-call — work
        already paid for, thrown away, and no checkpoint written for it. A gate
        halt is caught for the same reason and for one more: the other branches
        of this wave are exactly what should keep running while a human thinks.
        """
        async with semaphore, self._sessions() as session:
            scope = NodeScope(
                db=session,
                store=RunStore(session),
                run=await _reload(session, Run, run.id),
                project=await _reload(session, Project, project.id),
            )
            try:
                output = await self._run_node(
                    node_id=node_id,
                    scope=scope,
                    gateway=gateway,
                    router=router,
                    ledger=ledger,
                    events=events,
                    outputs=outputs,
                    scratch=scratch,
                )
            except NodeHalted:
                return NodeOutcome(node_id, halted=True)
            except NodeFailed:
                return NodeOutcome(node_id, output=None)
            return NodeOutcome(node_id, output=output)

    # -- one node ----------------------------------------------------------

    async def _run_node(
        self,
        *,
        node_id: str,
        scope: NodeScope,
        gateway: LLMGateway,
        router: ModelRouter,
        ledger: RunLedger,
        events: RunEventStream,
        outputs: dict[str, dict[str, Any]],
        scratch: dict[str, Any],
    ) -> dict[str, Any]:
        node = self.registry.node(node_id)
        run, project = scope.run, scope.project
        attempt = await self._next_attempt(scope, node_id)
        last_error: dict[str, Any] = {"code": "unknown", "message": "node did not run"}

        for offset in range(self._max_attempts):
            attempt_no = attempt + offset
            node_run = await scope.store.start_node(
                run_id=run.id, node_id=node_id, attempt=attempt_no
            )
            await events.publish(
                EventType.NODE_STARTED,
                node_id=node_id,
                attempt=attempt_no,
                name=node.spec.name,
                stage=node.spec.stage,
            )

            ctx = RunContext(
                run=run,
                project=project,
                db=scope.db,
                llm=gateway,
                router=router,
                ledger=ledger,
                outputs=outputs,
                node_id=node_id,
                scratch=scratch,
                plan=self._plan_context(node.spec, scope),
                creative=self._creative,
                _progress=_progress_sink(events),
            )

            try:
                output = await self._attempt_node(
                    node=node,
                    ctx=ctx,
                    scope=scope,
                    router=router,
                    node_run=node_run,
                    events=events,
                )
            except NodeHalted:
                # The gate is open and its `NodeRun` is already checkpointed as
                # `awaiting_approval`. Retrying would ask the same question
                # twice, so this unwinds straight past the attempt ladder.
                raise
            except (MissingCredential, LLMAuthError) as exc:
                # Every model on every node would fail the same way. Do not burn
                # two more attempts proving it.
                last_error = {"code": "credential", "message": str(exc)}
                await self._fail_node(
                    node_run, ctx, last_error, events, store=scope.store, will_retry=False
                )
                raise NodeFailed(node_id, last_error) from exc
            except Exception as exc:  # noqa: BLE001 — classified and recorded below
                last_error = {
                    "code": type(exc).__name__,
                    "message": str(exc)[:1000],
                    "attempt": attempt_no,
                }
                will_retry = offset < self._max_attempts - 1
                await self._fail_node(
                    node_run, ctx, last_error, events, store=scope.store, will_retry=will_retry
                )
                if not will_retry:
                    break
                await asyncio.sleep(self._backoff(offset + 1))
                continue

            self._executed += 1
            return output

        raise NodeFailed(node_id, last_error)

    async def _attempt_node(
        self,
        *,
        node: Node,
        ctx: RunContext,
        scope: NodeScope,
        router: ModelRouter,
        node_run: NodeRun,
        events: RunEventStream,
    ) -> dict[str, Any]:
        """One attempt: gather → (cache?) → reason → validate → checkpoint."""
        spec = node.spec
        run, project = scope.run, scope.project
        if run.stage is RunStage.PLAN:
            # Before `gather()`, so before any HTTP and before a token is
            # spent: Stage 02 law 12 and PRD §17 PS1 say a plan run reaches
            # no source that could write to an ad account. A `MutationForbidden`
            # here fails the node rather than degrading it, because the wrong
            # answer to "may this run mutate?" is not something to carry on past.
            for connector in spec.connectors:
                assert_read_only(connector, why=f"node {spec.id}")
        evidence: list[Evidence] = await node.gather(ctx)
        input_hash = _input_hash(
            spec_id=spec.id,
            version=spec.version,
            project=project,
            outputs=ctx.outputs,
            depends_on=spec.depends_on,
            model=router.chain(spec.task_class)[0],
            # PRD §8.3: a plan run's cache key includes the constants it was
            # parameterised with. Without it, changing a learning threshold
            # would leave every deterministic node serving an output computed
            # under the old one, and `calc_version` on the reused `PlanCalc`
            # row would say so while the node output did not.
            # Stage 04 PRD §8.3: the same for a creative run, whose constants
            # are `creative_constants.yaml` with the project's overrides.
            constants_version=(
                ctx.plan.constants.version
                if ctx.plan
                else ctx.creative.constants.version
                if ctx.creative
                else None
            ),
        )

        # A gate is never served from cache. The cached value is the *proposal*,
        # not the decision, and reusing it would skip the human the gate exists
        # for — the one failure mode here that is silent rather than loud.
        if _reuse_cache(run) and not spec.gate:
            cached = await scope.store.cached_output(
                project_id=project.id, node_id=spec.id, input_hash=input_hash
            )
            if cached is not None and cached.output is not None:
                await scope.store.finish_node(
                    node_run,
                    status=NodeRunStatus.SUCCEEDED,
                    output=cached.output,
                    evidence_ids=list(cached.evidence_ids),
                    model=cached.model,
                    prompt=cached.prompt,
                    input_hash=input_hash,
                    latency_ms=0,
                )
                await events.publish(
                    EventType.NODE_COMPLETED,
                    node_id=spec.id,
                    status=NodeRunStatus.SUCCEEDED,
                    cached=True,
                    cost_usd="0",
                    latency_ms=0,
                )
                return dict(cached.output)

        result: BaseModel = await node.reason(ctx, evidence)
        payload = result.model_dump(mode="json")

        if ctx.plan is not None:
            # A calculation made while *reasoning* is still a calculation this
            # node produced, and it must be citable.
            #
            # Some plan nodes cannot do all their arithmetic in `gather()`,
            # because what to compute depends on what the model chose: node
            # 2.2.2 asks which campaign each cluster of demand belongs to and
            # only then can check those campaigns against the learning
            # thresholds. Requiring every `derived` row to come back from
            # `gather()` would force the model call into `gather()` to satisfy
            # a check rather than to serve the node.
            #
            # Nothing is weakened by folding them in here. `PlanCalcRunner` is
            # the **only** door from a plan node to `agent/calc/` — enforced at
            # build time by `check_plan_calc_imports` and at run time by
            # `NodeSpec.calc` — so `calc.evidence` is exactly the set of
            # `derived` rows this node computed and persisted, which is the
            # property the citation check is about. It is if anything tighter
            # than what it replaces: `gather.collect` can read *stored*
            # `derived` rows another node wrote, and those were citable before.
            known = {item.id for item in evidence}
            evidence = [
                *evidence,
                *(row for row in ctx.plan.calc.evidence if row.id not in known),
            ]

        gathered = {item.id for item in evidence}
        cited = collect_evidence_ids(payload)
        unknown = cited - gathered
        if unknown:
            raise NodeContractError(
                f"node {spec.id} cited evidence it did not gather: "
                + ", ".join(sorted(str(item) for item in unknown))
            )

        # PRD §9.1 item 4 — the Stage 02 half of the same check. `evidence_ids`
        # is checked against everything gathered; `calc_evidence_ids` only
        # against the `derived` rows, so a node cannot present a connector's
        # observation as the calculation behind a figure.
        computed = collect_calc_evidence_ids(payload)
        if computed:
            produced = derived_ids(evidence)
            uncomputed = computed - produced
            if uncomputed:
                raise NodeContractError(
                    f"node {spec.id} cited {len(uncomputed)} calc evidence id(s) that are not "
                    f"`derived` rows it produced: "
                    + ", ".join(sorted(str(item) for item in uncomputed))
                    + ". Every number comes from an @formula in agent/calc/ (law 14); call it "
                    "through ctx.plan.calc.run() and return the row from gather()."
                )

        if run.stage is RunStage.CREATIVE:
            # Stage 04 PRD §8.1 item 2: `media` and `lint_required` are
            # asserted, not trusted — before a gate opens on the output and
            # before the node is checkpointed as succeeded.
            await creative_run.assert_node_contract(scope.db, run, spec)

        telemetry = ctx.telemetry
        if task_wanted(node, ctx, result):
            await self._open_human_task(
                node_run=node_run,
                spec=spec,
                scope=scope,
                payload=payload,
                gathered=sorted(gathered),
                input_hash=input_hash,
                telemetry=telemetry,
                events=events,
            )
        if gate_wanted(node, ctx, result):
            await self._open_gate(
                node_run=node_run,
                spec=spec,
                scope=scope,
                payload=payload,
                gathered=sorted(gathered),
                input_hash=input_hash,
                telemetry=telemetry,
                events=events,
            )

        await scope.store.finish_node(
            node_run,
            status=NodeRunStatus.SUCCEEDED,
            output=payload,
            evidence_ids=sorted(gathered),
            model=telemetry.model,
            prompt=telemetry.prompt,
            input_hash=input_hash,
            token_in=telemetry.token_in,
            token_out=telemetry.token_out,
            cost_usd=telemetry.cost_usd,
            # `or None` would be wrong here: a sub-millisecond call really did
            # take 0ms, and reporting that as "unknown" loses the measurement.
            latency_ms=(
                sum(item.latency_ms for item in telemetry.completions)
                if telemetry.completions
                else None
            ),
        )
        await events.publish(
            EventType.NODE_TOKENS,
            node_id=spec.id,
            token_in=telemetry.token_in,
            token_out=telemetry.token_out,
            cost_usd=str(node_run.cost_usd),
        )
        await events.publish(
            EventType.NODE_COMPLETED,
            node_id=spec.id,
            status=NodeRunStatus.SUCCEEDED,
            model=telemetry.model,
            cost_usd=str(node_run.cost_usd),
            latency_ms=node_run.latency_ms,
            repairs=telemetry.repairs,
        )
        return payload

    async def _open_human_task(
        self,
        *,
        node_run: NodeRun,
        spec: NodeSpec,
        scope: NodeScope,
        payload: dict[str, Any],
        gathered: list[uuid.UUID],
        input_hash: str,
        telemetry: NodeTelemetry,
        events: RunEventStream,
    ) -> None:
        """Checkpoint a person-task's brief, create the task, and stop this branch.

        The twin of `_open_gate`, and separate for the reason PRD §8.4 gives: an
        approval routes to a role and a person-task routes to one identity. The
        `NodeRun` and the `HumanTask` commit together, so there is no window in
        which a brief exists with nobody asked, or a task exists with no brief
        behind it.

        Only this branch stops. Everything in 3.1, 3.3 and 3.4 keeps running, so
        a slow signer never blocks spec-sheet production.
        """
        assignee = payload.get("assignee_id")
        if not assignee:
            raise NodeContractError(
                f"person-task node {spec.id} produced no assignee_id. A task with no "
                "named person is a task that falls back to a role, which is exactly "
                "what a non-delegable act must never do."
            )

        node_run.status = NodeRunStatus.AWAITING_HUMAN_TASK
        node_run.output = payload
        node_run.evidence_ids = gathered
        node_run.model = telemetry.model
        node_run.prompt = telemetry.prompt
        node_run.input_hash = input_hash
        node_run.token_in = telemetry.token_in
        node_run.token_out = telemetry.token_out
        node_run.cost_usd = telemetry.cost_usd
        node_run.finished_at = utcnow()

        task = await human_tasks.open_task(
            scope.db,
            run=scope.run,
            project_id=scope.project.id,
            node_id=spec.id,
            task_key=spec.human_task_key or "",
            assignee_id=uuid.UUID(str(assignee)),
            title=str(payload.get("title") or spec.name),
            instructions=str(payload.get("instructions") or ""),
            required_artifacts=dict(payload.get("required_artifacts") or {}),
            blocking_for=HumanTaskBlocking(
                str(payload.get("blocking_for") or HumanTaskBlocking.PUBLISH.value)
            ),
        )
        write_audit(
            scope.db,
            workspace_id=scope.run.workspace_id,
            action=AuditAction.HUMAN_TASK_OPENED,
            target_type=AuditTarget.HUMAN_TASK,
            target_id=task.id,
            meta={
                "run_id": str(scope.run.id),
                "node_id": spec.id,
                "task_key": task.task_key,
                "assignee_id": str(task.assignee_id),
                "blocking_for": task.blocking_for.value,
            },
        )
        await scope.db.commit()

        await events.publish(
            EventType.HUMAN_TASK_REQUIRED,
            node_id=spec.id,
            task_id=str(task.id),
            task_key=task.task_key,
            assignee_id=str(task.assignee_id),
            blocking_for=task.blocking_for.value,
            name=spec.name,
            stage=spec.stage,
        )
        self._executed += 1
        raise NodeHalted(spec.id, task.id)

    async def _open_gate(
        self,
        *,
        node_run: NodeRun,
        spec: NodeSpec,
        scope: NodeScope,
        payload: dict[str, Any],
        gathered: list[uuid.UUID],
        input_hash: str,
        telemetry: NodeTelemetry,
        events: RunEventStream,
    ) -> None:
        """Checkpoint a gate's proposal and stop, raising `NodeHalted`.

        The order matters: the `NodeRun` and the `Approval` commit together, so
        there is no window in which a proposal exists with nobody asked about it,
        or a question exists with no proposal behind it.
        """
        if spec.required_role is None:  # pragma: no cover — NodeSpec validates this
            raise NodeContractError(f"gate node {spec.id} has no required_role")
        run, project = scope.run, scope.project

        node_run.status = NodeRunStatus.AWAITING_APPROVAL
        node_run.output = payload
        node_run.evidence_ids = gathered
        node_run.model = telemetry.model
        node_run.prompt = telemetry.prompt
        node_run.input_hash = input_hash
        node_run.token_in = telemetry.token_in
        node_run.token_out = telemetry.token_out
        node_run.cost_usd = telemetry.cost_usd
        node_run.latency_ms = (
            sum(item.latency_ms for item in telemetry.completions)
            if telemetry.completions
            else None
        )
        node_run.finished_at = utcnow()

        approval = await approvals.open_gate(
            scope.db,
            run=run,
            project=project,
            node_id=spec.id,
            required_role=spec.required_role,
            proposal=payload,
            gate_key=spec.gate_key,
        )
        write_audit(
            scope.db,
            workspace_id=run.workspace_id,
            action=AuditAction.APPROVAL_REQUESTED,
            target_type=AuditTarget.APPROVAL,
            target_id=approval.id,
            meta={
                "run_id": str(run.id),
                "node_id": spec.id,
                "gate_key": approval.gate_key,
                "required_role": approval.required_role.value,
                "assignee_id": str(approval.assignee_id) if approval.assignee_id else None,
            },
        )
        await scope.db.commit()

        await events.publish(
            EventType.APPROVAL_REQUIRED,
            node_id=spec.id,
            approval_id=str(approval.id),
            required_role=approval.required_role.value,
            assignee_id=str(approval.assignee_id) if approval.assignee_id else None,
            name=spec.name,
            stage=spec.stage,
        )
        await self._notify_gate(scope, approval, spec)
        self._executed += 1
        raise NodeHalted(spec.id, approval.id)

    async def _notify_gate(self, scope: NodeScope, approval: Approval, spec: NodeSpec) -> None:
        """Tell whoever can decide. Never raises — a mail server cannot block a gate."""
        try:
            recipients = await approvals.notify_targets(scope.db, approval, scope.run.workspace_id)
            if not recipients:
                log.warning(
                    "approval.no_recipient",
                    approval_id=str(approval.id),
                    required_role=approval.required_role.value,
                )
                return
            link = f"{self.settings.app_base_url.rstrip('/')}/approvals/{approval.id}"
            for user in recipients:
                await send_approval_request(
                    self.settings,
                    to=user.email,
                    link=link,
                    project_name=scope.project.name,
                    node_name=spec.name,
                )
        except Exception as exc:  # noqa: BLE001 — notification is never load-bearing
            log.warning("approval.notify_failed", approval_id=str(approval.id), error=str(exc))

    async def _fail_node(
        self,
        node_run: NodeRun,
        ctx: RunContext,
        error: dict[str, Any],
        events: RunEventStream,
        *,
        store: RunStore,
        will_retry: bool,
    ) -> None:
        telemetry = ctx.telemetry
        await store.finish_node(
            node_run,
            status=NodeRunStatus.FAILED,
            model=telemetry.model,
            prompt=telemetry.prompt,
            token_in=telemetry.token_in,
            token_out=telemetry.token_out,
            cost_usd=telemetry.cost_usd,
            error=error,
        )
        log.warning(
            "node.failed",
            node_id=node_run.node_id,
            attempt=node_run.attempt,
            will_retry=will_retry,
            code=error.get("code"),
        )
        await events.publish(
            EventType.NODE_FAILED,
            node_id=node_run.node_id,
            attempt=node_run.attempt,
            will_retry=will_retry,
            error=error,
        )

    # -- helpers -----------------------------------------------------------

    async def _next_attempt(self, scope: NodeScope, node_id: str) -> int:
        latest = await scope.store.latest_by_node(scope.run.id)
        existing = latest.get(node_id)
        return 1 if existing is None else existing.attempt + 1

    def _backoff(self, attempt: int) -> float:
        if self._backoff_base <= 0:
            return 0.0
        return (2.0**attempt) * self._backoff_base * (0.5 + random.random() / 2)  # noqa: S311

    async def _check_cancelled(self, run_id: uuid.UUID) -> None:
        if await self.cancel.is_set(run_id):
            raise RunCancelled

    async def _budget_cap(self, project: Project, *, stage: RunStage) -> Decimal:
        """The ceiling for this run. See `orchestrator.budget` for the rule.

        The rule lives there and not here because the run console draws a meter
        against the same number, and two implementations of "what may this run
        spend" is how a plan run came to be killed at $8 behind a progress bar
        that read half of $15.
        """
        workspace = await self.db.get(Workspace, project.workspace_id)
        return resolve_cost_cap(
            stage=stage,
            project_settings=project.settings,
            workspace_settings=workspace.settings if workspace else None,
            defaults=self.settings,
            project_id=str(project.id),
        )

    async def _build_gateway(
        self, run: Run, project: Project
    ) -> tuple[LLMGateway, httpx.AsyncClient | None]:
        if self._gateway is not None:
            return self._gateway, self._http_client
        values = await resolve_values(
            self.db, workspace_id=run.workspace_id, kind=CredentialKind.OPENROUTER
        )
        gateway, client = build_gateway(
            api_key=values["api_key"], settings=self.settings, client=self._http_client
        )
        return gateway, client

    async def _plan_resources(self, run: Run, project: Project) -> PlanResources:
        """The `PlanInput` and constants this plan run executes against.

        Both are resolved once, before the first wave. `PlanInput` because law
        13 says a node never re-reads the `Report`; the constants because
        every `PlanCalc` row in one run must claim the same `calc_version`,
        and a per-node merge is how two nodes in the same plan end up
        disagreeing about which thresholds they used.
        """
        plan_input = await build_plan_input_for_run(self.db, run, workspace_id=project.workspace_id)
        constants = constants_for(project)
        log.info(
            "plan.resources",
            run_id=str(run.id),
            research_run_id=str(plan_input.research_run_id),
            constants_version=constants.version,
            degraded_sources=plan_input.degraded_sources,
        )
        return PlanResources(input=plan_input, constants=constants)

    def _plan_context(self, spec: NodeSpec, scope: NodeScope) -> PlanContext | None:
        """`ctx.plan` for one node, or None on a research run."""
        if self._plan is None:
            return None
        return self._plan.context_for(
            spec,
            session=scope.db,
            store=EvidenceStore(scope.db, scope.run.workspace_id),
            project_id=scope.project.id,
            plan_run_id=scope.run.id,
        )

    async def _build_router(self, run: Run, project: Project) -> ModelRouter:
        workspace = await self.db.get(Workspace, run.workspace_id)
        return ModelRouter.resolve(
            workspace_settings=workspace.settings if workspace else None,
            project_settings=project.settings,
        )

    async def _skip_unreached(self, run: Run, *, reason: str) -> None:
        """Record every selected node that will now never run."""
        selection = _selected_nodes(run)
        selected = self.dag.closure(selection) if selection else set(self.dag.node_ids)
        seen = await self.store.latest_by_node(run.id)
        pending = sorted(
            node_id
            for node_id in selected
            if node_id not in seen
            or seen[node_id].status in (NodeRunStatus.QUEUED, NodeRunStatus.RUNNING)
        )
        await self.store.record_skipped(run_id=run.id, node_ids=pending, reason=reason)

    async def _abort(
        self,
        run: Run,
        events: RunEventStream,
        ledger: RunLedger,
        status: RunStatus,
        error: dict[str, Any],
    ) -> ExecutionResult:
        """End a run that could not start — no node ran, nothing to check point."""
        await self._skip_unreached(run, reason=str(error.get("code")))
        await self.store.rollup(run, ledger)
        await self.store.finish_run(run, status=status, error=error)
        await events.publish(
            EventType.RUN_COMPLETED, run_id=str(run.id), status=status, error=error
        )
        await self.lock.release(run.project_id, run.id)
        log.warning("run.aborted", run_id=str(run.id), code=error.get("code"))
        return ExecutionResult(run.id, status, Decimal(run.cost_usd), 0, error)


async def _reload[T: Base](session: AsyncSession, model: type[T], entity_id: uuid.UUID) -> T:
    """Read a row into a node's own session. The caller has already proved it exists."""
    row = await session.get(model, entity_id)
    if row is None:  # pragma: no cover — the executor loaded both before the wave started
        raise NodeContractError(f"{model.__name__} {entity_id} disappeared mid-run")
    return row


def _progress_sink(
    events: RunEventStream,
) -> Callable[[str, str], Awaitable[None]]:
    """Adapt the event stream to `RunContext._progress`, discarding the entry id."""

    async def emit(node_id: str, message: str) -> None:
        await events.publish(EventType.NODE_PROGRESS, node_id=node_id, message=message)

    return emit


def _selected_nodes(run: Run) -> list[str] | None:
    """The `node_filter` of a partial run, or None for the whole DAG."""
    node_filter = run.node_filter or {}
    node_ids = node_filter.get("node_ids")
    if not node_ids:
        return None
    return [str(node_id) for node_id in node_ids]


def _reuse_cache(run: Run) -> bool:
    return bool((run.node_filter or {}).get("reuse_cache"))


def _input_hash(
    *,
    spec_id: str,
    version: int,
    project: Project,
    outputs: dict[str, dict[str, Any]],
    depends_on: tuple[str, ...],
    model: str,
    constants_version: str | None = None,
) -> str:
    """The cache key of PRD §7.1: a node is pure w.r.t. its inputs and its version.

    The model id is part of it on purpose — the same inputs answered by a
    different model are a different output, and reusing across that boundary
    would silently attribute one model's work to another. `constants_version`
    is the Stage 02 addition (PRD §8.3) and is None on a research run, so
    research cache keys are byte-identical to what they were before Stage 02
    existed and no completed research node is invalidated by this change.
    """
    material: dict[str, Any] = {
        "node": spec_id,
        "version": version,
        "model": model,
        "project": {
            "domain": project.domain,
            "product_context": project.product_context,
            "markets": project.markets,
        },
        "inputs": {dep: outputs.get(dep) for dep in depends_on},
    }
    if constants_version is not None:
        material["constants"] = constants_version
    encoded = json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
