"""`MediaJobs` as the worker builds it: one OpenRouter account, one pool.

The key is the workspace's OpenRouter connection (CR-E7), resolved the way
the executor resolves the text gateway's — never an environment read here, so
a workspace that disconnects OpenRouter stops spending on the next job rather
than on the next deploy.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from agent.config import get_settings
from agent.credentials import resolve_values
from agent.db.models import CredentialKind, GenerationJob, Run
from agent.db.session import get_sessionmaker
from agent.media.constants import media_constants
from agent.media.http import MediaApi
from agent.media.images import ImageClient
from agent.media.jobs import MediaJobs
from agent.media.videos import VideoClient
from agent.redis_client import get_redis
from agent.schemas.creative_input import CreativeInput, MediaModelChoice
from agent.storage.backend import get_storage

#: Longer than an image generation takes; a video POST returns at once.
TIMEOUT = httpx.Timeout(300.0, connect=10.0)


async def media_jobs(
    db: AsyncSession, *, workspace_id: Any, settings: Any = None
) -> tuple[MediaJobs, httpx.AsyncClient]:
    """The job layer for one workspace, and the client the caller closes."""
    settings = settings or get_settings()
    values = await resolve_values(db, workspace_id=workspace_id, kind=CredentialKind.OPENROUTER)
    client = httpx.AsyncClient(timeout=TIMEOUT)
    api = MediaApi(
        client=client,
        api_key=values["api_key"],
        base_url=settings.openrouter_base_url,
        referer=settings.app_base_url,
    )
    jobs = MediaJobs(
        sessionmaker=get_sessionmaker(),
        redis=get_redis(),
        images=ImageClient(api),
        videos=VideoClient(api),
        storage=get_storage(settings),
        constants=media_constants(),
        defaults=settings,
    )
    return jobs, client


def pinned_choices(run: Run | None) -> list[MediaModelChoice]:
    """The media models the run was started with (`Run.creative_input`)."""
    if run is None or not run.creative_input:
        return []
    return list(CreativeInput.model_validate(run.creative_input).media_models)


def choice_for(choices: Sequence[MediaModelChoice], row: GenerationJob) -> MediaModelChoice | None:
    """The run's own pinned choice for the model this job was made with.

    Read from what the run was started with, so a re-submit is validated
    against the capability record the job was priced on — never whatever the
    allowlist says today (Law 36).
    """
    for choice in choices:
        if choice.modality == row.modality.value and choice.model_id == row.model_id:
            return choice
    return None
