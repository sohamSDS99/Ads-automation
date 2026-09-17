"""Reading the worker's Volume from `api` (PRD §5.2, §12).

Railway attaches a Volume to exactly one service, and that service is `worker`.
Anything `api` needs to hand a browser — an export, a competitor screenshot —
lives on a disk this process cannot see, so it mints a signed capability for the
one storage key, streams the object off the worker's internal file server, and
relays the bytes.

This module holds the parts both consumers share. It was extracted from
`routes_reports` when the Evidence Explorer needed the same hop for creative
screenshots; a second hand-rolled proxy is how one of them ends up without the
`502`-before-the-body rule below.
"""

from __future__ import annotations

from typing import Annotated
from urllib.parse import quote

import httpx
import structlog
from fastapi import Depends, status

from agent.api import problems
from agent.config import Settings, get_settings
from agent.export.tokens import sign

log = structlog.get_logger(__name__)

#: How long `api` waits on the worker. Generous, because the hop is a file
#: stream over a private network and the worker may be mid-render on another
#: job; short enough that a wedged worker does not hold the connection open
#: indefinitely.
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=5.0, read=60.0, write=10.0, pool=5.0)

_worker_client: httpx.AsyncClient | None = None


async def get_worker_client() -> httpx.AsyncClient:
    """The client `api` uses to reach the worker's file server.

    One client for the process, opened on first use and closed in the app
    lifespan — the same shape as the arq pool and the Redis client, and for the
    same reason: connection reuse to one known host.

    It is deliberately **not** a `yield` dependency. FastAPI finalises those once
    the response is handed off, which for a StreamingResponse is before the body
    has finished streaming; the client would be closed out from under the
    download. Its lifetime belongs to the process, not the request.

    It is a dependency at all so the integration suite can override it with a
    client bound to the file-server ASGI app, and exercise the real token check
    without a second process listening on a port.
    """
    global _worker_client
    if _worker_client is None:
        _worker_client = httpx.AsyncClient(timeout=DOWNLOAD_TIMEOUT)
    return _worker_client


async def close_worker_client() -> None:
    global _worker_client
    if _worker_client is not None:
        await _worker_client.aclose()
        _worker_client = None


WorkerClient = Annotated[httpx.AsyncClient, Depends(get_worker_client)]


def signed_url(key: str, *, settings: Settings | None = None) -> str:
    """The worker URL that will serve `key`, carrying a short-lived capability.

    The key is quoted whole, `/` included: storage keys are nested paths and the
    file server matches one path parameter, so an unquoted separator would route
    to something else entirely.
    """
    settings = settings or get_settings()
    token = sign(key, settings=settings)
    return f"{settings.worker_internal_url.rstrip('/')}/files/{quote(key)}?token={token}"


async def open_upstream(
    client: httpx.AsyncClient,
    url: str,
    *,
    subject: str,
    **log_context: str,
) -> httpx.Response:
    """Begin the worker fetch and prove it succeeded before we answer the caller.

    The status is checked here, with nothing yet written to the client, so a
    worker that cannot serve the object produces a clean 502. Checking it inside
    the streaming generator instead would mean the 200 and its headers had
    already gone out, and the only remaining signal would be an aborted
    connection — which most clients save to disk as a truncated file.
    """
    title = f"{subject.capitalize()} could not be read"
    try:
        upstream = await client.send(client.build_request("GET", url), stream=True)
    except httpx.HTTPError as exc:
        log.error("worker_file.unreachable", subject=subject, error=str(exc), **log_context)
        raise problems.Problem(
            status_code=status.HTTP_502_BAD_GATEWAY,
            title=title,
            detail=f"The worker that stores this {subject} is unreachable.",
        ) from exc

    if upstream.status_code != httpx.codes.OK:
        await upstream.aread()
        await upstream.aclose()
        log.error(
            "worker_file.upstream_failed",
            subject=subject,
            status=upstream.status_code,
            **log_context,
        )
        raise problems.Problem(
            status_code=status.HTTP_502_BAD_GATEWAY,
            title=title,
            detail=f"The worker did not return this {subject}.",
        )
    return upstream
