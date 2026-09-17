"""Prune the Volume, and keep the database honest about what is left on it.

PRD §16, "Volume full": "Storage check before each run; nightly retention job
prunes screenshots beyond the configured window; admin sees a storage banner in
`/settings`." This is the middle clause, plus the part that clause does not say
out loud: deleting a file that a database row still points at is not tidying up,
it is manufacturing a broken link.

So the job has two halves that run in order:

1. **Prune by age**, per prefix, with a window per prefix. The windows differ
   because the objects differ in kind: a creative screenshot is *evidence a
   report cites* and gets a quarter; a dumped failure page is a debugging aid
   and gets a fortnight; a rendered export is regenerable from the stored report
   in seconds and gets a month.
2. **Reconcile.** Any `Export` row whose object just went is moved out of
   `ready` and given a reason, so the Report Viewer offers a regenerate rather
   than a download that 404s.

Evidence rows are deliberately *not* reconciled. An `Evidence` row is the record
that something was observed, with its extracted text and its embedding; the
screenshot is an attachment to it. Deleting the row because its picture aged out
would delete the citation a published report depends on — and PRD §15 NF6 says
every claim resolves to an evidence id. The gallery renders a missing-image
state instead, which is the honest rendering of "we saw this in March and no
longer keep the picture".
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from agent.audit import AuditAction, AuditTarget, write_audit
from agent.config import Settings, get_settings
from agent.db.models import Export, ExportStatus
from agent.export.jobs import EXPORT_PREFIX
from agent.scheduling.backups import BACKUP_PREFIX
from agent.storage.backend import StorageBackend, StorageUsage, get_storage

log = structlog.get_logger(__name__)

#: Creative screenshots, written by the Transparency Center connector.
CREATIVE_PREFIX = "creatives"
#: Connector failure dumps (PRD §16, "Transparency Center selector change").
DEBUG_PREFIX = "debug"

#: The message an expired export carries, in the words the viewer shows.
EXPIRED_MESSAGE = "This file has passed its retention window. Generate it again from the report."


@dataclass(frozen=True, slots=True)
class PrefixRule:
    """One prefix, and how long an object under it is kept."""

    prefix: str
    days: int
    label: str

    def cutoff(self, moment: datetime) -> datetime | None:
        """The age boundary, or None when the window is 'keep forever'."""
        return None if self.days <= 0 else moment - timedelta(days=self.days)


@dataclass(frozen=True, slots=True)
class RetentionOutcome:
    deleted: dict[str, int] = field(default_factory=dict)
    bytes_freed: int = 0
    exports_expired: int = 0
    usage: StorageUsage | None = None

    @property
    def total_deleted(self) -> int:
        return sum(self.deleted.values())


def rules_for(settings: Settings) -> tuple[PrefixRule, ...]:
    """The configured windows, one per prefix. A window of 0 keeps forever."""
    return (
        PrefixRule(CREATIVE_PREFIX, settings.screenshot_retention_days, "screenshots"),
        PrefixRule(DEBUG_PREFIX, settings.debug_retention_days, "debug dumps"),
        PrefixRule(EXPORT_PREFIX, settings.export_retention_days, "exports"),
        PrefixRule(BACKUP_PREFIX, settings.backup_retention_days, "backups"),
    )


async def prune_storage(
    db: AsyncSession,
    *,
    settings: Settings | None = None,
    storage: StorageBackend | None = None,
    now: datetime | None = None,
) -> RetentionOutcome:
    """Delete aged-out objects and reconcile the rows that pointed at them."""
    config = settings or get_settings()
    store = storage or get_storage(config)
    moment = now or datetime.now(UTC)

    deleted: dict[str, int] = {}
    freed = 0
    expired_keys: list[str] = []

    for rule in rules_for(config):
        cutoff = rule.cutoff(moment)
        if cutoff is None:
            continue
        removed = 0
        for item in list(store.iter_objects(rule.prefix)):
            if item.modified_at >= cutoff:
                continue
            store.delete(item.key)
            removed += 1
            freed += item.bytes
            if rule.prefix == EXPORT_PREFIX:
                expired_keys.append(item.key)
        if removed:
            deleted[rule.label] = removed
            log.info(
                "retention.pruned",
                prefix=rule.prefix,
                deleted=removed,
                older_than_days=rule.days,
            )

    exports_expired = await _expire_exports(db, expired_keys, moment=moment)
    usage = store.usage()

    outcome = RetentionOutcome(
        deleted=deleted,
        bytes_freed=freed,
        exports_expired=exports_expired,
        usage=usage,
    )
    if outcome.total_deleted:
        log.info(
            "retention.swept",
            deleted=outcome.total_deleted,
            bytes_freed=freed,
            exports_expired=exports_expired,
        )
    _warn_if_full(config, usage)
    return outcome


async def _expire_exports(db: AsyncSession, keys: list[str], *, moment: datetime) -> int:
    """Move every `ready` export whose file just went to `failed`, with a reason.

    Not deleted: the row is the record that someone asked for this format of
    this report, and the audit log refers to it. What changes is that it stops
    claiming to be downloadable.
    """
    if not keys:
        return 0
    result = await db.execute(
        sa.select(Export).where(Export.path.in_(set(keys)), Export.status == ExportStatus.READY)
    )
    rows = list(result.scalars().all())
    if not rows:
        await db.rollback()
        return 0
    for row in rows:
        row.status = ExportStatus.FAILED
        row.error = EXPIRED_MESSAGE
        row.path = None
    log.info("retention.exports_expired", count=len(rows), at=moment.isoformat())
    await db.commit()
    return len(rows)


def _warn_if_full(settings: Settings, usage: StorageUsage) -> None:
    """Log loudly when the Volume is filling. `/settings` reads the same numbers."""
    fraction = usage.used_fraction
    if fraction is None or fraction < settings.storage_warn_fraction:
        return
    log.warning(
        "storage.filling",
        used_bytes=usage.bytes,
        capacity_bytes=usage.capacity_bytes,
        used_fraction=round(fraction, 3),
        threshold=settings.storage_warn_fraction,
    )


async def record_sweep(
    db: AsyncSession, workspace_id: uuid.UUID, outcome: RetentionOutcome
) -> None:
    """Audit one sweep. Separate from the sweep so a read-only caller can skip it."""
    if not outcome.total_deleted and not outcome.exports_expired:
        return
    write_audit(
        db,
        workspace_id=workspace_id,
        actor_id=None,
        action=AuditAction.RETENTION_PRUNED,
        target_type=AuditTarget.BACKUP,
        meta={
            "deleted": outcome.deleted,
            "bytes_freed": outcome.bytes_freed,
            "exports_expired": outcome.exports_expired,
        },
    )
    await db.commit()
