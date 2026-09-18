"""The worker's internal file server (PRD §5.1, §12).

Railway attaches a Volume to exactly one service, and that service is `worker`
(PRD §5.2). `api` therefore cannot read an export off disk — there is no disk to
read. It streams the bytes from here, over the private network, and relays them
to the browser.

This is why the worker has an HTTP port at all despite having no public ingress.
It is not a second API: two routes, no database, no sessions, and every read
authorised by a signed capability for one storage key (`export/tokens.py`).
Trusting the private network instead would let anything that resolves
`worker.railway.internal` read any object under the Volume by guessing a key.

The server runs inside the arq worker process, started from `on_startup` and
stopped on `on_shutdown`, so there is one process, one lifecycle and one place
that owns the Volume.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Iterator
from typing import IO, Any

import structlog
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

from agent.config import Settings, get_settings
from agent.export.tokens import TokenError, verify
from agent.storage.backend import StorageBackend, StorageError, get_storage

log = structlog.get_logger(__name__)

#: Streamed in 64 KiB pieces. A 15 MB PDF read whole would sit in the worker's
#: memory for as long as the slowest client takes to accept it.
CHUNK_BYTES = 64 * 1024


async def _iter_file(handle: IO[bytes]) -> AsyncIterator[bytes]:
    """Read a sync file object without blocking the loop the worker runs jobs on."""
    try:
        while True:
            chunk = await asyncio.to_thread(handle.read, CHUNK_BYTES)
            if not chunk:
                return
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


def create_file_server(
    *,
    storage: StorageBackend | None = None,
    settings: Settings | None = None,
) -> Starlette:
    """The ASGI app. Built as a factory so tests can drive it over ASGITransport."""
    resolved_settings = settings or get_settings()
    backend = storage or get_storage(resolved_settings)

    async def health(_: Request) -> Response:
        """Liveness for a private-network smoke test. Not a Railway healthcheck —
        PRD §5.2 gives the worker none, because a failing arq worker should
        restart on its own policy rather than be replaced mid-job."""
        return JSONResponse({"status": "ok", "service": "fileserver"})

    async def usage(_: Request) -> Response:
        """Totals for the storage banner (PRD §16, "Volume full").

        Unauthenticated, like `/health`, and for the same reason: the worker has
        no public ingress (§15 NF8d) and this answers with four integers. It
        names no key and lists no object, so it is not the directory oracle the
        download route goes out of its way not to become.
        """
        totals = backend.usage()
        return JSONResponse(
            {
                "objects": totals.objects,
                "bytes": totals.bytes,
                "capacity_bytes": totals.capacity_bytes,
                "used_fraction": totals.used_fraction,
            }
        )

    async def download(request: Request) -> Response:
        key = request.path_params["key"]
        token = request.query_params.get("token", "")

        try:
            verify(key, token, settings=resolved_settings)
        except TokenError as exc:
            # Never distinguish "bad token" from "no such file": answering that
            # question for an unsigned guess turns this into a directory oracle
            # over the Volume.
            log.warning("fileserver.denied", key=key, reason=str(exc))
            return JSONResponse({"detail": "Not authorised for this object."}, status_code=403)

        try:
            handle = backend.open(key)
        except StorageError as exc:
            log.warning("fileserver.missing", key=key, error=str(exc))
            return JSONResponse({"detail": "No such object."}, status_code=404)

        log.info("fileserver.served", key=key)
        return StreamingResponse(
            _iter_file(handle),
            media_type="application/octet-stream",
            # The filename a user sees is decided by `api` on the public hop;
            # this one is a byte pipe and says nothing about presentation.
            headers={"Cache-Control": "no-store"},
        )

    return Starlette(
        routes=[
            Route("/health", health, methods=["GET"]),
            Route("/usage", usage, methods=["GET"]),
            Route("/files/{key:path}", download, methods=["GET"]),
        ]
    )


class _SignalSafeServer(uvicorn.Server):
    """A uvicorn server that keeps its hands off the process's signals.

    uvicorn wraps `serve()` in `capture_signals()`, which — in the main thread,
    which is where arq runs — replaces the SIGINT and SIGTERM handlers with its
    own for the lifetime of the server. arq installs those handlers to drain the
    job it is running before exiting, so leaving this in place means a `docker
    stop` shuts the file server down cleanly and kills a 40-minute run.

    (Older uvicorn spelled this `install_signal_handlers`; 0.53 renamed it to
    this context manager. Overriding the wrong one fails silently — the server
    runs, and the handlers are quietly stolen.)
    """

    @contextlib.contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


class FileServer:
    """The file server's lifecycle, owned by the arq worker process."""

    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[Any] | None = None

    async def start(self) -> None:
        config = uvicorn.Config(
            create_file_server(settings=self._settings),
            # `0.0.0.0`, not `::` — the same rule the api service follows, and
            # for the same reason the comment here used to get wrong: asyncio
            # sets IPV6_V6ONLY on every AF_INET6 socket, so binding `::` does
            # NOT also accept IPv4. Railway environments created after
            # 2025-10-16 resolve `*.railway.internal` to both an A and an AAAA
            # record, so an IPv4 listener is reachable from `api`; an IPv6-only
            # one is reachable only if the client happens to pick the AAAA.
            # noqa justification: `worker` has no public domain and no published
            # port — this listener is reachable only from inside the project's
            # private network, which is the whole point of it.
            host="0.0.0.0",  # noqa: S104
            port=self._settings.file_server_port,
            log_level=self._settings.log_level.lower(),
            access_log=False,
        )
        self._server = _SignalSafeServer(config)
        self._task = asyncio.create_task(self._server.serve(), name="fileserver")
        log.info("fileserver.started", port=self._settings.file_server_port)

    async def stop(self) -> None:
        if self._server is None or self._task is None:
            return
        self._server.should_exit = True
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except TimeoutError:
            log.warning("fileserver.stop_timeout")
            self._task.cancel()
        finally:
            self._server = None
            self._task = None
            log.info("fileserver.stopped")
