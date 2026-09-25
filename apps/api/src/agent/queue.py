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
STORE_REFERENCE = "store_reference"
CHECK_GENERATION_JOB = "check_generation_job"
REGENERATE_ASSET = "regenerate_asset"

#: How long `POST /lint/image` waits for the worker before giving up. §17 CF5
#: budgets the measurement itself at 3 s p95; the rest is queue time behind
#: another image, since §9.5 measures one at a time. Past this the caller gets
#: an `indeterminate` verdict rather than an error — a precheck that 500s
#: teaches people to skip it, and a precheck that lies teaches them to trust it.
IMAGE_RESULT_TIMEOUT_SECONDS = 25.0
#: How long `POST /projects/{id}/media-references` waits for the worker to write
#: the file. A write of at most `media.reference_max_bytes` takes milliseconds;
#: the rest is queue time behind whatever the worker is already running.
REFERENCE_STORE_TIMEOUT_SECONDS = 30.0


class WorkerUnavailable(RuntimeError):
    """No worker did the job in time. The caller writes nothing and says so."""


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


async def enqueue_generation_check(job_id: uuid.UUID, *, state: str) -> str | None:
    """Queue "Check again" on one generation job (Stage 04 PRD §16, §18).

    Re-polling a video waits out its whole poll window, which is minutes, so
    it runs in the worker. The job id is derived from the row **and its
    state**: a double-click enqueues once, but a job that times out again an
    hour later can be checked again — arq keeps a finished job's id for an
    hour and would otherwise drop the second enqueue without a word.
    """
    pool = await get_arq_pool()
    arq_id = f"generation-check:{job_id}:{state}"
    job = await pool.enqueue_job(CHECK_GENERATION_JOB, str(job_id), _job_id=arq_id)
    if job is None:
        log.info("generation_check.enqueue_deduped", generation_job_id=str(job_id), job_id=arq_id)
        return None
    log.info("generation_check.enqueued", generation_job_id=str(job_id), job_id=job.job_id)
    return str(job.job_id)


async def enqueue_regeneration(asset_id: uuid.UUID, *, redrive: bool = False) -> str | None:
    """Queue an operator's regeneration before G8 (Stage 04 PRD §16).

    The job id is derived from the new asset, so a double-click enqueues once.
    `redrive` re-queues one whose worker died mid-way: arq keeps a job id for
    an hour, and the regeneration resumes from its committed jobs (Law 37),
    so a second id is safe and the first would be dropped without a word.
    """
    pool = await get_arq_pool()
    arq_id = f"regenerate:{asset_id}" + (f":{uuid.uuid4().hex[:8]}" if redrive else "")
    job = await pool.enqueue_job(REGENERATE_ASSET, str(asset_id), _job_id=arq_id)
    if job is None:
        log.info("regeneration.enqueue_deduped", asset_id=str(asset_id), job_id=arq_id)
        return None
    log.info("regeneration.enqueued", asset_id=str(asset_id), job_id=job.job_id)
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


async def store_reference(payload: dict[str, object]) -> dict[str, object]:
    """Have the worker write one reference image to the Volume, and wait.

    The worker owns the Volume (Stage 04 PRD §22); `api` mounts none, so a file
    `api` wrote itself would land on a disk the worker — which sends references
    to providers and composites product photos — can never read. Unlike
    `measure_image` there is no degraded answer: a reference row without its
    bytes would be a reference nobody can use, so every failure raises
    `WorkerUnavailable` and the route writes no row.
    """
    pool = await get_arq_pool()
    job = await pool.enqueue_job(STORE_REFERENCE, payload)
    if job is None:  # pragma: no cover - only on a job-id collision we do not set
        raise WorkerUnavailable("the worker queue refused the job")
    try:
        result = await job.result(timeout=REFERENCE_STORE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - arq raises several unrelated types here
        log.warning("media_reference.store_unavailable", job_id=str(job.job_id), error=str(exc))
        raise WorkerUnavailable(
            f"the worker did not store the file ({type(exc).__name__})"
        ) from exc
    if not isinstance(result, dict):  # pragma: no cover - the worker returns a dict
        raise WorkerUnavailable("the worker returned no receipt for the file")
    return result
