"""HTTP `Range` on the worker's file server (Stage 04 PRD §6, §22: "fileserver.py
:8081, HMAC tokens, now with HTTP Range").

A browser `<video>` seeks by asking for byte ranges, and a server that answers
every range with the whole file makes a 20 MB master un-seekable. RFC 9110 §14
is the contract: one satisfiable range is a `206` with `Content-Range`, a range
that starts past the end is a `416` naming the size, and a `Range` header the
server does not understand is ignored — the whole file, `200`. Nothing about
ranges comes before the token: an unsigned range request is refused exactly
like an unsigned whole-file one.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
import uvicorn

from agent.config import Settings
from agent.export.tokens import sign
from agent.fileserver import CHUNK_BYTES, create_file_server
from agent.storage.local import LocalStorage

KEY = "creative/33333333-3333-4333-8333-333333333333/media/a/master.mp4"
#: Bigger than two chunks, and not a multiple of one, so a range can straddle them.
PAYLOAD = bytes(range(256)) * ((2 * CHUNK_BYTES + 12_345) // 256 + 1)
SIZE = len(PAYLOAD)


@pytest.fixture
def storage(tmp_path: Path) -> LocalStorage:
    backend = LocalStorage(str(tmp_path), "http://worker:8081")
    backend.put(KEY, PAYLOAD)
    return backend


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(storage_dir=str(tmp_path))


@pytest_asyncio.fixture
async def client(storage: LocalStorage, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    app = create_file_server(storage=storage, settings=settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://worker"
    ) as http:
        yield http


async def _get(
    client: httpx.AsyncClient, settings: Settings, range_header: str | None
) -> httpx.Response:
    headers = {"Range": range_header} if range_header is not None else {}
    return await client.get(
        f"/files/{KEY}", params={"token": sign(KEY, settings=settings)}, headers=headers
    )


async def test_the_whole_file_says_it_accepts_ranges(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    response = await _get(client, settings, None)
    assert response.status_code == 200
    assert response.headers["accept-ranges"] == "bytes"
    assert response.headers["content-length"] == str(SIZE)
    assert response.content == PAYLOAD


@pytest.mark.parametrize(
    ("header", "start", "end"),
    [
        ("bytes=0-1023", 0, 1023),
        (f"bytes={CHUNK_BYTES - 10}-{CHUNK_BYTES + 10}", CHUNK_BYTES - 10, CHUNK_BYTES + 10),
        ("bytes=1000-", 1000, SIZE - 1),
        ("bytes=-500", SIZE - 500, SIZE - 1),
        (f"bytes=10-{SIZE + 999}", 10, SIZE - 1),
        (f"bytes=-{SIZE + 5}", 0, SIZE - 1),
        (f"bytes={SIZE - 1}-{SIZE - 1}", SIZE - 1, SIZE - 1),
        ("BYTES=5-9", 5, 9),
    ],
    ids=[
        "first-kib",
        "straddles-a-chunk",
        "open-ended",
        "suffix",
        "end-past-the-file",
        "suffix-longer-than-the-file",
        "last-byte",
        "unit-is-case-insensitive",
    ],
)
async def test_one_satisfiable_range_is_a_206_of_exactly_those_bytes(
    client: httpx.AsyncClient, settings: Settings, header: str, start: int, end: int
) -> None:
    response = await _get(client, settings, header)
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes {start}-{end}/{SIZE}"
    assert response.headers["content-length"] == str(end - start + 1)
    assert response.headers["accept-ranges"] == "bytes"
    assert response.content == PAYLOAD[start : end + 1]


@pytest.mark.parametrize("header", [f"bytes={SIZE}-", f"bytes={SIZE + 10}-{SIZE + 20}", "bytes=-0"])
async def test_a_range_past_the_end_is_a_416_naming_the_size(
    client: httpx.AsyncClient, settings: Settings, header: str
) -> None:
    response = await _get(client, settings, header)
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{SIZE}"
    assert response.content != PAYLOAD


@pytest.mark.parametrize(
    "header",
    [
        "items=0-10",
        "bytes=abc-def",
        "bytes=20-10",
        "bytes=",
        "bytes=0-10,20-30",
        "bytes=--5",
        "bytes = 5 - 9",
    ],
    ids=[
        "other-unit",
        "not-numbers",
        "backwards",
        "empty",
        "multipart",
        "double-dash",
        "inner-whitespace",
    ],
)
async def test_a_range_it_does_not_serve_is_ignored_for_the_whole_file(
    client: httpx.AsyncClient, settings: Settings, header: str
) -> None:
    """RFC 9110 §14.2: a server MAY ignore Range. A multi-range answer is a
    `multipart/byteranges` body no `<video>` element asks for, so one is the whole
    file too — never a wrong single range."""
    response = await _get(client, settings, header)
    assert response.status_code == 200
    assert response.content == PAYLOAD


async def test_a_range_never_gets_past_the_token(client: httpx.AsyncClient) -> None:
    response = await client.get(f"/files/{KEY}", headers={"Range": "bytes=0-1023"})
    assert response.status_code == 403
    assert "content-range" not in response.headers


async def test_a_signed_range_for_a_missing_object_is_a_404(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    missing = "creative/nope/master.mp4"
    response = await client.get(
        f"/files/{missing}",
        params={"token": sign(missing, settings=settings)},
        headers={"Range": "bytes=0-1023"},
    )
    assert response.status_code == 404


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.mark.skipif(shutil.which("curl") is None, reason="curl is not installed")
async def test_curl_r_0_1023_over_a_real_socket_is_a_206(
    storage: LocalStorage, settings: Settings, tmp_path: Path
) -> None:
    """The phase's exit criterion, literally: `curl -r 0-1023` against the
    server uvicorn actually runs, not an in-process transport."""
    port = _free_port()
    server = uvicorn.Server(
        uvicorn.Config(
            create_file_server(storage=storage, settings=settings),
            host="127.0.0.1",
            port=port,
            log_level="warning",
        )
    )
    task = asyncio.create_task(server.serve())
    try:
        async with asyncio.timeout(10):
            # uvicorn exposes `started` as a flag, not an event.
            while not server.started:  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        out = tmp_path / "part.bin"
        url = f"http://127.0.0.1:{port}/files/{KEY}?token={sign(KEY, settings=settings)}"
        done = await asyncio.to_thread(
            subprocess.run,
            ["curl", "-s", "-r", "0-1023", "-o", str(out), "-w", "%{http_code}", url],
            capture_output=True,
            check=False,
            timeout=30,
        )
        assert done.returncode == 0, done.stderr
        assert done.stdout.decode() == "206"
        assert out.read_bytes() == PAYLOAD[:1024]
    finally:
        server.should_exit = True
        await task
