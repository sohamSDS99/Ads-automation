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
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx
import structlog
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.config import Settings, get_settings
from agent.credentials import MissingCredential, resolve_values
from agent.db.models import (
    Approval,
    Base,
    CredentialKind,
    Evidence,
    NodeRun,
    NodeRunStatus,
    Project,
    Run,
    RunStage,
    RunStatus,
    Workspace,
)
from agent.db.session import get_sessionmaker
from agent.llm.gateway import LLMAuthError, LLMGateway, build_gateway
from agent.llm.ledger import BudgetExceeded, RunLedger
from agent.llm.router import ModelRouter
from agent.nodes.base import Node, NodeSpec, NodeTelemetry, RunContext, collect_evidence_ids
from agent.notify.email import send_approval_request
from agent.orchestrator import approvals
from agent.orchestrator.dag import Dag, get_dag
from agent.orchestrator.events import EventType, RunEventStream
from agent.orchestrator.heartbeat import RunHeartbeat
from agent.orchestrator.registry import NodeRegistry, get_registry
from agent.orchestrator.state import TERMINAL_STATUSES, CancelFlag, RunLock, RunStore, utcnow

log = structlog.get_logger(__name__)

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


class NodeContractError(RuntimeError):
    """Output cites evidence the node did not gather (PRD §18 law 1)."""


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
        ledger = RunLedger(cap_usd=await self._budget_cap(project), spent_usd=Decimal(run.cost_usd))

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

        if status is RunStatus.AWAITING_APPROVAL:
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
        evidence: list[Evidence] = await node.gather(ctx)
        input_hash = _input_hash(
            spec_id=spec.id,
            version=spec.version,
            project=project,
            outputs=ctx.outputs,
            depends_on=spec.depends_on,
            model=router.chain(spec.task_class)[0],
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

        gathered = {item.id for item in evidence}
        cited = collect_evidence_ids(payload)
        unknown = cited - gathered
        if unknown:
            raise NodeContractError(
                f"node {spec.id} cited evidence it did not gather: "
                + ", ".join(sorted(str(item) for item in unknown))
            )

        telemetry = ctx.telemetry
        if spec.gate:
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

    async def _budget_cap(self, project: Project) -> Decimal:
        """Narrowest scope wins: project, then workspace, then the environment.

        The workspace layer is what `/settings` writes. Without it an admin can
        set a workspace-wide ceiling and watch every project ignore it.
        """
        workspace = await self.db.get(Workspace, project.workspace_id)
        for scope, settings in (
            ("project", project.settings),
            ("workspace", workspace.settings if workspace else None),
        ):
            raw = (settings or {}).get("max_run_cost_usd")
            if raw is None:
                continue
            try:
                return Decimal(str(raw))
            except (InvalidOperation, ValueError):
                log.warning(
                    "run.bad_budget_setting",
                    scope=scope,
                    project_id=str(project.id),
                    value=raw,
                )
        return Decimal(self.settings.max_run_cost_usd)

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
) -> str:
    """The cache key of PRD §7.1: a node is pure w.r.t. its inputs and its version.

    The model id is part of it on purpose — the same inputs answered by a
    different model are a different output, and reusing across that boundary
    would silently attribute one model's work to another.
    """
    material = {
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
    encoded = json.dumps(material, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
