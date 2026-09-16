"""`GET /models` — the OpenRouter catalogue, proxied (PRD §14).

Proxied rather than called from the browser for the obvious reason: the key
never leaves the server. The catalogue is also the same for everyone, so it is
cached per process with a short TTL — a settings screen that is being fiddled
with should not issue an upstream request per keystroke.

Guarded by `settings_write` because the one screen that consumes it, the
wizard's model-routing step, is admin-only (PRD §13.4: "Step 3 … and the
credential fields in step 2 are admin-only"). An operator's read-only view of
that step renders the ids already stored on the project and needs no catalogue.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Annotated

import httpx
import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from agent.api import problems
from agent.api.schemas_models import ModelListResponse, ModelOption, TaskClassRouting
from agent.auth.deps import Principal, require
from agent.auth.rbac import Permission
from agent.config import get_settings
from agent.credentials import MissingCredential, resolve_secret
from agent.db.models import CredentialKind
from agent.db.session import get_session
from agent.llm.estimate import usage_baseline
from agent.llm.openrouter import ModelInfo, OpenRouterError, list_models
from agent.llm.router import SEED_MODELS, TaskClass

log = structlog.get_logger(__name__)

router = APIRouter(tags=["models"])

Db = Annotated[AsyncSession, Depends(get_session)]
SettingsWriter = Annotated[Principal, Depends(require(Permission.SETTINGS_WRITE))]

CATALOGUE_TTL = timedelta(minutes=10)

#: What each task class is for, in the words the picker shows next to it.
TASK_CLASS_COPY: dict[TaskClass, tuple[str, str]] = {
    TaskClass.EXTRACT: (
        "Extract",
        "Pulls structure out of evidence. High volume, long context, no judgement.",
    ),
    TaskClass.CLASSIFY: (
        "Classify",
        "Labels things against a fixed rubric. Fast, cheap, deterministic.",
    ),
    TaskClass.SYNTHESIZE: (
        "Synthesize",
        "Writes the report. This is the one worth paying for.",
    ),
    TaskClass.CRITIQUE: (
        "Critique",
        "Reviews the report adversarially — deliberately a different vendor.",
    ),
}

_cache: tuple[datetime, list[ModelInfo]] | None = None
_lock = asyncio.Lock()


@router.get("/models", response_model=ModelListResponse, summary="The OpenRouter catalogue")
async def get_models(
    me: SettingsWriter,
    db: Db,
    refresh: Annotated[bool, Query(description="Bypass the cache")] = False,
) -> ModelListResponse:
    settings = get_settings()
    try:
        api_key = await resolve_secret(
            db,
            workspace_id=me.workspace_id,
            kind=CredentialKind.OPENROUTER,
            user_id=me.user.id,
        )
    except MissingCredential as exc:
        raise problems.conflict(
            str(exc),
            title="No OpenRouter key",
            missing_credential=CredentialKind.OPENROUTER.value,
        ) from exc

    try:
        models = await _catalogue(settings.openrouter_base_url, api_key, refresh=refresh)
    except OpenRouterError as exc:
        raise problems.Problem(
            status_code=502,
            title="OpenRouter unavailable",
            detail=exc.detail,
        ) from exc

    baseline = await usage_baseline(db, me.workspace_id)
    return ModelListResponse(
        models=[
            ModelOption(
                id=model.id,
                name=model.name,
                context_length=model.context_length,
                prompt_per_million=model.prompt_per_million,
                completion_per_million=model.completion_per_million,
                supports_structured_output=model.supports_structured_output,
            )
            for model in models
        ],
        task_classes=[
            TaskClassRouting(
                task_class=usage.task_class,
                label=TASK_CLASS_COPY[usage.task_class][0],
                purpose=TASK_CLASS_COPY[usage.task_class][1],
                default_model=SEED_MODELS[usage.task_class][0],
                fallbacks=list(SEED_MODELS[usage.task_class][1:]),
                calls_per_run=usage.calls,
                token_in_per_run=usage.token_in,
                token_out_per_run=usage.token_out,
                usage_source=usage.source,
            )
            for usage in baseline
        ],
        usage_source="measured"
        if all(usage.source == "measured" for usage in baseline)
        else "assumed",
    )


async def _catalogue(base_url: str, api_key: str, *, refresh: bool) -> list[ModelInfo]:
    """The catalogue, fetched at most once per TTL per process.

    The lock matters more than the TTL: without it, four browsers opening the
    settings screen at once each start their own upstream fetch and the last one
    wins, having achieved nothing the first would not have.
    """
    global _cache
    async with _lock:
        cached = _cache
        if not refresh and cached is not None and datetime.now(UTC) - cached[0] < CATALOGUE_TTL:
            return cached[1]
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as client:
            models = await list_models(client, base_url=base_url, api_key=api_key)
        _cache = (datetime.now(UTC), models)
        return models


def reset_cache() -> None:
    """Drop the cached catalogue. For tests, which must not inherit one."""
    global _cache
    _cache = None
