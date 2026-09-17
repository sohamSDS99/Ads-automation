"""arq cron entrypoints. Thin by design.

Each function here does three things and nothing else: open a session, call the
domain function, and swallow whatever comes back out. The logic is next door in
`poller.py`, `reaper.py`, `reminders.py` and `backups.py`, where it can be
called directly from a test without an arq context.

**Nothing in here may raise.** arq logs a failed cron job and moves on, but a
job that throws has already abandoned whatever it was mid-way through, and the
next tick starts from a state nobody described. Every entrypoint therefore ends
in a bare `except` that logs and returns — the one place in this codebase where
that is the correct shape.
"""

from __future__ import annotations

from typing import Any

import structlog

from agent.db.session import get_sessionmaker
from agent.redis_client import get_redis

log = structlog.get_logger(__name__)


async def poll_schedules_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Launch every schedule that has come due (PRD §17 P8, §5.2)."""
    from agent.scheduling.poller import poll_schedules

    try:
        async with get_sessionmaker()() as session:
            outcome = await poll_schedules(session, get_redis())
    except Exception as exc:  # noqa: BLE001 — see module docstring
        log.error("cron.poll_schedules_failed", error=str(exc))
        return {"error": str(exc)}
    return {
        "considered": outcome.considered,
        "launched": len(outcome.launched),
        "skipped_busy": len(outcome.skipped_busy),
        "failed": len(outcome.failed),
        "disabled": len(outcome.disabled),
    }


async def reap_stale_runs_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Fail runs whose worker died (PRD §16, "Worker killed")."""
    from agent.scheduling.reaper import reap_stale_runs

    try:
        async with get_sessionmaker()() as session:
            outcome = await reap_stale_runs(session, get_redis())
    except Exception as exc:  # noqa: BLE001 — see module docstring
        log.error("cron.reaper_failed", error=str(exc))
        return {"error": str(exc)}
    return {"orphaned": len(outcome.orphaned), "never_started": len(outcome.never_started)}


async def approval_reminders_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Nudge approvers at 50% and 100% of a gate's SLA (PRD §16)."""
    from agent.scheduling.reminders import send_due_reminders

    try:
        async with get_sessionmaker()() as session:
            outcome = await send_due_reminders(session)
    except Exception as exc:  # noqa: BLE001 — see module docstring
        log.error("cron.reminders_failed", error=str(exc))
        return {"error": str(exc)}
    return {"sent": outcome.total, "unaddressed": len(outcome.unaddressed)}


async def nightly_maintenance_job(ctx: dict[str, Any]) -> dict[str, Any]:
    """Back the database up, then prune what has aged out.

    One job rather than two crons, and in this order on purpose: the prune is
    what frees the space the *next* dump needs, so a Volume that is already
    tight still ends the night with a fresh backup on it rather than with a
    failed dump and a tidy disk.
    """
    from agent.auth.bootstrap import get_workspace
    from agent.config import get_settings
    from agent.scheduling.backups import BackupError, run_backup
    from agent.scheduling.retention import prune_storage, record_sweep

    settings = get_settings()
    result: dict[str, Any] = {}

    if settings.backup_enabled:
        try:
            backup = await run_backup(settings=settings)
            result["backup"] = {"key": backup.key, "bytes": backup.bytes}
        except BackupError as exc:
            # Loud, and not fatal to the prune: a workspace whose dump failed
            # still needs its disk swept, and the two failures are unrelated.
            log.error("cron.backup_failed", error=str(exc))
            result["backup_error"] = str(exc)
    else:
        result["backup"] = "disabled"

    try:
        async with get_sessionmaker()() as session:
            outcome = await prune_storage(session, settings=settings)
            workspace = await get_workspace(session)
            if workspace is not None:
                await record_sweep(session, workspace.id, outcome)
    except Exception as exc:  # noqa: BLE001 — see module docstring
        log.error("cron.retention_failed", error=str(exc))
        result["retention_error"] = str(exc)
        return result

    result["retention"] = {
        "deleted": outcome.total_deleted,
        "bytes_freed": outcome.bytes_freed,
        "exports_expired": outcome.exports_expired,
    }
    return result
