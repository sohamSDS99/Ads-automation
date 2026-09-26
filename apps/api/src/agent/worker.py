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

import asyncio
import hashlib
import re
import uuid
from typing import Any

import structlog
from arq import cron
from arq.connections import RedisSettings

from agent.config import get_settings
from agent.creative.constants import get_creative_constants
from agent.credentials import MissingCredential
from agent.db.models import GenerationJob, Run
from agent.db.session import dispose_engine, get_sessionmaker
from agent.export.jobs import generate_export
from agent.fileserver import FileServer
from agent.logging_setup import configure_logging
from agent.media import runtime as media_runtime
from agent.orchestrator.executor import RunExecutor
from agent.planning.constants import get_planning_constants
from agent.redis_client import close_redis, get_redis
from agent.scheduling.jobs import (
    approval_reminders_job,
    nightly_maintenance_job,
    policy_watch_job,
    poll_schedules_job,
    reap_stale_runs_job,
)
from agent.storage.backend import get_storage

log = structlog.get_logger(__name__)

#: A full 21-node run is allowed 45 minutes (PRD §15 NF1). arq's default of 300s
#: would kill every real run at the five-minute mark.
JOB_TIMEOUT_SECONDS = 60 * 60

#: The executor owns retries, node by node, with its own checkpoints. arq
#: re-running the whole job would re-enter a run that is already running.
MAX_TRIES = 1

#: §17 CF5 gives the image precheck 3 s p95. Ten is the hard stop: long enough
#: that a big JPEG on a busy worker still succeeds, short enough that a wedged
#: `tesseract` cannot hold the single measurement slot for the hour
#: `JOB_TIMEOUT_SECONDS` would otherwise allow. A timeout here surfaces as
#: `detector_unavailable`, which law 31 makes `indeterminate` rather than a pass.
IMAGE_MEASURE_TIMEOUT_SECONDS = 10

#: §9.5 and open question Q6: "one image at a time". `max_jobs` is 4, so
#: without this four uploads would OCR concurrently on a container whose memory
#: floor was sized for one. Process-wide because the worker is one process; if
#: it ever becomes several, this has to become a Redis lock and the comment is
#: here so that is a decision rather than a surprise.
_IMAGE_SLOT = asyncio.Semaphore(1)


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


async def check_generation_job(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """ "Check again" on one generation job (Stage 04 PRD §16, §18).

    `MediaJobs.check()` does the work: a timed-out video resumes polling with
    a fresh window, an image whose submit state is unknown is POSTed again
    under its own key. Anything else comes back unchanged — the route only
    enqueues what `media.jobs.checkable` accepts, and this re-reads the row
    rather than trusting that it is still in that state.
    """
    identifier = uuid.UUID(job_id)
    async with get_sessionmaker()() as session:
        row = await session.get(GenerationJob, identifier)
        if row is None:
            log.warning("generation_check.missing", generation_job_id=job_id)
            return {"job_id": job_id, "status": None}
        run = await session.get(Run, row.creative_run_id)
        choice = media_runtime.choice_for(media_runtime.pinned_choices(run), row)
        try:
            jobs, client = await media_runtime.media_jobs(session, workspace_id=row.workspace_id)
        except MissingCredential as exc:
            log.warning("generation_check.no_credential", generation_job_id=job_id, reason=str(exc))
            return {"job_id": job_id, "status": row.status.value}
    try:
        result = await jobs.check(identifier, choice=choice)
    finally:
        await client.aclose()
    return {"job_id": job_id, "status": result.status.value}


async def regenerate_asset(ctx: dict[str, Any], asset_id: str) -> dict[str, Any]:
    """An operator's media regeneration before G8 (Stage 04 PRD §16), through
    node 4.4.6's own code (`orchestrator/regeneration.py`)."""
    from agent.orchestrator.regeneration import run_regeneration

    return await run_regeneration(uuid.UUID(asset_id))


async def measure_image(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Measure one image for the precheck (PRD §9.5). Worker-only, by design.

    This is the only place `tesseract` and OpenCV are used, and the worker is
    the only image that carries them — §6 keeps `api` slim, so `api` receives
    the upload, enqueues this, and adjudicates the numbers that come back with
    the pure rules in `guardrails/`.

    Runs in a thread because the measurement is CPU-bound C code: awaiting it
    on the event loop would stall every other job in this worker for the
    duration, including the SSE heartbeats of a run in flight.
    """
    from agent.imaging import precheck

    content = payload["content"]
    templates = tuple(
        precheck.LogoTemplateData(
            asset_id=uuid.UUID(str(item["asset_id"])),
            label=str(item["label"]),
            phash=str(item["phash"]),
            descriptors_b64=str(item.get("descriptors_b64") or ""),
            keypoint_count=int(item.get("keypoint_count") or 0),
            min_score=float(item["min_score"]),
        )
        for item in payload.get("templates") or []
    )

    async with _IMAGE_SLOT:
        try:
            measurement = await asyncio.wait_for(
                asyncio.to_thread(
                    precheck.measure,
                    content,
                    templates=templates,
                    working_width=int(payload.get("working_width") or 1280),
                    lang=str(payload.get("lang") or "eng"),
                ),
                timeout=IMAGE_MEASURE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # Fails closed (law 31). The caller turns a `detector_unavailable`
            # measurement into `indeterminate` findings, never into a pass.
            log.warning("imaging.timeout", seconds=IMAGE_MEASURE_TIMEOUT_SECONDS)
            return {
                "status": "detector_unavailable",
                "reason": "detector_timeout",
            }
    return measurement.model_dump(mode="json")


#: `references/{project_id}/{sha256}.{ext}` (PRD §7.4) and nothing else.
_REFERENCE_KEY = re.compile(
    r"references/[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}/"
    r"(?P<sha256>[0-9a-f]{64})\.(?:png|jpg|webp)"
)


async def store_reference(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Write one media reference to the Volume (Stage 04 PRD §10.3, §7.4).

    Worker-only because the worker owns the Volume; `api` decodes and hashes the
    upload, then enqueues this and waits (`queue.store_reference`). The bytes are
    hashed again here and must match both the payload and the key, and the key
    must be exactly a reference key — so this job can neither store bytes other
    than the ones attested nor write anywhere else on the Volume.
    """
    key = str(payload["key"])
    content = payload["content"]
    if not isinstance(content, bytes | bytearray):
        raise ValueError("store_reference needs the file's bytes")
    match = _REFERENCE_KEY.fullmatch(key)
    if match is None:
        raise ValueError(f"refusing to store a reference under {key!r}")
    digest = hashlib.sha256(content).hexdigest()
    if digest != payload["sha256"] or digest != match["sha256"]:
        raise ValueError("the reference bytes do not hash to the sha256 they were sent with")
    await asyncio.to_thread(
        get_storage().put, key, bytes(content), content_type=str(payload["content_type"])
    )
    log.info("media_reference.stored", key=key, bytes=len(content))
    return {"key": key, "bytes": len(content), "sha256": digest}


async def _tool_version(*argv: str) -> str | None:
    """The first line `argv` prints, or None when the binary is absent or fails.

    Never raises: this feeds a log line, and a host without ffmpeg (the test
    suite, a laptop) must still be able to run `startup`.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
    except OSError:
        return None
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout=10)
    except TimeoutError:
        process.kill()
        await process.wait()
        return None
    if process.returncode != 0:
        return None
    lines = stdout.decode(errors="replace").strip().splitlines()
    return lines[0] if lines else None


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)
    # Import-time validation: the registry and the DAG both fail loudly here
    # rather than on the first job, so a bad node declaration cannot reach a run.
    from agent.orchestrator.dag import all_dags

    dags = all_dags()
    # Same reason as the api's lifespan, one process further along: the worker is
    # what actually runs a plan, so an unattributable planning constant has to
    # stop it here rather than surface as a wrong number inside a finished plan
    # (global law 15).
    constants = get_planning_constants()
    # Stage 04 law 25: the worker is what submits media and writes packages, so
    # an unattributable creative constant stops it here, naming the key.
    creative = get_creative_constants()

    file_server = FileServer(settings)
    await file_server.start()
    ctx["file_server"] = file_server

    # Stage 04's post-production shells out to both (§9.4, §13). The image build
    # fails without them; logging the versions here is what proves the RUNNING
    # container is that image — on Railway, a worker built from the wrong
    # Dockerfile has reported SUCCESS before.
    ffmpeg_version, exiftool_version = await asyncio.gather(
        _tool_version("ffmpeg", "-version"), _tool_version("exiftool", "-ver")
    )

    log.info(
        "worker.startup",
        storage_dir=settings.storage_dir,
        nodes={stage.value: len(dag.node_ids) for stage, dag in dags.items()},
        planning_constants=constants.version,
        creative_constants=creative.version,
        file_server_port=settings.file_server_port,
        ffmpeg=ffmpeg_version,
        exiftool=exiftool_version,
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


#: A package file's path inside `package/{package_id}/` — what
#: `creative/package.assemble` names: media under `media/`, patches under
#: `landing/`, never a `..` segment.
_PACKAGE_PATH = re.compile(r"(?:media|landing)/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_-][A-Za-z0-9_.-]*")
#: Where a copied package file may come from: the creative run's own files.
_PACKAGE_SOURCE = "creative/"


async def write_package_files(ctx: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    """Write a package's files under `package/{package_id}/` (Stage 04 PRD §12.4).

    Each file is either copied from the creative run's own storage
    (`source_key`) or written from the bytes release sends (`content` — a
    landing patch rendered from its row). Every file is **read back and
    hashed after it lands**, so the receipt release checks against the
    manifest is of the bytes on the Volume, not of the bytes it meant to
    write. A path outside `media/` or `landing/`, or a source outside the
    run's `creative/` files, is refused before anything is written.
    """
    package_id = uuid.UUID(str(payload["package_id"]))
    files = list(payload["files"])
    for item in files:
        path, source = str(item["path"]), item.get("source_key")
        if not _PACKAGE_PATH.fullmatch(path) or ".." in path.split("/"):
            raise ValueError(f"refusing to write a package file at {path!r}")
        if source is not None and not str(source).startswith(_PACKAGE_SOURCE):
            raise ValueError(f"refusing to copy {source!r} into a package")
        if source is None and not isinstance(item.get("content"), bytes | bytearray):
            raise ValueError(f"{path} carries neither a source key nor its bytes")
    storage = get_storage()
    written: list[dict[str, Any]] = []
    for item in files:
        path, source = str(item["path"]), item.get("source_key")
        data = (
            await asyncio.to_thread(storage.get, str(source))
            if source is not None
            else bytes(item["content"])
        )
        key = f"package/{package_id}/{path}"
        await asyncio.to_thread(storage.put, key, data, content_type=item.get("media_type"))
        landed = await asyncio.to_thread(storage.get, key)
        written.append(
            {
                "path": path,
                "key": key,
                "sha256": hashlib.sha256(landed).hexdigest(),
                "bytes": len(landed),
            }
        )
    log.info("package_files.written", package_id=str(package_id), files=len(written))
    return {"files": written}


class WorkerSettings:
    functions = [
        execute_run,
        generate_export,
        measure_image,
        check_generation_job,
        regenerate_asset,
        store_reference,
        write_package_files,
    ]
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
        # 04:00 UTC, the cadence `policy_sources.yaml` ships (Q10). Deliberately
        # after the 03:17 maintenance window: the sweep writes amendments, and a
        # prune running underneath it would be competing for the same rows.
        cron(policy_watch_job, hour={4}, minute={0}, run_at_startup=False),
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
