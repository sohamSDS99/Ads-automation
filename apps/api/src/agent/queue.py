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
MEASURE_IMAGE = "measure_image"

#: How long `POST /lint/image` waits for the worker before giving up. §17 CF5
#: budgets the measurement itself at 3 s p95; the rest is queue time behind
#: another image, since §9.5 measures one at a time. Past this the caller gets
#: an `indeterminate` verdict rather than an error — a precheck that 500s
#: teaches people to skip it, and a precheck that lies teaches them to trust it.
IMAGE_RESULT_TIMEOUT_SECONDS = 25.0

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


async def measure_image(payload: dict[str, object]) -> dict[str, object]:
    """Ask the worker to measure one image, and wait for the answer.

    Synchronous from the caller's point of view and asynchronous underneath,
    which is the only shape that satisfies all three constraints at once: §16
    specifies a route that returns metrics and findings in one response, §9.5
    puts the measurement in `worker`, and §6 keeps `tesseract` out of the `api`
    image entirely.

    **Never raises for an unavailable worker.** Every failure — no worker, a
    dead job, a timeout — returns a `detector_unavailable` measurement, which
    `matchers/image.py` turns into `indeterminate` findings. Law 31: a blocking
    detector that cannot run must not be able to produce a pass, and it must
    not produce a 500 either, because a 500 is indistinguishable from "the
    button is broken" and gets routed around.
    """
    pool = await get_arq_pool()
    job = await pool.enqueue_job(MEASURE_IMAGE, payload)
    if job is None:  # pragma: no cover - only on a job-id collision we do not set
        log.warning("imaging.enqueue_returned_nothing")
        return {"status": "detector_unavailable", "reason": "detector_unavailable"}
    try:
        result = await job.result(timeout=IMAGE_RESULT_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - arq raises several unrelated types here
        log.warning("imaging.result_unavailable", job_id=str(job.job_id), error=str(exc))
        return {"status": "detector_unavailable", "reason": "detector_unavailable"}
    if not isinstance(result, dict):  # pragma: no cover - the worker returns a dict
        log.warning("imaging.result_malformed", job_id=str(job.job_id))
        return {"status": "detector_unavailable", "reason": "detector_unavailable"}
    return result
