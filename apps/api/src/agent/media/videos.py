"""`POST /api/v1/videos` → poll → download (PRD §8.4, §9.1 item 4, §23.1 item 5).

**Submit is one POST, never retried.** Video is billed per job: a 5xx or a
dropped connection after the POST may already have created — and billed — a
job, so the only safe automatic behaviour is to report `ProviderUnavailable`
and let the job layer mark the row `unknown_submit_state` for a human (§18).
No `callback_url` in v1: polling keeps `api` free of a public webhook route.

**`unsigned_urls` never leave this module.** They are not presigned — they
need the key — so the download happens here, in the worker, with the
Authorization header, and `Poll` keeps only how many there were. The download
URL is rebuilt from the job id and the index rather than read back from the
poll body, and httpx's own request log line for it is filtered out: at the
INFO level `configure_logging` sets, httpx logs every request line, and for
this route the request line *is* the unsigned URL.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

import httpx
import structlog

from agent.media.capability import CapabilityUnsupported, validate
from agent.media.http import (
    JobNotFound,
    MediaApi,
    ProviderRejected,
    ProviderUnavailable,
    response_body,
)
from agent.media.images import data_url_part
from agent.media.types import CapabilityRecord, StoredMedia, VideoRequest
from agent.storage.backend import StorageBackend

log = structlog.get_logger(__name__)

VideoStatus = Literal["pending", "in_progress", "completed", "failed", "cancelled", "expired"]
TERMINAL: frozenset[str] = frozenset({"completed", "failed", "cancelled", "expired"})
_STATUSES: frozenset[str] = TERMINAL | {"pending", "in_progress"}


class _ContentUrlFilter(logging.Filter):
    """Drop httpx's request line for `/videos/{id}/content` — the unsigned URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ("/videos/" in message and "/content" in message)


def _install_log_filter() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(existing, _ContentUrlFilter) for existing in logger.filters):
        logger.addFilter(_ContentUrlFilter())


_install_log_filter()


@dataclass(frozen=True, slots=True)
class Submitted:
    id: str
    polling_url: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class Poll:
    """One `GET /api/v1/videos/{id}`: the state, never the URLs."""

    status: VideoStatus
    #: `len(unsigned_urls)`: how many outputs `download(index)` may fetch.
    outputs: int = 0
    cost_usd: Decimal | None = None
    #: Verbatim, for failed / cancelled / expired.
    error: str | None = None
    generation_id: str | None = None

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL


class VideoClient:
    def __init__(self, api: MediaApi) -> None:
        self._api = api

    async def submit(self, request: VideoRequest, *, capability: CapabilityRecord) -> Submitted:
        """Validate, then POST exactly once. Raises `CapabilityUnsupported`
        (nothing sent), `ProviderRejected` (4xx: no job exists) or
        `ProviderUnavailable` (a job may or may not exist)."""
        errors = validate(request, capability)
        if errors:
            raise CapabilityUnsupported(errors)
        try:
            response = await self._api.client.post(
                self._api.url("/videos"), json=wire_video(request), headers=self._api.headers()
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(
                f"The video submit did not complete ({type(exc).__name__}); OpenRouter may "
                "or may not have created the job."
            ) from exc
        if response.status_code >= 500:
            raise ProviderUnavailable(
                f"OpenRouter answered {response.status_code} to the video submit; it may or "
                "may not have created the job."
            )
        if response.status_code >= 400:
            raise ProviderRejected(response.status_code, response_body(response))
        try:
            payload = response.json()
            submitted = Submitted(id=str(payload["id"]), polling_url=str(payload["polling_url"]))
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                "OpenRouter accepted the video submit but its answer cannot be read; the job "
                "exists and its id is unknown."
            ) from exc
        log.info("media.video_submitted", model=request.model, openrouter_job_id=submitted.id)
        return submitted

    async def poll(self, job_id: str) -> Poll:
        try:
            response = await self._api.client.get(
                self._api.url(f"/videos/{job_id}"), headers=self._api.headers()
            )
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"The video poll failed ({type(exc).__name__}).") from exc
        _raise_for_status(response, what="poll")
        try:
            payload = response.json()
            status = str(payload["status"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderUnavailable(
                "OpenRouter returned a poll body that cannot be read."
            ) from exc
        if status not in _STATUSES:
            raise ProviderUnavailable(f"OpenRouter reported an unknown video status {status!r}.")
        usage = payload.get("usage") or {}
        error = payload.get("error")
        return Poll(
            status=status,  # type: ignore[arg-type]
            outputs=len(payload.get("unsigned_urls") or []),
            cost_usd=_money(usage.get("cost")),
            error=None if error is None else str(error),
            generation_id=payload.get("generation_id"),
        )

    async def download(
        self, job_id: str, index: int, *, storage: StorageBackend, key: str
    ) -> StoredMedia:
        """Stream output `index` of a completed job into `storage` at `key`."""
        buffer = io.BytesIO()
        digest = hashlib.sha256()
        try:
            async with self._api.client.stream(
                "GET",
                self._api.url(f"/videos/{job_id}/content"),
                params={"index": index},
                headers=self._api.headers(),
            ) as response:
                if response.status_code >= 400:
                    await response.aread()
                    _raise_for_status(response, what="download")
                media_type = response.headers.get("content-type", "video/mp4").split(";")[0]
                async for chunk in response.aiter_bytes():
                    buffer.write(chunk)
                    digest.update(chunk)
        except httpx.HTTPError as exc:
            raise ProviderUnavailable(f"The video download failed ({type(exc).__name__}).") from exc
        size = buffer.tell()
        if size == 0:
            raise ProviderUnavailable("OpenRouter returned an empty video.")
        buffer.seek(0)
        await asyncio.to_thread(storage.put, key, buffer, content_type=media_type)
        log.info("media.video_downloaded", openrouter_job_id=job_id, index=index, bytes=size)
        return StoredMedia(key=key, bytes=size, sha256=digest.hexdigest(), media_type=media_type)


def wire_video(request: VideoRequest) -> dict[str, Any]:
    """The JSON body: every set field; frame images as `ContentPartImage` plus
    their `frame_type`, bytes inline as data URLs."""
    payload = request.model_dump(
        mode="json", exclude_none=True, exclude={"frame_images", "input_references"}
    )
    if request.frame_images:
        payload["frame_images"] = [
            {**data_url_part(frame.image), "frame_type": frame.frame_type}
            for frame in request.frame_images
        ]
    if request.input_references:
        payload["input_references"] = [data_url_part(ref) for ref in request.input_references]
    return payload


def _raise_for_status(response: httpx.Response, *, what: str) -> None:
    if response.status_code == 404:
        raise JobNotFound(404, response_body(response))
    if response.status_code >= 500:
        raise ProviderUnavailable(
            f"OpenRouter answered {response.status_code} to the video {what}."
        )
    if response.status_code >= 400:
        raise ProviderRejected(response.status_code, response_body(response))


def _money(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
