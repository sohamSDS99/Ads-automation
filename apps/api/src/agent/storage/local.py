"""Filesystem-backed storage, rooted at `STORAGE_DIR`.

This is the default backend for both Compose and Railway. On Railway the root
is the worker's Volume mount at /data; nothing may ever be written outside it.
"""

from __future__ import annotations

import shutil
from pathlib import Path, PurePosixPath
from typing import IO
from urllib.parse import quote

from agent.storage.backend import StorageError


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
