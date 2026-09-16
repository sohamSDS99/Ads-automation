"""The worker's internal file server (PRD §5.1, §12).

It serves the Volume to the one caller that cannot read it directly. Since it
listens on the private network with no session in front of it, "does the token
check actually run" is the only thing about it that really matters, and most of
these are about the ways it could stop running.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from agent.config import Settings
from agent.export.tokens import sign
from agent.fileserver import create_file_server
from agent.storage.local import LocalStorage

KEY = "exports/22222222-2222-4222-8222-222222222222/report.pdf"
PAYLOAD = b"%PDF-1.7\n" + b"x" * 200_000


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


async def test_a_signed_request_gets_the_bytes(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    response = await client.get(f"/files/{KEY}", params={"token": sign(KEY, settings=settings)})
    assert response.status_code == 200
    assert response.content == PAYLOAD


async def test_an_unsigned_request_is_refused(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"/files/{KEY}")).status_code == 403


async def test_a_token_for_another_object_is_refused(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    other = sign("exports/somebody-elses-run/report.pdf", settings=settings)
    assert (await client.get(f"/files/{KEY}", params={"token": other})).status_code == 403


async def test_an_expired_token_is_refused(client: httpx.AsyncClient, settings: Settings) -> None:
    stale = sign(KEY, ttl_seconds=-1, settings=settings)
    assert (await client.get(f"/files/{KEY}", params={"token": stale})).status_code == 403


async def test_a_signed_request_for_a_missing_object_is_a_404(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    """404 only *after* the signature checks out — see the test below."""
    absent = "exports/run/does-not-exist.pdf"
    response = await client.get(
        f"/files/{absent}", params={"token": sign(absent, settings=settings)}
    )
    assert response.status_code == 404


async def test_an_unsigned_probe_cannot_tell_present_from_absent(
    client: httpx.AsyncClient,
) -> None:
    """Otherwise this is a directory listing of the Volume, one guess at a time."""
    present = await client.get(f"/files/{KEY}")
    absent = await client.get("/files/exports/run/does-not-exist.pdf")
    assert present.status_code == absent.status_code == 403
    assert present.json() == absent.json()


async def test_a_traversal_key_is_refused_even_when_signed(
    client: httpx.AsyncClient, settings: Settings
) -> None:
    """The storage backend refuses it; the signature does not make it safe."""
    escape = "exports/../../etc/passwd"
    response = await client.get(
        f"/files/{escape}", params={"token": sign(escape, settings=settings)}
    )
    assert response.status_code in {403, 404}
    assert b"root:" not in response.content


async def test_large_objects_stream_rather_than_buffer(
    storage: LocalStorage, settings: Settings
) -> None:
    """A 15 MB PDF must not sit in the worker's memory for the slowest client.

    Driven against the ASGI app rather than through httpx: httpx re-chunks the
    body on the client side, so counting chunks there measures httpx's buffer
    and not whether the server streamed anything. Counting `http.response.body`
    messages measures what the server actually sent.
    """
    from agent.fileserver import CHUNK_BYTES

    app = create_file_server(storage=storage, settings=settings)
    token = sign(KEY, settings=settings)
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": f"/files/{KEY}",
        "raw_path": f"/files/{KEY}".encode(),
        "query_string": f"token={token}".encode(),
        "headers": [(b"host", b"worker")],
        "client": ("::1", 12345),
        "server": ("worker", 8081),
        "root_path": "",
    }

    sent: list[dict[str, object]] = []
    # StreamingResponse watches `receive` for a disconnect while it sends. A
    # receive that returns instantly every time is a busy loop that never yields
    # to the event loop, and the response never gets a turn to stream — the test
    # hangs rather than fails. A connected client is one whose receive blocks.
    connected = asyncio.Event()

    async def receive() -> dict[str, object]:
        await connected.wait()
        return {"type": "http.disconnect"}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    await app(scope, receive, send)

    start = next(message for message in sent if message["type"] == "http.response.start")
    assert start["status"] == 200

    bodies = [message["body"] for message in sent if message["type"] == "http.response.body"]
    assert b"".join(body for body in bodies if isinstance(body, bytes)) == PAYLOAD
    assert len(PAYLOAD) > CHUNK_BYTES, "the fixture is too small to prove anything"
    assert len([body for body in bodies if body]) > 1, "the object was sent in one piece"


async def test_health_answers_without_a_token(client: httpx.AsyncClient) -> None:
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json()["service"] == "fileserver"


def test_the_server_leaves_the_processes_signals_alone() -> None:
    """arq drains a running job on SIGTERM; uvicorn must not take that over.

    The override has to be the method uvicorn actually calls. uvicorn renamed
    this once already (`install_signal_handlers` → `capture_signals`), and
    overriding the name it no longer calls fails silently: the server runs, and
    the handlers are stolen anyway. So this asserts against uvicorn's own class,
    not against ours.
    """
    import inspect

    import uvicorn

    from agent.fileserver import _SignalSafeServer

    assert hasattr(uvicorn.Server, "capture_signals"), (
        "uvicorn renamed its signal hook again — _SignalSafeServer now overrides nothing"
    )
    assert _SignalSafeServer.capture_signals is not uvicorn.Server.capture_signals

    source = inspect.getsource(_SignalSafeServer.capture_signals)
    assert "signal.signal" not in source, "the override installs handlers of its own"
