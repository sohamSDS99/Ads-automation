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
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import Settings, get_settings
from agent.credentials import MissingCredential, resolve_secret
from agent.db.models import (
    CredentialKind,
    Evidence,
    NodeRun,
    NodeRunStatus,
    Project,
    Run,
    RunStatus,
    Workspace,
)
from agent.llm.gateway import LLMAuthError, LLMGateway, build_gateway
from agent.llm.ledger import BudgetExceeded, RunLedger
from agent.llm.router import ModelRouter
from agent.nodes.base import Node, RunContext, collect_evidence_ids
from agent.orchestrator.dag import Dag, get_dag
from agent.orchestrator.events import EventType, RunEventStream
from agent.orchestrator.registry import NodeRegistry, get_registry
from agent.orchestrator.state import TERMINAL_STATUSES, CancelFlag, RunLock, RunStore

log = structlog.get_logger(__name__)

#: Independent nodes inside one wave (PRD §7.2 item 2).
WAVE_CONCURRENCY = 4

#: Attempts per node, including the first (PRD §7.2 item 3).
MAX_NODE_ATTEMPTS = 3

#: `2^n * 1.5s`, jittered.
BACKOFF_BASE_SECONDS = 1.5


class RunCancelled(RuntimeError):
    """The cancel flag was set (PRD §7.2 item 6)."""


class NodeContractError(RuntimeError):
    """Output cites evidence the node did not gather (PRD §18 law 1)."""


class NodeFailed(RuntimeError):
    """A node exhausted its attempts."""

    def __init__(self, node_id: str, error: dict[str, Any]) -> None:
        super().__init__(f"node {node_id} failed: {error.get('message')}")
        self.node_id = node_id
        self.error = error


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    """What the worker reports back."""

    run_id: uuid.UUID
    status: RunStatus
    cost_usd: Decimal
    nodes_executed: int
    error: dict[str, Any] | None = None


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
    ) -> None:
        self.db = db
        self.redis = redis
        self.settings = settings or get_settings()
        self.registry = registry or get_registry()
        self.dag = dag or get_dag()
        self.store = RunStore(db)
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
        try:
            await self._run_waves(
                run=run,
                project=project,
                gateway=gateway,
                router=router,
                ledger=ledger,
                events=events,
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

        if status is not RunStatus.SUCCEEDED:
            await self._skip_unreached(run, reason=str(error and error.get("code") or "aborted"))

        await self.store.rollup(run, ledger)
        await self.store.finish_run(run, status=status, error=error)
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
    ) -> None:
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

        for wave in waves:
            await self._check_cancelled(run.id)
            pending = [
                node_id for node_id in wave if node_id not in outputs and node_id not in blocked
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
                    )
                    for node_id in pending
                )
            )

            for node_id, output in results:
                if output is None:
                    blocked.add(node_id)
                    downstream = sorted((self.dag.descendants(node_id) & selected) - set(outputs))
                    blocked.update(downstream)
                    await self.store.record_skipped(
                        run_id=run.id,
                        node_ids=downstream,
                        reason=f"upstream node {node_id} failed",
                    )
                else:
                    outputs[node_id] = output

            await self.store.rollup(run, ledger)
            await self.lock.refresh(run.project_id, run.id)
            ledger.enforce()
            await self._check_cancelled(run.id)

        if blocked:
            first = sorted(blocked)[0]
            raise NodeFailed(first, {"code": "node_failed", "message": f"node {first} failed"})

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
    ) -> tuple[str, dict[str, Any] | None]:
        """One node inside a wave. A failure is a `None` output, not an exception.

        Letting it raise would cancel the wave's other tasks mid-call — work
        already paid for, thrown away, and no checkpoint written for it.
        """
        async with semaphore:
            try:
                output = await self._run_node(
                    node_id=node_id,
                    run=run,
                    project=project,
                    gateway=gateway,
                    router=router,
                    ledger=ledger,
                    events=events,
                    outputs=outputs,
                )
            except NodeFailed:
                return node_id, None
            return node_id, output

    # -- one node ----------------------------------------------------------

    async def _run_node(
        self,
        *,
        node_id: str,
        run: Run,
        project: Project,
        gateway: LLMGateway,
        router: ModelRouter,
        ledger: RunLedger,
        events: RunEventStream,
        outputs: dict[str, dict[str, Any]],
    ) -> dict[str, Any]:
        node = self.registry.node(node_id)
        attempt = await self._next_attempt(run.id, node_id)
        last_error: dict[str, Any] = {"code": "unknown", "message": "node did not run"}

        for offset in range(self._max_attempts):
            attempt_no = attempt + offset
            node_run = await self.store.start_node(
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
                db=self.db,
                llm=gateway,
                router=router,
                ledger=ledger,
                outputs=outputs,
                node_id=node_id,
                _progress=_progress_sink(events),
            )

            try:
                output = await self._attempt_node(
                    node=node,
                    ctx=ctx,
                    run=run,
                    project=project,
                    router=router,
                    node_run=node_run,
                    events=events,
                )
            except (MissingCredential, LLMAuthError) as exc:
                # Every model on every node would fail the same way. Do not burn
                # two more attempts proving it.
                last_error = {"code": "credential", "message": str(exc)}
                await self._fail_node(node_run, ctx, last_error, events, will_retry=False)
                raise NodeFailed(node_id, last_error) from exc
            except Exception as exc:  # noqa: BLE001 — classified and recorded below
                last_error = {
                    "code": type(exc).__name__,
                    "message": str(exc)[:1000],
                    "attempt": attempt_no,
                }
                will_retry = offset < self._max_attempts - 1
                await self._fail_node(node_run, ctx, last_error, events, will_retry=will_retry)
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
        run: Run,
        project: Project,
        router: ModelRouter,
        node_run: NodeRun,
        events: RunEventStream,
    ) -> dict[str, Any]:
        """One attempt: gather → (cache?) → reason → validate → checkpoint."""
        spec = node.spec
        evidence: list[Evidence] = await node.gather(ctx)
        input_hash = _input_hash(
            spec_id=spec.id,
            version=spec.version,
            project=project,
            outputs=ctx.outputs,
            depends_on=spec.depends_on,
            model=router.chain(spec.task_class)[0],
        )

        if _reuse_cache(run):
            cached = await self.store.cached_output(
                project_id=project.id, node_id=spec.id, input_hash=input_hash
            )
            if cached is not None and cached.output is not None:
                await self.store.finish_node(
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
        await self.store.finish_node(
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

    async def _fail_node(
        self,
        node_run: NodeRun,
        ctx: RunContext,
        error: dict[str, Any],
        events: RunEventStream,
        *,
        will_retry: bool,
    ) -> None:
        telemetry = ctx.telemetry
        await self.store.finish_node(
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

    async def _next_attempt(self, run_id: uuid.UUID, node_id: str) -> int:
        latest = await self.store.latest_by_node(run_id)
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
        """Project setting wins; the environment default is the floor under it."""
        raw = (project.settings or {}).get("max_run_cost_usd")
        if raw is None:
            return Decimal(self.settings.max_run_cost_usd)
        try:
            return Decimal(str(raw))
        except (InvalidOperation, ValueError):
            log.warning("run.bad_budget_setting", project_id=str(project.id), value=raw)
            return Decimal(self.settings.max_run_cost_usd)

    async def _build_gateway(
        self, run: Run, project: Project
    ) -> tuple[LLMGateway, httpx.AsyncClient | None]:
        if self._gateway is not None:
            return self._gateway, self._http_client
        api_key = await resolve_secret(
            self.db,
            workspace_id=run.workspace_id,
            kind=CredentialKind.OPENROUTER,
            project_id=project.id,
            user_id=run.triggered_by,
        )
        gateway, client = build_gateway(
            api_key=api_key, settings=self.settings, client=self._http_client
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
