"""arq worker entrypoint.

`CMD arq agent.worker.WorkerSettings`. The worker owns the storage Volume and,
from P1 onward, executes the research DAG. In P0 it only proves the service
boots and can drain a job.
"""

from __future__ import annotations

from typing import Any

import structlog
from arq.connections import RedisSettings

from agent.config import get_settings
from agent.logging_setup import configure_logging

log = structlog.get_logger(__name__)


async def noop(ctx: dict[str, Any]) -> str:
    """Smallest possible job. Keeps the function registry non-empty until P1."""
    return "ok"


async def startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    configure_logging(settings)
    log.info("worker.startup", storage_dir=settings.storage_dir)


async def shutdown(ctx: dict[str, Any]) -> None:
    log.info("worker.shutdown")


class WorkerSettings:
    functions = [noop]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(get_settings().redis_url)
    # Playwright is memory-hungry; §9.2 pins browser concurrency at 1. Job
    # concurrency is raised in P1 when there are real jobs to run.
    max_jobs = 4
