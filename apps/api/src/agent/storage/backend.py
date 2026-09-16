"""The artifact storage boundary.

Railway Volumes attach to exactly one service and container filesystems are
wiped on every deploy (PRD §5.2). Everything durable therefore goes through
this interface onto the worker's Volume — and nothing outside this package is
allowed to open a path directly. `tests/test_filesystem_boundary.py` enforces
that mechanically.
"""

from __future__ import annotations

from typing import IO, Protocol, runtime_checkable

from agent.config import Settings, get_settings


class StorageError(Exception):
    """Storage operation failed, or was refused as unsafe."""


@runtime_checkable
class StorageBackend(Protocol):
    """Where generated artifacts live. `key` is always a relative POSIX path."""

    def put(self, key: str, data: bytes | IO[bytes], *, content_type: str | None = None) -> str:
        """Write `data` at `key`. Returns the key. Overwrites."""
        ...

    def get(self, key: str) -> bytes:
        """Read the whole object. Raises `StorageError` if absent."""
        ...

    def open(self, key: str) -> IO[bytes]:
        """Open the object for streaming reads. Caller closes."""
        ...

    def delete(self, key: str) -> None:
        """Remove the object. Absent keys are not an error."""
        ...

    def exists(self, key: str) -> bool:
        """True if the object is present."""
        ...

    def url_for(self, key: str) -> str:
        """An address the `api` service can fetch this object from."""
        ...


def get_storage(settings: Settings | None = None) -> StorageBackend:
    """Build the configured backend. `local` is the default in every environment."""
    settings = settings or get_settings()
    if settings.storage_backend == "local":
        from agent.storage.local import LocalStorage

        return LocalStorage(settings.storage_dir, settings.worker_internal_url)
    if settings.storage_backend == "s3":
        from agent.storage.s3 import S3Storage

        return S3Storage()
    raise StorageError(f"unknown STORAGE_BACKEND: {settings.storage_backend!r}")
