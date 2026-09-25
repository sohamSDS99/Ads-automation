"""Filesystem-backed storage, rooted at `STORAGE_DIR`.

This is the default backend for both Compose and Railway. On Railway the root
is the worker's Volume mount at /data; nothing may ever be written outside it.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO
from urllib.parse import quote

from agent.storage.backend import ObjectInfo, StorageError, StorageUsage


class LocalStorage:
    """`StorageBackend` over a single directory tree."""

    def __init__(self, root: str, file_server_url: str = "") -> None:
        self._root = Path(root).resolve()
        self._file_server_url = file_server_url.rstrip("/")

    @property
    def root(self) -> Path:
        return self._root

    def _resolve(self, key: str) -> Path:
        """Map `key` to an absolute path, refusing anything that escapes the root."""
        if not key or key.strip() != key:
            raise StorageError(f"invalid storage key: {key!r}")
        # Validate the raw segments, not PurePosixPath's normalised view: it
        # silently collapses "a//b" and "./a", so two keys would map to one
        # object and a traversal check on the normalised form would pass.
        segments = key.split("/")
        if any(segment in {"", ".", ".."} for segment in segments):
            raise StorageError(f"unsafe storage key: {key!r}")
        candidate = (self._root / PurePosixPath(key)).resolve()
        if candidate != self._root and self._root not in candidate.parents:
            raise StorageError(f"storage key escapes STORAGE_DIR: {key!r}")
        return candidate

    def put(self, key: str, data: bytes | IO[bytes], *, content_type: str | None = None) -> str:
        path = self._resolve(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("wb") as handle:
            if isinstance(data, bytes):
                handle.write(data)
            else:
                shutil.copyfileobj(data, handle)
        return key

    def get(self, key: str) -> bytes:
        path = self._resolve(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise StorageError(f"no such object: {key!r}") from exc

    def open(self, key: str) -> IO[bytes]:
        path = self._resolve(key)
        try:
            return path.open("rb")
        except FileNotFoundError as exc:
            raise StorageError(f"no such object: {key!r}") from exc

    def delete(self, key: str) -> None:
        self._resolve(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._resolve(key).is_file()

    def url_for(self, key: str) -> str:
        """The worker's private-network file server address for this object.

        `api` never reads the Volume itself — it streams from the worker.
        """
        self._resolve(key)
        return f"{self._file_server_url}/files/{quote(key)}"

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        """Walk the tree under `prefix`, yielding one entry per file.

        `prefix` is resolved through the same guard as every other key, so a
        retention rule cannot be pointed at `../` and made to enumerate — or
        prune — outside STORAGE_DIR.
        """
        base = self._resolve(prefix) if prefix else self._root
        if not base.exists():
            return
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            try:
                stat = path.stat()
            except OSError:  # pragma: no cover — deleted between walk and stat
                continue
            yield ObjectInfo(
                key=path.relative_to(self._root).as_posix(),
                bytes=stat.st_size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=UTC),
            )

    def usage(self) -> StorageUsage:
        """Totals for the whole root, plus the size of the filesystem behind it.

        Capacity is the *Volume's*, not the sum of what we wrote: PRD §16's
        "Volume full" banner has to fire on a disk that something else is also
        filling, and a total we computed ourselves would never notice that.
        """
        objects = 0
        total = 0
        for item in self.iter_objects():
            objects += 1
            total += item.bytes
        capacity: int | None = None
        try:
            capacity = os.statvfs(self._root).f_blocks * os.statvfs(self._root).f_frsize
        except (OSError, AttributeError):  # pragma: no cover — non-POSIX or missing root
            capacity = None
        return StorageUsage(objects=objects, bytes=total, capacity_bytes=capacity)

    def free_bytes(self) -> int | None:
        """What the filesystem under the root still gives an unprivileged
        writer (`f_bavail`, not `f_bfree`: the reserved blocks are not ours)."""
        try:
            stats = os.statvfs(self._root)
        except (OSError, AttributeError):  # pragma: no cover — non-POSIX or missing root
            return None
        return stats.f_bavail * stats.f_frsize

    def prune(self, key: str) -> None:
        """Delete an object and any directories it leaves empty.

        The tree is keyed by run id, so pruning a run's screenshots without this
        leaves an empty `creatives/<uuid>/` behind for every run ever executed.
        """
        path = self._resolve(key)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != self._root and parent.is_dir() and not any(parent.iterdir()):
            parent.rmdir()
            parent = parent.parent
