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
import io
import re
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

#: One byte range, RFC 9110 §14.1.2: `first-last`, `first-` or `-suffix`.
_BYTE_RANGE = re.compile(r"(\d*)-(\d*)")

#: The media the Media Library shows straight from here (Stage 04 PRD §16:
#: `GET /media/{id}/content` redirects to this server). An `<img>` or a
#: `<video>` reads its type from the response — Safari will not play an
#: `application/octet-stream` video at all — so a known media suffix is served
#: as what it is. Everything else (exports, uploads) stays a byte pipe.
MEDIA_TYPES: dict[str, str] = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".mp4": "video/mp4",
}


def media_type_for(key: str) -> str:
    """The type a stored key is served as: its media type, or octet-stream."""
    name = key.rsplit("/", 1)[-1].lower()
    suffix = name[name.rfind(".") :] if "." in name else ""
    return MEDIA_TYPES.get(suffix, "application/octet-stream")


class Unsatisfiable(ValueError):
    """A valid range that selects no byte of the file — a `416`."""


async def _iter_file(handle: IO[bytes], length: int | None = None) -> AsyncIterator[bytes]:
    """Read a sync file object without blocking the loop the worker runs jobs on
    — to its end, or `length` bytes from where it is positioned."""
    remaining = length
    try:
        while remaining is None or remaining > 0:
            size = CHUNK_BYTES if remaining is None else min(CHUNK_BYTES, remaining)
            chunk = await asyncio.to_thread(handle.read, size)
            if not chunk:
                return
            if remaining is not None:
                remaining -= len(chunk)
            yield chunk
    finally:
        await asyncio.to_thread(handle.close)


def byte_range(header: str | None, size: int) -> tuple[int, int] | None:
    """The one range `header` asks for, as inclusive `(first, last)` clamped to
    a `size`-byte file (RFC 9110 §14.1.2, §14.2).

    None when the header is absent or is one this server ignores — another
    unit, bad syntax, `last < first`, or several ranges: a `multipart/byteranges`
    answer is not what a `<video>` element asks for, and the RFC lets a server
    serve the whole file instead. Raises `Unsatisfiable` when the range is
    valid but starts past the end, or is a zero-length suffix.
    """
    if header is None:
        return None
    unit, _, spec = header.strip().partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return None
    match = _BYTE_RANGE.fullmatch(spec.strip())
    if match is None or match.group(0) == "-":
        return None
    first, last = match.group(1), match.group(2)
    if not first:  # `-suffix`: the last `suffix` bytes
        suffix = int(last)
        if suffix == 0 or size == 0:
            raise Unsatisfiable(f"a {suffix}-byte suffix of a {size}-byte file")
        return max(0, size - suffix), size - 1
    start = int(first)
    end = int(last) if last else size - 1
    if last and end < start:
        return None
    if start >= size:
        raise Unsatisfiable(f"byte {start} of a {size}-byte file")
    return start, min(end, size - 1)


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

        # The filename a user sees is decided by `api` on the public hop; this
        # one is a byte pipe and says nothing about presentation.
        headers = {"Cache-Control": "no-store", "Accept-Ranges": "bytes"}
        size = await asyncio.to_thread(handle.seek, 0, io.SEEK_END)
        try:
            wanted = byte_range(request.headers.get("range"), size)
        except Unsatisfiable as exc:
            await asyncio.to_thread(handle.close)
            log.info("fileserver.unsatisfiable", key=key, reason=str(exc))
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{size}"}
            )
        if wanted is None:
            await asyncio.to_thread(handle.seek, 0)
            log.info("fileserver.served", key=key)
            return StreamingResponse(
                _iter_file(handle),
                media_type=media_type_for(key),
                headers={**headers, "Content-Length": str(size)},
            )
        # A `<video>` element seeks by asking for byte ranges (PRD §6, §22).
        start, end = wanted
        await asyncio.to_thread(handle.seek, start)
        log.info("fileserver.served_range", key=key, start=start, end=end)
        return StreamingResponse(
            _iter_file(handle, end - start + 1),
            status_code=206,
            media_type=media_type_for(key),
            headers={
                **headers,
                "Content-Range": f"bytes {start}-{end}/{size}",
                "Content-Length": str(end - start + 1),
            },
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
