"""Nightly `pg_dump` onto the Volume.

PRD §17's P8 row: "nightly `pg_dump` + retention job … `pg_dump` lands in
`/data/backups/`". This is a self-hosted product with its own Postgres
container (PRD §18: "NO managed database vendor"), so there is no provider
taking snapshots on our behalf. If this job does not run, there is no backup.

Three decisions worth defending:

* **Custom format (`-Fc`), not plain SQL.** `pg_restore` can then restore one
  table, reorder, or skip the extension setup — which matters because the schema
  depends on `pgvector` and a plain-SQL restore into a database without the
  extension fails halfway through with the data half-loaded.
* **Streamed through `StorageBackend`, never written to a path.** Law 10, and
  the boundary test enforces it: a dump written to a container path is gone at
  the next deploy, which is the exact failure a backup exists to survive.
* **Spooled, not buffered.** A dump of a workspace with a year of evidence is
  not something to hold in the worker's heap. `SpooledTemporaryFile` keeps small
  dumps in memory and spills large ones to the container's scratch disk, which
  is ephemeral and allowed to be.

`pg_dump` itself lives in the worker image (the repo-root `Dockerfile` installs the
PGDG client). Its version must be >= the server's, or it refuses to run — which
is why the image pins the client to 16 rather than taking Ubuntu's 14.
"""

from __future__ import annotations

import asyncio
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import IO, cast
from urllib.parse import urlsplit, urlunsplit

import structlog

from agent.config import Settings, get_settings
from agent.storage.backend import StorageBackend, StorageError, get_storage

log = structlog.get_logger(__name__)

#: Where dumps land, relative to STORAGE_DIR. PRD §17 names `/data/backups/`.
BACKUP_PREFIX = "backups"

#: Above this, the dump spills from memory to the container's scratch disk.
SPOOL_MAX_BYTES = 32 * 1024 * 1024

#: A dump that has not finished in this long is not going to. Generous: it runs
#: at night and a large workspace is slow, but an unbounded wait would pin a
#: worker slot until the process is restarted.
DUMP_TIMEOUT_SECONDS = 30 * 60


class BackupError(RuntimeError):
    """The dump did not complete, and no object was written."""


@dataclass(frozen=True, slots=True)
class BackupResult:
    key: str
    bytes: int
    started_at: datetime
    finished_at: datetime

    @property
    def duration_seconds(self) -> float:
        return (self.finished_at - self.started_at).total_seconds()


def backup_key(moment: datetime) -> str:
    """One object per run, named so a plain listing sorts chronologically."""
    return f"{BACKUP_PREFIX}/ads-research-{moment:%Y%m%dT%H%M%S}Z.dump"


def dump_command(database_url: str) -> list[str]:
    """The `pg_dump` argv.

    `--no-owner` and `--no-acl` because the restore target is a fresh container
    whose role names are not ours to assume; `-Fc` for the custom format;
    `--no-password` so a misconfigured URL fails immediately instead of blocking
    forever on a prompt no one is there to answer.
    """
    return [
        "pg_dump",
        "--format=custom",
        "--compress=6",
        "--no-owner",
        "--no-acl",
        "--no-password",
        _libpq_url(database_url),
    ]


def _libpq_url(database_url: str) -> str:
    """`DATABASE_URL` with any SQLAlchemy driver suffix stripped.

    `postgresql+asyncpg://…` is meaningful to SQLAlchemy and meaningless to
    libpq, which rejects the scheme outright.
    """
    parts = urlsplit(database_url)
    scheme = parts.scheme.split("+", 1)[0] or "postgresql"
    return urlunsplit((scheme, parts.netloc, parts.path, parts.query, parts.fragment))


async def run_backup(
    *,
    settings: Settings | None = None,
    storage: StorageBackend | None = None,
    now: datetime | None = None,
) -> BackupResult:
    """Dump the database onto the Volume. Raises `BackupError` if nothing was written."""
    config = settings or get_settings()
    store = storage or get_storage(config)
    started = now or datetime.now(UTC)
    key = backup_key(started)

    with tempfile.SpooledTemporaryFile(max_size=SPOOL_MAX_BYTES) as spool:
        sink = cast("IO[bytes]", spool)
        written = await _dump_into(
            sink, dump_command(config.database_url), secret=config.database_url
        )
        if written == 0:
            # A zero-byte dump is the one failure mode that looks like success
            # from the outside: the job "ran", an object exists, and it restores
            # nothing. Never write it.
            raise BackupError("pg_dump produced no output")
        sink.seek(0)
        try:
            store.put(key, sink, content_type="application/octet-stream")
        except StorageError as exc:
            raise BackupError(f"could not write {key}: {exc}") from exc

    finished = datetime.now(UTC)
    result = BackupResult(key=key, bytes=written, started_at=started, finished_at=finished)
    log.info(
        "backup.written",
        key=key,
        bytes=written,
        duration_seconds=round(result.duration_seconds, 1),
    )
    return result


async def _dump_into(sink: IO[bytes], command: list[str], *, secret: str) -> int:
    """Run `pg_dump`, streaming stdout into `sink`. Returns bytes written.

    `secret` is the connection string, scrubbed out of anything this reports —
    libpq echoes the DSN back in some failure messages, and a backup job is not
    a reason for the database password to reach the log.
    """
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise BackupError(
            "pg_dump is not on PATH. The worker image installs postgresql-client; "
            "this job cannot run anywhere else."
        ) from exc

    stdout = process.stdout
    stderr = process.stderr
    if stdout is None or stderr is None:  # pragma: no cover — both are PIPEs
        raise BackupError("pg_dump was started without pipes")
    written = 0

    async def pump() -> None:
        nonlocal written
        while True:
            chunk = await stdout.read(64 * 1024)
            if not chunk:
                return
            sink.write(chunk)
            written += len(chunk)

    try:
        async with asyncio.timeout(DUMP_TIMEOUT_SECONDS):
            await pump()
            await process.wait()
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise BackupError(f"pg_dump did not finish within {DUMP_TIMEOUT_SECONDS}s") from exc

    if process.returncode != 0:
        detail = (await stderr.read()).decode("utf-8", "replace").strip()
        raise BackupError(f"pg_dump exited {process.returncode}: {_scrub(detail, secret)[:400]}")
    return written


def _scrub(text: str, secret: str) -> str:
    """Remove the connection string from a message, without mangling the message.

    Only whole-DSN forms are replaced — the full URL, and the `user:pass@host`
    netloc inside it. libpq echoes the connection string, not a bare password,
    so those two cover the real leak.

    A blanket `text.replace(password, ...)` looks safer and is not: this
    installation's default password is `agent`, a test one was `p`, and either
    turns `pg_dump version 14` into `[redacted]g_dum[redacted] version 14`. A
    scrubbed message nobody can read is a worse outcome than the one it was
    protecting against, and it hides the version mismatch that is the single
    most likely reason this job fails.
    """
    if not secret:
        return text
    parts = urlsplit(secret)
    cleaned = text.replace(secret, "[database-url]")
    if parts.netloc and parts.password:
        cleaned = cleaned.replace(parts.netloc, "[database-host]")
        # The credentials pair on its own, which some drivers print separately.
        cleaned = cleaned.replace(f"{parts.username}:{parts.password}", "[credentials]")
    return cleaned
