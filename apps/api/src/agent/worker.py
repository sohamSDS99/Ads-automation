"""arq worker entrypoint.

`CMD arq agent.worker.WorkerSettings`. The worker owns the storage Volume and
executes the research DAG; `api` never runs a node.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from arq.connections import RedisSettings

from agent.config import get_settings
from agent.db.session import dispose_engine, get_sessionmaker
from agent.logging_setup import configure_logging
from agent.orchestrator.executor import RunExecutor
from agent.redis_client import close_redis, get_redis

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
    log.info("worker.startup", storage_dir=settings.storage_dir, nodes=len(dag.node_ids))


async def shutdown(ctx: dict[str, Any]) -> None:
    await close_redis()
    await dispose_engine()
    log.info("worker.shutdown")


class WorkerSettings:
    functions = [execute_run]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    job_timeout = JOB_TIMEOUT_SECONDS
    max_tries = MAX_TRIES
    # Playwright is memory-hungry; §9.2 pins browser concurrency at 1. Four
    # concurrent runs is a queue-depth ceiling, not a node-concurrency one —
    # nodes inside a run are limited by the executor's own semaphore.
    max_jobs = 4
