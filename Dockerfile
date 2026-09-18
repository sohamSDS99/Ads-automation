# ---------------------------------------------------------------------------
# worker service — arq + Playwright + the storage Volume.
# The Playwright base image ships Ubuntu 22.04 with Python 3.10, so uv installs
# and pins a managed CPython 3.12 to match the api service exactly.
#
# Railway: Root Directory = <repo root>, Dockerfile Path = unset.
#
# WHY THIS FILE IS AT THE REPO ROOT AND NOT `apps/api/Dockerfile.worker`
#
# Railway auto-detects the build file by composing Root Directory + `Dockerfile`
# and, on this project, honours NOTHING else: the `RAILWAY_DOCKERFILE_PATH`
# service variable (tried as `Dockerfile.worker`, `./Dockerfile.worker` and
# `apps/api/Dockerfile.worker`) and the service's own Dockerfile Path build
# setting were both ignored, including on a brand-new service configured before
# its first build ever ran. `api` and `worker` cannot therefore share the
# `apps/api` directory as a root, because only one of them can own the file
# literally named `Dockerfile` there.
#
# So the worker takes the repo root as its context and owns the `Dockerfile`
# there; `api` keeps Root Directory `apps/api` and owns `apps/api/Dockerfile`.
# Each service is then satisfied by plain auto-detection, and nothing depends on
# a setting that silently does not apply.
#
# The cost is that every COPY below is prefixed `apps/api/`, because the build
# context is now the repo root rather than `apps/api`.
#
# To confirm which file Railway actually chose, read the first line of a build
# log — `load build definition from <path>`. Image digests will not tell you:
# they are identical across cache hits.
# ---------------------------------------------------------------------------
FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_PYTHON=3.12 \
    UV_PYTHON_PREFERENCE=only-managed \
    UV_PYTHON_INSTALL_DIR=/opt/uv-python

COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/

WORKDIR /app

RUN uv python install 3.12

# WeasyPrint's native dependencies (PRD §12). Since WeasyPrint 53 that is Pango
# alone — Cairo and GDK-PixBuf were dropped — plus a font the stylesheet can
# actually name. Without fonts-dejavu-core the PDF still renders, silently
# substituting whatever face is present, and a document that picks its own font
# per deploy is a document that does not reproduce.
#
# These live in the worker image only. Exports are generated here (the worker
# owns the Volume); the api image stays slim and never imports WeasyPrint.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpango-1.0-0 \
        libpangoft2-1.0-0 \
        fonts-dejavu-core \
    && rm -rf /var/lib/apt/lists/*

# `pg_dump` for the nightly backup (PRD §17, P8). Version 16 specifically, from
# PGDG rather than Ubuntu's archive: jammy ships client 14, and pg_dump refuses
# outright to dump a newer server ("server version 16.x; pg_dump version 14.x").
# The database is `pgvector/pgvector:pg16`, so client 14 would mean the backup
# job fails every night and only says so in a log nobody reads.
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL https://www.postgresql.org/media/keys/ACCC4CF8.asc \
        -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] \
https://apt.postgresql.org/pub/repos/apt jammy-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update && apt-get install -y --no-install-recommends postgresql-client-16 \
    && rm -rf /var/lib/apt/lists/*

COPY apps/api/pyproject.toml apps/api/uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# Only `apps/api` — the context is the whole repo now, and copying `.` would
# drag `apps/web`, `node_modules` and the PRD files into the worker image.
COPY apps/api/ ./
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH" \
    STORAGE_DIR=/data

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

# Install the browsers this venv's Playwright actually asks for.
#
# The base image ships browsers for the Playwright *it* was built around
# (v1.49). This image then builds a separate managed-CPython venv and installs
# `playwright` from `uv.lock`, which resolves the `>=1.49.0` floor to whatever
# is current — 1.63 at the time of writing. Playwright addresses browsers by
# build number, so the venv looked for `chromium-1243`, found the 1.49-era
# builds, and every browser call died with "Executable doesn't exist at
# /ms-playwright/...". That took out the Ads Transparency Center scrape
# entirely — the report's whole competitor-creative section — and degraded the
# landing-page vitals pass, and it did it quietly, as a partial source rather
# than a failed build.
#
# Running the install from the venv is what keeps the two in step: it fetches
# the builds THIS Playwright names, so bumping the lock or the base image can
# never drift them apart again. Both binaries are installed because
# `launch(headless=True)` prefers the headless shell while `launch()` and any
# future headed/trace use want the full browser.
RUN /app/.venv/bin/python -m playwright install chromium chromium-headless-shell

# Fail the build rather than ship a worker that cannot open a page. A browser
# missing at runtime is invisible until a research run quietly returns no
# competitor ads; missing at build time it is one red line here.
RUN /app/.venv/bin/python -c "\
from playwright.sync_api import sync_playwright; \
p = sync_playwright().start(); \
b = p.chromium.launch(args=['--no-sandbox', '--disable-dev-shm-usage']); \
print('chromium', b.version); b.close(); p.stop()"

# No healthcheck and no exposed port: the worker has no HTTP ingress.
CMD ["arq", "agent.worker.WorkerSettings"]
