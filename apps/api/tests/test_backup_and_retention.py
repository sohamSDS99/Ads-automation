"""The nightly job: `pg_dump` onto the Volume, then prune what has aged out.

`pg_dump` itself is not run here — it lives in the worker image and
`scripts/verify-p8.sh` proves it end to end against the real container. What is
pinned here is everything around it: the argv, the fact that a zero-byte dump is
never written, that the connection string never reaches an error message, and
that the retention job deletes by age and by prefix rather than by whatever it
happens to find.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import IO, Any

import pytest

from agent.config import Settings
from agent.scheduling import backups, retention
from agent.storage.local import LocalStorage


def settings_for(root: Path, **overrides: Any) -> Settings:
    return Settings(
        app_encryption_key="d29yZHdvcmR3b3Jkd29yZHdvcmR3b3Jkd29yZHdvcmQ=",
        storage_dir=str(root),
        **overrides,
    )


# ---------------------------------------------------------------------------
# the dump command
# ---------------------------------------------------------------------------


def test_the_sqlalchemy_driver_suffix_is_stripped_for_libpq() -> None:
    """`postgresql+asyncpg://` is meaningful to SQLAlchemy and rejected by libpq."""
    argv = backups.dump_command("postgresql+asyncpg://agent:pw@postgres:5432/agent")
    assert argv[-1] == "postgresql://agent:pw@postgres:5432/agent"


def test_the_dump_is_custom_format_and_claims_no_ownership() -> None:
    """A restore target is a fresh container whose roles are not ours to assume."""
    argv = backups.dump_command("postgresql://agent:pw@postgres:5432/agent")
    assert argv[0] == "pg_dump"
    assert "--format=custom" in argv
    assert "--no-owner" in argv
    assert "--no-acl" in argv
    # Without this a misconfigured URL blocks forever on a prompt nobody answers.
    assert "--no-password" in argv


def test_backup_keys_sort_chronologically() -> None:
    earlier = backups.backup_key(datetime(2026, 9, 1, 3, 17, tzinfo=UTC))
    later = backups.backup_key(datetime(2026, 9, 10, 3, 17, tzinfo=UTC))
    assert earlier < later
    assert earlier.startswith(f"{backups.BACKUP_PREFIX}/")


def test_a_connection_string_never_reaches_an_error_message() -> None:
    dsn = "postgresql://agent:s3cr3t-password@postgres:5432/agent"
    noisy = f'connection to "{dsn}" failed: password authentication failed'
    scrubbed = backups._scrub(noisy, dsn)
    assert "s3cr3t-password" not in scrubbed
    assert dsn not in scrubbed


def test_the_credentials_pair_is_scrubbed_on_its_own() -> None:
    """Some drivers print `user:pass@host` without the scheme around it."""
    dsn = "postgresql://agent:s3cr3t-password@postgres:5432/agent"
    noisy = "could not connect to agent:s3cr3t-password@postgres:5432"
    assert "s3cr3t-password" not in backups._scrub(noisy, dsn)


def test_scrubbing_does_not_mangle_the_message_around_the_secret() -> None:
    """A short password is a substring of ordinary words.

    This installation's default is `agent`; a blanket `replace` turned
    `pg_dump version 14` into `[redacted]g_dum[redacted] version 14` and hid the
    version mismatch that is the likeliest reason this job ever fails.
    """
    dsn = "postgresql://p:p@postgres:5432/agent"
    message = "server version 16; pg_dump version 14; aborting"
    assert backups._scrub(message, dsn) == message


# ---------------------------------------------------------------------------
# run_backup, with pg_dump faked
# ---------------------------------------------------------------------------


class FakeProcess:
    def __init__(self, payload: bytes, returncode: int = 0, stderr: bytes = b"") -> None:
        self._payload = payload
        self._stderr = stderr
        self.returncode = returncode
        self.stdout = self
        self.stderr_reader = self
        self._offset = 0

    async def read(self, size: int = -1) -> bytes:
        chunk = self._payload[self._offset :]
        self._offset = len(self._payload)
        return chunk

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:  # pragma: no cover — only the timeout path calls this
        pass


def fake_exec(payload: bytes, returncode: int = 0, stderr: bytes = b"") -> Any:
    process = FakeProcess(payload, returncode)
    process.stderr = _Reader(stderr)  # type: ignore[attr-defined]

    async def factory(*_args: Any, **_kwargs: Any) -> Any:
        return process

    return factory


class _Reader:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    async def read(self, size: int = -1) -> bytes:
        return self._payload


@pytest.mark.asyncio
async def test_a_dump_lands_in_the_backups_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec(b"PGDMP" + b"x" * 500))
    storage = LocalStorage(str(tmp_path))
    result = await backups.run_backup(settings=settings_for(tmp_path), storage=storage)

    assert result.key.startswith("backups/")
    assert result.bytes == 505
    assert storage.exists(result.key)
    assert storage.get(result.key).startswith(b"PGDMP")


@pytest.mark.asyncio
async def test_an_empty_dump_is_never_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one failure that looks like success: the job ran, an object exists, it restores nothing."""
    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec(b""))
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(backups.BackupError, match="no output"):
        await backups.run_backup(settings=settings_for(tmp_path), storage=storage)
    assert list(storage.iter_objects("backups")) == []


@pytest.mark.asyncio
async def test_a_failing_pg_dump_writes_nothing_and_says_why(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "asyncio.create_subprocess_exec",
        fake_exec(b"partial", returncode=1, stderr=b"server version 16; pg_dump version 14"),
    )
    storage = LocalStorage(str(tmp_path))
    with pytest.raises(backups.BackupError, match="pg_dump version 14"):
        await backups.run_backup(settings=settings_for(tmp_path), storage=storage)
    assert list(storage.iter_objects("backups")) == []


@pytest.mark.asyncio
async def test_a_missing_pg_dump_names_the_image_that_has_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def missing(*_args: Any, **_kwargs: Any) -> Any:
        raise FileNotFoundError("pg_dump")

    monkeypatch.setattr("asyncio.create_subprocess_exec", missing)
    with pytest.raises(backups.BackupError, match="worker image"):
        await backups.run_backup(
            settings=settings_for(tmp_path), storage=LocalStorage(str(tmp_path))
        )


# ---------------------------------------------------------------------------
# storage listing and usage
# ---------------------------------------------------------------------------


def write_aged(storage: LocalStorage, key: str, *, days_old: float, payload: bytes = b"x") -> None:
    storage.put(key, payload)
    target = time.time() - days_old * 86_400
    os.utime(Path(storage.root) / key, (target, target))


def test_iter_objects_walks_a_prefix_and_reports_size_and_age(tmp_path: Path) -> None:
    storage = LocalStorage(str(tmp_path))
    write_aged(storage, "creatives/run-a/one.png", days_old=1, payload=b"12345")
    write_aged(storage, "creatives/run-b/two.png", days_old=1)
    write_aged(storage, "exports/run-a/report.pdf", days_old=1)

    creatives = sorted(storage.iter_objects("creatives"), key=lambda item: item.key)
    assert [item.key for item in creatives] == [
        "creatives/run-a/one.png",
        "creatives/run-b/two.png",
    ]
    assert creatives[0].bytes == 5
    assert creatives[0].modified_at.tzinfo is not None


def test_iter_objects_on_a_prefix_that_does_not_exist_is_empty_not_an_error(
    tmp_path: Path,
) -> None:
    assert list(LocalStorage(str(tmp_path)).iter_objects("nothing-here")) == []


def test_a_prefix_cannot_escape_the_storage_root(tmp_path: Path) -> None:
    """A retention rule pointed at `../` must not enumerate — or prune — outside."""
    from agent.storage.backend import StorageError

    with pytest.raises(StorageError):
        list(LocalStorage(str(tmp_path / "root")).iter_objects("../.."))


def test_usage_totals_what_is_stored(tmp_path: Path) -> None:
    storage = LocalStorage(str(tmp_path))
    storage.put("creatives/a.png", b"1234567890")
    storage.put("exports/b.pdf", b"12345")
    usage = storage.usage()
    assert usage.objects == 2
    assert usage.bytes == 15
    # The Volume's own size, not the sum above — §16's banner has to notice a
    # disk something else is also filling.
    assert usage.capacity_bytes is None or usage.capacity_bytes > usage.bytes


def test_pruning_removes_the_directories_it_empties(tmp_path: Path) -> None:
    storage = LocalStorage(str(tmp_path))
    storage.put("creatives/run-a/one.png", b"x")
    storage.prune("creatives/run-a/one.png")
    assert not (Path(storage.root) / "creatives" / "run-a").exists()


# ---------------------------------------------------------------------------
# retention rules
# ---------------------------------------------------------------------------


def test_each_prefix_has_its_own_window() -> None:
    rules = {
        rule.prefix: rule.days
        for rule in retention.rules_for(
            Settings(app_encryption_key="d29yZHdvcmR3b3Jkd29yZHdvcmR3b3Jkd29yZHdvcmQ=")
        )
    }
    assert set(rules) == {"creatives", "debug", "exports", "backups"}
    # Evidence a report cites outlives debris an engineer reads once.
    assert rules["creatives"] > rules["debug"]


def test_a_window_of_zero_keeps_forever() -> None:
    rule = retention.PrefixRule("creatives", 0, "screenshots")
    assert rule.cutoff(datetime.now(UTC)) is None


def test_a_window_is_measured_in_days_back_from_now() -> None:
    now = datetime(2026, 9, 17, 12, tzinfo=UTC)
    assert retention.PrefixRule("creatives", 90, "screenshots").cutoff(now) == now - timedelta(
        days=90
    )


@pytest.mark.asyncio
async def test_pruning_deletes_only_what_is_past_its_own_window(tmp_path: Path) -> None:
    storage = LocalStorage(str(tmp_path))
    write_aged(storage, "creatives/run-a/old.png", days_old=120)
    write_aged(storage, "creatives/run-a/new.png", days_old=10)
    write_aged(storage, "debug/transparency/old.html", days_old=30)
    write_aged(storage, "debug/transparency/new.html", days_old=2)
    write_aged(storage, "backups/ancient.dump", days_old=60)
    write_aged(storage, "backups/recent.dump", days_old=1)

    config = settings_for(
        tmp_path,
        screenshot_retention_days=90,
        debug_retention_days=14,
        backup_retention_days=14,
        export_retention_days=30,
    )
    outcome = await retention.prune_storage(_NoDb(), settings=config, storage=storage)

    remaining = {item.key for item in storage.iter_objects()}
    assert remaining == {
        "creatives/run-a/new.png",
        "debug/transparency/new.html",
        "backups/recent.dump",
    }
    assert outcome.total_deleted == 3
    assert outcome.bytes_freed == 3


class _NoDb:
    """A session stand-in for the prefix rules, which touch no database.

    The export reconciliation half of the job does, and is covered by
    `tests/integration/test_retention_api.py` against a real one.
    """

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:  # pragma: no cover
        raise AssertionError("prune_storage should not query with no expired exports")

    async def rollback(self) -> None:  # pragma: no cover
        pass

    async def commit(self) -> None:  # pragma: no cover
        pass


def test_the_spooled_dump_is_an_io_object_the_storage_backend_accepts(tmp_path: Path) -> None:
    """`put` streams a file object; a `SpooledTemporaryFile` has to satisfy that."""
    import tempfile

    storage = LocalStorage(str(tmp_path))
    with tempfile.SpooledTemporaryFile(max_size=16) as spool:
        handle: IO[bytes] = spool  # type: ignore[assignment]
        handle.write(b"a" * 64)
        handle.seek(0)
        storage.put("backups/spooled.dump", handle)
    assert storage.get("backups/spooled.dump") == b"a" * 64
