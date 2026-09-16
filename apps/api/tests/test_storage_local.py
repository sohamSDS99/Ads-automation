"""LocalStorage writes inside STORAGE_DIR and refuses to write anywhere else."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.storage.backend import StorageError, get_storage
from agent.storage.local import LocalStorage
from agent.storage.s3 import S3Storage


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    return LocalStorage(str(tmp_path), "http://worker:8081")


def test_put_get_round_trip(storage: LocalStorage) -> None:
    storage.put("exports/report.pdf", b"%PDF-1.7")
    assert storage.get("exports/report.pdf") == b"%PDF-1.7"
    assert storage.exists("exports/report.pdf")


def test_put_accepts_a_file_object(storage: LocalStorage, tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"streamed")
    with source.open("rb") as handle:
        storage.put("copied.bin", handle)
    assert storage.get("copied.bin") == b"streamed"


def test_open_streams(storage: LocalStorage) -> None:
    storage.put("a/b/c.txt", b"hello")
    with storage.open("a/b/c.txt") as handle:
        assert handle.read() == b"hello"


def test_delete_is_idempotent(storage: LocalStorage) -> None:
    storage.put("gone.txt", b"x")
    storage.delete("gone.txt")
    storage.delete("gone.txt")
    assert not storage.exists("gone.txt")


def test_missing_key_raises(storage: LocalStorage) -> None:
    with pytest.raises(StorageError):
        storage.get("nope.txt")
    with pytest.raises(StorageError):
        storage.open("nope.txt")


@pytest.mark.parametrize(
    "key",
    [
        "../escape.txt",
        "a/../../escape.txt",
        "/etc/passwd",
        "a//b.txt",
        "./sneaky.txt",
        "",
        " leading-space.txt",
    ],
)
def test_traversal_and_malformed_keys_are_refused(storage: LocalStorage, key: str) -> None:
    with pytest.raises(StorageError):
        storage.put(key, b"x")


def test_nothing_is_written_outside_the_root(storage: LocalStorage, tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    with pytest.raises(StorageError):
        storage.put(f"../{outside.name}", b"x")
    assert not outside.exists()


def test_url_for_points_at_the_worker_file_server(storage: LocalStorage) -> None:
    storage.put("exports/r.pdf", b"x")
    assert storage.url_for("exports/r.pdf") == "http://worker:8081/files/exports/r.pdf"


def test_get_storage_returns_local_by_default() -> None:
    assert isinstance(get_storage(), LocalStorage)


def test_s3_backend_is_an_explicit_stub() -> None:
    with pytest.raises(NotImplementedError):
        S3Storage()
