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

# Bake the embedding model into the image.
#
# `fastembed` downloads BAAI/bge-small-en-v1.5 (~130MB) on first use. Container
# filesystems are ephemeral (PRD §5.2 / Law 10), so without this every deploy
# would re-download it on the first request that touches search — a cold start
# measured in tens of seconds, and an outage if HuggingFace is unreachable.
# Downloading it at build time makes the image bigger once and startup
# predictable forever.
ENV FASTEMBED_CACHE_PATH=/opt/fastembed
RUN mkdir -p $FASTEMBED_CACHE_PATH && \
    python -c "from fastembed import TextEmbedding; \
TextEmbedding(model_name='BAAI/bge-small-en-v1.5', cache_dir='$FASTEMBED_CACHE_PATH')"

EXPOSE 8000

# Shell form on purpose: $PORT is injected by Railway at runtime and must be
# expanded.
#
# Bind `0.0.0.0`, not `::`. Two facts make that the correct choice, and both
# changed after the `::` advice in PRD §5.2 was written:
#
#  1. Railway environments created after 2025-10-16 resolve `*.railway.internal`
#     to BOTH an A and an AAAA record, so an IPv4 listener is reachable by
#     sibling services. The private network is no longer IPv6-only.
#  2. Railway's healthcheck connects over IPv4 (observed: 100.64.0.2). asyncio
#     sets IPV6_V6ONLY on every AF_INET6 socket it opens, so a uvicorn bound to
#     `::` refuses IPv4 outright — the healthcheck could never connect, and
#     reported the deploy as "service unavailable" with no request ever reaching
#     the app. Binding `::` is therefore not a stricter version of the same
#     thing; it is the one option that cannot pass a healthcheck.
#
# uvicorn takes a single --host, and asyncio gives no way to ask for a
# dual-stack socket, so this is a choice between the two — not a compromise.
CMD uvicorn agent.main:app --host 0.0.0.0 --port ${PORT:-8000}

# ---------------------------------------------------------------------------
# dev — the integration suite's image. Adds pytest and mypy to `base`, and is
# built only by the profiled `test` service in docker-compose.yml.
# ---------------------------------------------------------------------------
FROM base AS dev
# The integration suite renders real PDFs, so the test image needs WeasyPrint's
# native dependencies. They are added HERE and not in `base`: production `api`
# never renders — the worker does — and pulling Pango into the runtime image
# would undo the "slim, no browser" property the first line of this file claims.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*
RUN uv sync --frozen

# ---------------------------------------------------------------------------
# runtime — production, and deliberately the LAST stage. Railway builds this
# Dockerfile with no `--target`, so whatever ends up last is what ships; keeping
# `dev` above this line is what stops pytest reaching production.
# ---------------------------------------------------------------------------
FROM base AS runtime
