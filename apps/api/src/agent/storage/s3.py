"""S3-compatible storage (Cloudflare R2 and friends).

Stub until artifacts outgrow a single Railway Volume. Selecting it must fail
loudly rather than silently writing nowhere.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import IO

from agent.storage.backend import ObjectInfo, StorageUsage


class S3Storage:
    """Not implemented — see PRD §5.2 'Artifact storage'."""

    def __init__(self) -> None:
        raise NotImplementedError(
            "STORAGE_BACKEND=s3 is not implemented yet. The default `local` "
            "backend writes to the worker's Volume at STORAGE_DIR."
        )

    def put(self, key: str, data: bytes | IO[bytes], *, content_type: str | None = None) -> str:
        raise NotImplementedError

    def get(self, key: str) -> bytes:
        raise NotImplementedError

    def open(self, key: str) -> IO[bytes]:
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def url_for(self, key: str) -> str:
        raise NotImplementedError

    def iter_objects(self, prefix: str = "") -> Iterator[ObjectInfo]:
        raise NotImplementedError

    def usage(self) -> StorageUsage:
        raise NotImplementedError

    def free_bytes(self) -> int | None:
        raise NotImplementedError
