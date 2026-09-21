"""The run API (PRD §14): launch, observe, cancel, retry, inspect.

Launching is the one place in the product where two people can collide, so it
is also the one place with a lock: PRD §15 NF5d requires that two operators
launching the same project produce exactly one run and a `409` naming the
holder. The lock is taken *before* the row is written, so the loser never sees a
run that is about to be thrown away.
"""

from __future__ import annotations

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from agent.api import problems
from agent.api.middleware import client_ip
from agent.api.schemas_diff import RunDiffResponse, to_response
from agent.api.schemas_runs import (
    DagEdge,
    DegradedSource,
    LaunchRunRequest,
    NodeRunDetail,
    NodeState,
    PresenceResponse,
    RunResponse,
    RunViewer,
)
from agent.api.sse import cursor_from, sse_response
from agent.api.throttle import throttle
from agent.audit import AuditAction, AuditTarget, write_audit
from agent.auth.deps import Principal, require
from agent.auth.ratelimit import RUN_QUOTA
from agent.auth.rbac import Permission
from agent.db.models import NodeRun, Run, RunMode, RunStage, RunStatus, RunTrigger
from agent.db.repos import ProjectRepo, ReportRepo, RunRepo, UserRepo
from agent.db.session import get_session
from agent.export.contract import ResearchReport
from agent.export.diff import diff_reports
from agent.nodes.gather import parse_note
from agent.orchestrator.approvals import expire_pending
from agent.orchestrator.dag import Dag, DagError, get_dag
from agent.orchestrator.events import EventType, RunEventStream
from agent.orchestrator.launch import LaunchRequest, ProjectBusy, QueueUnavailable, launch
from agent.orchestrator.presence import MAX_VIEWERS, RunPresence
from agent.orchestrator.registry import NodeRegistry, get_registry
from agent.orchestrator.state import (
    TERMINAL_STATUSES,
    CancelFlag,
    LockHolder,
    RunLock,
    RunStore,
)
from agent.queue import enqueue_run
from agent.redis_client import get_redis

log = structlog.get_logger(__name__)

router = APIRouter(tags=["runs"])

Db = Annotated[AsyncSession, Depends(get_session)]
AnyMember = Annotated[Principal, Depends(require(Permission.READ))]
RunOperator = Annotated[Principal, Depends(require(Permission.RUN_EXECUTE))]


# ---------------------------------------------------------------------------
# launch
# ---------------------------------------------------------------------------


@router.post(
    "/projects/{project_id}/runs",
    response_model=RunResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Launch a run",
    # Tighter than the workspace-wide write quota, because each of these can
    # cost dollars and hold a worker for the better part of an hour. The run
    # lock already allows one per project; this bounds a caller cycling across
    # projects. Declared alongside `require(RUN_EXECUTE)`, never instead of it.
    dependencies=[Depends(throttle(RUN_QUOTA))],
)
async def launch_run(
    project_id: uuid.UUID,
    body: LaunchRunRequest,
    me: RunOperator,
    request: Request,
    db: Db,
) -> RunResponse:
    project = await ProjectRepo(db, me.workspace_id).get(project_id)
    if project is None:
        raise problems.not_found(f"No project {project_id}.")

    # Research only: a plan run is started from `POST /projects/{id}/plan/runs`,
    # which has its own permission, its own lock and its own preconditions.
    dag = get_dag(RunStage.RESEARCH)
    selection = _resolve_selection(dag, body)

    try:
        run = await launch(
            db,
            get_redis(),
            LaunchRequest(
                project=project,
                workspace_id=me.workspace_id,
                trigger=RunTrigger.MANUAL,
                actor_id=me.user.id,
                actor_name=me.user.name,
                node_ids=selection,
                reuse_cache=body.reuse_cache,
                ip=client_ip(request),
            ),
        )
    except ProjectBusy as busy:
        raise problems.conflict(
            f"{busy.holder.user_name or 'Someone'} is already running this project.",
            title="Project is already running",
            holder=busy.holder.as_dict(),
        ) from busy
    except QueueUnavailable as unavailable:
        raise problems.Problem(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            title="Queue unavailable",
            detail="The run was recorded but could not be queued. Retry it once Redis is back.",
        ) from unavailable

    return await _run_response(db, run, dag=dag, registry=get_registry())


# ---------------------------------------------------------------------------
# read
# ---------------------------------------------------------------------------


@router.get("/runs/{run_id}", response_model=RunResponse, summary="Run state")
async def get_run(run_id: uuid.UUID, me: AnyMember, db: Db) -> RunResponse:
    run = await _load_run(db, run_id, me)
    return await _run_response(db, run, dag=get_dag(run.stage), registry=get_registry())


@router.get(
    "/runs/{run_id}/events",
    summary="Live run events (SSE)",
    response_class=StreamingResponse,
)
async def stream_events(run_id: uuid.UUID, me: AnyMember, request: Request, db: Db) -> Response:
    """The run console's feed. Replays from `Last-Event-ID`, heartbeats every 15s."""
    run = await _load_run(db, run_id, me)
    # A dependency-scoped session lives until the response *finishes*, and this
    # response can stay open for the length of a run. Handing the connection
    # back now keeps a room full of open consoles from draining the pool; the
    # stream itself reads Redis and never touches Postgres again.
    await db.close()
    stream = RunEventStream(get_redis(), run.id)
    return sse_response(
        stream,
        request,
        after=cursor_from(request),
        # A finished run has nothing more to say; drain the record and close
        # rather than holding a connection open forever.
        live=run.status not in TERMINAL_STATUSES,
    )


@router.get(
    "/runs/{run_id}/nodes/{node_id}",
    response_model=NodeRunDetail,
    summary="One node's output, prompt and metrics",
)
async def get_node_run(run_id: uuid.UUID, node_id: str, me: AnyMember, db: Db) -> NodeRunDetail:
    run = await _load_run(db, run_id, me)
    latest = await RunStore(db).latest_by_node(run.id)
    node_run = latest.get(node_id)
    if node_run is None:
        raise problems.not_found(f"Run {run_id} has no node {node_id}.")
    registry = get_registry()
    spec = registry.spec(node_id) if node_id in registry else None
    return NodeRunDetail(
        run_id=run.id,
        node_id=node_run.node_id,
        name=spec.name if spec else node_run.node_id,
        stage=spec.stage if spec else "",
        status=node_run.status,
        attempt=node_run.attempt,
        input_hash=node_run.input_hash,
        output=node_run.output,
        evidence_ids=list(node_run.evidence_ids),
        prompt=node_run.prompt,
        model=node_run.model,
        token_in=node_run.token_in,
        token_out=node_run.token_out,
        cost_usd=node_run.cost_usd,
        latency_ms=node_run.latency_ms,
        started_at=node_run.started_at,
        finished_at=node_run.finished_at,
        error=node_run.error,
    )


@router.get(
    "/runs/{run_id}/diff",
    response_model=RunDiffResponse,
    summary="What changed since another run",
)
async def diff_run(
    run_id: uuid.UUID,
    me: AnyMember,
    db: Db,
    against: uuid.UUID | None = None,
) -> RunDiffResponse:
    """Compare this run's report with another's (PRD §14, §13.4 C).

    `against` defaults to `parent_run_id` — the run that was current when this
    one launched — because that is the comparison the viewer's toggle offers and
    asking the client to look it up first would be a round trip for a value the
    run already carries.
    """
    run = await _load_run(db, run_id, me)
    against_is_parent = against is None
    target_id = against or run.parent_run_id
    if target_id is None:
        raise problems.unprocessable(
            "This is the first completed run for this project, so there is nothing to "
            "compare it against yet.",
            title="Nothing to compare",
        )
    if target_id == run_id:
        raise problems.unprocessable("A run cannot be compared with itself.")

    other = await _load_run(db, target_id, me)
    if other.project_id != run.project_id:
        # Two projects' reports share a schema and nothing else. Diffing them
        # would produce a page of confident nonsense.
        raise problems.unprocessable(
            "Runs belong to different projects and cannot be compared.",
            title="Different projects",
        )

    reports = ReportRepo(db, me.workspace_id)
    current = await reports.for_run(run_id)
    previous = await reports.for_run(target_id)
    missing = [
        str(identifier)
        for identifier, row in ((run_id, current), (target_id, previous))
        if row is None
    ]
    if missing or current is None or previous is None:
        raise problems.not_found(
            f"No report for {' and '.join(missing)}. A run only has one once it finishes."
        )

    result = diff_reports(
        ResearchReport.model_validate(current.payload),
        ResearchReport.model_validate(previous.payload),
    )
    return to_response(result, against_is_parent=against_is_parent)


@router.post(
    "/runs/{run_id}/presence",
    response_model=PresenceResponse,
    summary="Check in as a viewer of this run",
)
async def check_in(run_id: uuid.UUID, me: AnyMember, db: Db) -> PresenceResponse:
    """Say "I am watching this run", and get back everyone else who is.

    A POST that grants nothing: it writes a 30-second mark in Redis and reads
    the set back. `READ` is the right permission because a `viewer` watching a
    run is precisely who this exists to show — anything stricter would make the
    avatars a privilege rather than a courtesy.
    """
    run = await _load_run(db, run_id, me)
    watching = await RunPresence(get_redis(), run.id).check_in(me.user.id)
    names = await UserRepo(db, me.workspace_id).names(watching)
    # Ordered by the name people read, not by Redis's lexicographic uuid order,
    # so the row does not reshuffle itself every time someone checks in.
    viewers = sorted(
        (RunViewer(id=user_id, name=names[user_id]) for user_id in watching if user_id in names),
        key=lambda viewer: viewer.name.casefold(),
    )
    return PresenceResponse(viewers=viewers[:MAX_VIEWERS], total=len(viewers))


# ---------------------------------------------------------------------------
# control
# ---------------------------------------------------------------------------


@router.post(
    "/runs/{run_id}/cancel",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Cancel a run",
)
async def cancel_run(run_id: uuid.UUID, me: RunOperator, request: Request, db: Db) -> RunResponse:
    run = await _load_run(db, run_id, me)
    if run.status in TERMINAL_STATUSES:
        raise problems.conflict(f"This run already finished as {run.status.value}.")

    redis = get_redis()
    await CancelFlag(redis).request(run.id)
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.RUN_CANCELLED,
        target_type=AuditTarget.RUN,
        target_id=run.id,
        meta={"status_at_request": run.status.value},
        ip=client_ip(request),
    )

    store = RunStore(db)
    if run.status in (RunStatus.QUEUED, RunStatus.AWAITING_APPROVAL):
        # No worker is inside this run — it is queued, or parked on a gate — so
        # nobody will ever see the flag. Finalising here is the difference
        # between a cancelled run and one that sits paused forever.
        await store.finish_run(run, status=RunStatus.CANCELLED, error={"code": "cancelled"})
        await store.record_skipped(
            run_id=run.id, node_ids=list(_selected_ids(run, get_dag(run.stage))), reason="cancelled"
        )
        # An open gate on a cancelled run is a question nobody can act on.
        await expire_pending(db, run.id)
        await RunLock(redis, run.stage).release(run.project_id, run.id)
        await RunEventStream(redis, run.id).publish(
            EventType.RUN_COMPLETED, run_id=str(run.id), status=RunStatus.CANCELLED
        )
    await db.commit()
    return await _run_response(db, run, dag=get_dag(run.stage), registry=get_registry())


@router.post(
    "/runs/{run_id}/retry-failed",
    response_model=RunResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Re-run the failed nodes of a finished run",
)
async def retry_failed(run_id: uuid.UUID, me: RunOperator, request: Request, db: Db) -> RunResponse:
    run = await _load_run(db, run_id, me)
    if run.status not in TERMINAL_STATUSES:
        raise problems.conflict(
            f"This run is {run.status.value}. Cancel it before retrying.",
        )

    # The lock comes first: `reset_failed` deletes rows and commits, and a
    # conflict discovered after that would have erased the failure record of a
    # run this caller is not allowed to restart.
    redis = get_redis()
    lock = RunLock(redis, run.stage)
    holder = await lock.acquire(
        run.project_id, LockHolder(run_id=run.id, user_id=me.user.id, user_name=me.user.name)
    )
    if holder is not None and holder.run_id != run.id:
        raise problems.conflict(
            f"{holder.user_name or 'Someone'} is already running this project.",
            title="Project is already running",
            holder=holder.as_dict(),
        )

    store = RunStore(db)
    retried = await store.reset_failed(run.id)
    if not retried:
        await lock.release(run.project_id, run.id)
        raise problems.conflict("This run has no failed nodes to retry.")

    await CancelFlag(redis).clear(run.id)
    run.status = RunStatus.QUEUED
    run.error = None
    run.finished_at = None
    write_audit(
        db,
        workspace_id=me.workspace_id,
        actor_id=me.user.id,
        action=AuditAction.RUN_RETRIED,
        target_type=AuditTarget.RUN,
        target_id=run.id,
        meta={"node_ids": retried},
        ip=client_ip(request),
    )
    await db.commit()

    await RunEventStream(redis, run.id).publish(
        EventType.RUN_STATUS, run_id=str(run.id), status=RunStatus.QUEUED, retrying=retried
    )
    # Not unique: this run id has already been queued once, and arq would treat
    # a second enqueue under the same job id as a duplicate and drop it.
    await enqueue_run(run.id, unique=False)
    return await _run_response(db, run, dag=get_dag(run.stage), registry=get_registry())


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


async def _load_run(db: AsyncSession, run_id: uuid.UUID, me: Principal) -> Run:
    run = await RunRepo(db, me.workspace_id).get(run_id)
    if run is None:
        raise problems.not_found(f"No run {run_id}.")
    return run


def _resolve_selection(dag: Dag, body: LaunchRunRequest) -> set[str]:
    """The node ids a partial run will execute, widened to their dependencies."""
    if body.mode is RunMode.FULL:
        if body.node_ids:
            raise problems.unprocessable(
                "node_ids only applies to a partial run. Set mode='partial'."
            )
        return set()
    if not body.node_ids:
        raise problems.unprocessable(
            "A partial run must name the nodes to run. Send node_ids, or mode='full'."
        )
    try:
        return dag.closure(body.node_ids)
    except DagError as exc:
        raise problems.unprocessable(str(exc), registered_nodes=list(dag.node_ids)) from exc


def _selected_ids(run: Run, dag: Dag) -> tuple[str, ...]:
    node_ids = (run.node_filter or {}).get("node_ids")
    if not node_ids:
        return dag.node_ids
    return tuple(str(node_id) for node_id in node_ids)


async def _run_response(
    db: AsyncSession, run: Run, *, dag: Dag, registry: NodeRegistry
) -> RunResponse:
    latest = await RunStore(db).latest_by_node(run.id)
    selected = set(_selected_ids(run, dag))
    # The console header reads "Triggered by {name}", and a uuid is not a name.
    # Resolved here rather than joined onto the run so a deleted user degrades
    # to "Unknown" instead of taking the whole response down.
    names = await UserRepo(db, run.workspace_id).names(
        [run.triggered_by] if run.triggered_by else []
    )
    nodes = [
        _node_state(registry, node_id, latest.get(node_id))
        for node_id in dag.node_ids
        if node_id in selected
    ]
    return RunResponse(
        id=run.id,
        project_id=run.project_id,
        stage=run.stage,
        status=run.status,
        mode=run.mode,
        trigger=run.trigger,
        triggered_by=run.triggered_by,
        triggered_by_name=names.get(run.triggered_by) if run.triggered_by else None,
        parent_run_id=run.parent_run_id,
        selected_node_ids=sorted(selected),
        cost_usd=run.cost_usd,
        token_in=run.token_in,
        token_out=run.token_out,
        started_at=run.started_at,
        finished_at=run.finished_at,
        error=run.error,
        nodes=nodes,
        edges=[
            DagEdge(source=edge.source, target=edge.target)
            for edge in dag.edges
            if edge.source in selected and edge.target in selected
        ],
        degraded_sources=degraded_sources(latest),
    )


def degraded_sources(latest: dict[str, NodeRun]) -> list[DegradedSource]:
    """Fold every node's `coverage` output into one banner-shaped list.

    Derived from the checkpointed node output rather than from an event: PRD
    §13.5 item 2 has the console reconcile against `GET /runs/{id}` on every SSE
    reconnect, so anything only announced over the stream is gone the moment a
    reader reloads the page. A banner that disappears on refresh is worse than
    no banner, because it teaches people the problem went away.
    """
    found: dict[tuple[str, str], DegradedSource] = {}
    for node_id, node_run in sorted(latest.items()):
        for raw in (node_run.output or {}).get("coverage") or []:
            note = parse_note(str(raw))
            if note is None:
                continue
            severity = "degraded" if note.is_degraded else "unavailable"
            key = (note.kind, severity)
            existing = found.get(key)
            if existing is None:
                found[key] = DegradedSource(
                    kind=note.kind,
                    nodes=[node_id],
                    detail=note.detail,
                    severity=severity,
                )
            else:
                # Same source, another node. One banner entry, every node that
                # hit it — and the first non-empty detail, because the nodes
                # share one connector and therefore one reason.
                existing.nodes.append(node_id)
                existing.detail = existing.detail or note.detail
    # Degraded first: a malfunction is news, an unconfigured source is setup.
    return sorted(found.values(), key=lambda item: (item.severity != "degraded", item.kind))


def _node_state(registry: NodeRegistry, node_id: str, node_run: NodeRun | None) -> NodeState:
    spec = registry.spec(node_id)
    return NodeState(
        id=spec.id,
        name=spec.name,
        stage=spec.stage,
        task_class=spec.task_class,
        depends_on=list(spec.depends_on),
        gate=spec.gate,
        status=node_run.status if node_run else None,
        attempt=node_run.attempt if node_run else None,
        model=node_run.model if node_run else None,
        token_in=node_run.token_in if node_run else None,
        token_out=node_run.token_out if node_run else None,
        cost_usd=node_run.cost_usd if node_run else None,
        latency_ms=node_run.latency_ms if node_run else None,
        started_at=node_run.started_at if node_run else None,
        finished_at=node_run.finished_at if node_run else None,
        error=node_run.error if node_run else None,
    )
