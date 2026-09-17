"""arq worker entrypoint.

`CMD arq agent.worker.WorkerSettings`. The worker owns the storage Volume and
executes the research DAG; `api` never runs a node.

It also carries the internal file server (`fileserver.py`). That is not a second
service: the Volume attaches to exactly one container, so the process that owns
the disk has to be the process that serves it. It starts and stops with the
worker rather than alongside it, so there is one lifecycle, not two that can
disagree about whether the Volume is mounted.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from agent.config import get_settings
from agent.db.session import dispose_engine, get_sessionmaker
from agent.export.jobs import generate_export
from agent.fileserver import FileServer
from agent.logging_setup import configure_logging
from agent.orchestrator.executor import RunExecutor
from agent.redis_client import close_redis, get_redis
from agent.scheduling.jobs import (
    approval_reminders_job,
    nightly_maintenance_job,
    poll_schedules_job,
    reap_stale_runs_job,
)

log = structlog.get_logger(__name__)

#: A full 21-node run is allowed 45 minutes (PRD §15 NF1). arq's default of 300s
#: would kill every real run at the five-minute mark.
JOB_TIMEOUT_SECONDS = 60 * 60

#: The executor owns retries, node by node, with its own checkpoints. arq
#: re-running the whole job would re-enter a run that is already running.
MAX_TRIES = 1


async def execute_run(ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    """Run one DAG to a terminal state. Idempotent: a finished run returns at once."""
    identifier = uuid.UUID(run_id)
    async with get_sessionmaker()() as session:
        executor = RunExecutor(db=session, redis=get_redis())
        result = await executor.execute(identifier)
    return {
        "run_id": str(result.run_id),
        "status": result.status.value,
        "cost_usd": str(result.cost_usd),
        "nodes_executed": result.nodes_executed,
    }


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)
    # Import-time validation: the registry and the DAG both fail loudly here
    # rather than on the first job, so a bad node declaration cannot reach a run.
    from agent.orchestrator.dag import get_dag

    dag = get_dag()

    file_server = FileServer(settings)
    await file_server.start()
    ctx["file_server"] = file_server

    log.info(
        "worker.startup",
        storage_dir=settings.storage_dir,
        nodes=len(dag.node_ids),
        file_server_port=settings.file_server_port,
    )

    # PRD §16 calls this a *startup* reaper, and the wording is the design: the
    # common case is this very process having been killed mid-run and restarted,
    # and those runs should be closed out now rather than up to a minute later.
    # The cron below covers the other case — a sibling worker dying while this
    # one stays up — and both are idempotent.
    await reap_stale_runs_job(ctx)


async def shutdown(ctx: dict[str, Any]) -> None:
    file_server: FileServer | None = ctx.get("file_server")
    if file_server is not None:
        await file_server.stop()
    await close_redis()
    await dispose_engine()
    log.info("worker.shutdown")


class WorkerSettings:
    functions = [execute_run, generate_export]
    # Everything unattended. `run_at_startup` is off for all of them: startup
    # already reaps explicitly above, and firing a nightly backup on every
    # deploy would make a busy afternoon of releases into a busy afternoon of
    # `pg_dump`.
    cron_jobs = [
        cron(poll_schedules_job, minute=set(range(60)), run_at_startup=False),
        cron(reap_stale_runs_job, minute=set(range(60)), run_at_startup=False),
        cron(approval_reminders_job, minute={0, 15, 30, 45}, run_at_startup=False),
        # 03:17 rather than 03:00: a self-hosted install is one container, and
        # an off-the-hour slot is less likely to land on whatever else the host
        # runs nightly.
        cron(nightly_maintenance_job, hour={3}, minute={17}, run_at_startup=False),
    ]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    job_timeout = JOB_TIMEOUT_SECONDS
    max_tries = MAX_TRIES
    # Playwright is memory-hungry; §9.2 pins browser concurrency at 1. Four
    # concurrent runs is a queue-depth ceiling, not a node-concurrency one —
    # nodes inside a run are limited by the executor's own semaphore.
    max_jobs = 4
