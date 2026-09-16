"""The API's side of the arq queue.

`api` enqueues; `worker` executes. They share nothing but Redis, which is what
keeps a 45-minute run off the request path and lets the API be redeployed while
a run is in flight.
"""

from __future__ import annotations

import uuid

import structlog
from arq import create_pool
from arq.connections import ArqRedis, RedisSettings

from agent.config import get_settings

log = structlog.get_logger(__name__)

EXECUTE_RUN = "execute_run"
GENERATE_EXPORT = "generate_export"

_pool: ArqRedis | None = None


async def get_arq_pool() -> ArqRedis:
    """One pool per process, opened on first enqueue."""
    global _pool
    if _pool is None:
        _pool = await create_pool(RedisSettings.from_dsn(get_settings().redis_url))
    return _pool


async def close_arq_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.aclose()
        _pool = None


async def enqueue_run(run_id: uuid.UUID, *, unique: bool = True) -> str | None:
    """Queue a run for the worker.

    With `unique`, the job id is derived from the run id, so a double-clicked
    launch enqueues once. `retry-failed` passes `unique=False`: the run has
    already been executed under that job id, and arq would silently drop the
    second enqueue.
    """
    pool = await get_arq_pool()
    job_id = f"run:{run_id}" if unique else f"run:{run_id}:{uuid.uuid4().hex[:8]}"
    job = await pool.enqueue_job(EXECUTE_RUN, str(run_id), _job_id=job_id)
    if job is None:
        log.info("run.enqueue_deduped", run_id=str(run_id), job_id=job_id)
        return None
    log.info("run.enqueued", run_id=str(run_id), job_id=job.job_id)
    return str(job.job_id)


async def enqueue_export(export_id: uuid.UUID) -> str | None:
    """Queue an export for the worker (PRD §12).

    The job id is derived from the export row id, so a double-clicked download
    button enqueues once — and unlike a run, there is no `unique=False` variant:
    re-exporting is a new row, not a second job against the same one.
    """
    pool = await get_arq_pool()
    job_id = f"export:{export_id}"
    job = await pool.enqueue_job(GENERATE_EXPORT, str(export_id), _job_id=job_id)
    if job is None:
        log.info("export.enqueue_deduped", export_id=str(export_id), job_id=job_id)
        return None
    log.info("export.enqueued", export_id=str(export_id), job_id=job.job_id)
    return str(job.job_id)
