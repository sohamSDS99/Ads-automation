"""The artifact storage boundary.

Railway Volumes attach to exactly one service and container filesystems are
wiped on every deploy (PRD §5.2). Everything durable therefore goes through
this interface onto the worker's Volume — and nothing outside this package is
allowed to open a path directly. `tests/test_filesystem_boundary.py` enforces
that mechanically.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from typing import IO, Protocol, runtime_checkable

from agent.config import Settings, get_settings


class StorageError(Exception):
    """Storage operation failed, or was refused as unsafe."""


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    """What the retention job needs to decide whether an object has aged out."""

    key: str
    bytes: int
    modified_at: datetime


@dataclass(frozen=True, slots=True)
class StorageUsage:
    """Totals for the storage banner in `/settings` (PRD §16, "Volume full")."""

    objects: int
    bytes: int
    #: Capacity of the filesystem the root sits on, when the backend can know
    #: it. `None` for a backend with no fixed size — an object store is not
    #: full, it is only expensive.
    capacity_bytes: int | None = None

    @property
    def used_fraction(self) -> float | None:
        if not self.capacity_bytes:
            return None
        return self.bytes / self.capacity_bytes


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

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """Every object under `prefix`, in no guaranteed order.

        Added in P8 for the retention job, which cannot prune what it cannot
        enumerate. Streaming rather than returning a list: a Volume holding a
        year of creative screenshots has no reason to be materialised in memory
        to delete forty of them.
        """
        ...

    def usage(self) -> StorageUsage:
        """How much is stored, and how much room is left if that is knowable."""
        ...

    def free_bytes(self) -> int | None:
        """Bytes a write can still use right now, or None for a backend with
        no fixed size. Cheap — unlike `usage()`, it walks nothing."""
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
