# ---------------------------------------------------------------------------
# api service — slim, no browser dependencies.
# Railway: Root Directory = apps/api, dockerfilePath = Dockerfile (railway.json)
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/

WORKDIR /app

# Dependencies first, so a source-only change does not re-resolve the world.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    PORT=8000

EXPOSE 8000

# Shell form on purpose: $PORT is injected by Railway at runtime and must be
# expanded. Bind `::` — Railway's private network is IPv6-only (PRD §5.2).
CMD uvicorn agent.main:app --host :: --port ${PORT:-8000}
