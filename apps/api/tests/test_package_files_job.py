"""`worker.write_package_files` — release's file step (Stage 04 PRD §12.4)."""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterator
from pathlib import Path

import pytest

from agent import worker
from agent.storage.local import LocalStorage

pytestmark = pytest.mark.asyncio

PACKAGE = uuid.UUID(int=9000)


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[LocalStorage]:
    from agent.config import get_settings

    monkeypatch.setenv("STORAGE_DIR", str(tmp_path))
    get_settings.cache_clear()
    yield LocalStorage(str(tmp_path))
    get_settings.cache_clear()


async def test_copies_and_writes_under_the_package_and_hashes_what_landed(
    storage: LocalStorage,
) -> None:
    storage.put("creative/run/renditions/a/1x1.jpg", b"jpeg-bytes", content_type="image/jpeg")
    receipt = await worker.write_package_files(
        {},
        {
            "package_id": str(PACKAGE),
            "files": [
                {
                    "path": "media/a/m.jpg",
                    "source_key": "creative/run/renditions/a/1x1.jpg",
                    "media_type": "image/jpeg",
                },
                {
                    "path": "landing/x/patch.html",
                    "content": b"<h1>x</h1>",
                    "media_type": "text/html",
                },
            ],  # fmt: skip
        },
    )
    by_path = {item["path"]: item for item in receipt["files"]}
    assert storage.get(f"package/{PACKAGE}/media/a/m.jpg") == b"jpeg-bytes"
    assert by_path["media/a/m.jpg"]["sha256"] == hashlib.sha256(b"jpeg-bytes").hexdigest()
    assert by_path["landing/x/patch.html"]["bytes"] == len(b"<h1>x</h1>")


@pytest.mark.parametrize(
    "item",
    [
        {"path": "media/../../etc/passwd", "content": b"x"},
        {"path": "secrets/key", "content": b"x"},
        {"path": "media/a/b.jpg", "source_key": "exports/other/report.pdf"},
        {"path": "media/a/b.jpg"},
    ],
)
async def test_refuses_anything_but_package_files_from_the_run(
    storage: LocalStorage, item: dict[str, object]
) -> None:
    with pytest.raises(ValueError):
        await worker.write_package_files({}, {"package_id": str(PACKAGE), "files": [item]})
    assert not storage.exists(f"package/{PACKAGE}/media/a/b.jpg")
