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

# The image precheck's OCR engine (Stage 03 PRD §9.5, §6). WORKER ONLY — `api`
# receives the upload and enqueues the measurement; it never runs tesseract, so
# it stays slim exactly as §6 requires.
#
# MEASURED, because the PRD's estimate is wrong by a factor of twenty: these two
# packages cost **9.8 MiB** on this base (879,849,937 -> 890,090,986 bytes), not
# the "roughly +200 MB" §6 and open question Q6 budget for. Q6 asks whether the
# OCR stack justifies a separate one-shot job. At 10 MiB the question does not
# arise.
#
# `tesseract-ocr-eng` is listed explicitly rather than relied upon. jammy's
# `tesseract-ocr` recommends it and this install uses --no-install-recommends,
# so without the second package there is a tesseract binary with NO language
# data — which fails at the first call, at runtime, in the worker, on an image
# somebody was waiting on, rather than here at build time.
#
# NOT version-pinned, deliberately, and the alternative was tried first.
# `tesseract-ocr=4.1.1-2.1build1` is what jammy resolves today and the pin builds
# fine — until Ubuntu supersedes it and drops the old version from the archive,
# at which point every worker build fails on a line nobody has touched in months.
# A pin that expires silently is worse than no pin.
#
# The drift it was meant to catch is caught properly instead, in two places that
# cannot expire: the assertion below fails the BUILD if the engine is ever not
# tesseract 4.x, and `imaging/precheck.detector_version()` stamps the exact
# running version into every `derived` image_metric row, so two metrics taken
# months apart are always comparable by inspection rather than by assumption.
# The assertion runs in its own `sh -eu` with a `case`, not as a chain of
# `&&`/`||` around a pipe. Two reasons, both learned the hard way in this repo:
# `cmd | head -1` SIGPIPEs the writer and reads as a failure the moment anything
# enables pipefail, and a `||` guard in a long `&&` chain is exactly the shape
# that reports a pass when the check itself never ran.
RUN apt-get update && apt-get install -y --no-install-recommends \
        tesseract-ocr \
        tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/* \
    && tesseract --version \
    && tesseract --list-langs \
    && sh -euc 'version=$(tesseract --version 2>&1 | sed -n "1s/^tesseract //p"); \
        case "$version" in \
          4.*) echo "tesseract $version — as expected" ;; \
          *) echo "FATAL: expected tesseract 4.x, got \"${version:-nothing}\". A major-version change moves every stored image metric."; exit 1 ;; \
        esac; \
        if ! tesseract --list-langs 2>&1 | grep -qx eng; then \
          echo "FATAL: no eng language data. OCR would fail at runtime in the worker, not here."; exit 1; \
        fi'

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

# Stage 04's deterministic post-production (Stage 04 PRD §9.4, §13, §22). WORKER
# ONLY, like tesseract above: `api` never opens a media file.
#
#   ffmpeg                  video assembly, caption burn-in and renditions.
#   libimage-exiftool-perl  `exiftool` writes, then RE-READS, the XMP
#                           DigitalSourceType / MP4 comment provenance that §13
#                           requires on every AI-made file.
#   fonts-inter,
#   fonts-noto-core         the faces captions, end cards and disclosure labels
#                           are composited in. Named packages, not whatever the
#                           base image happens to carry: a missing face falls back
#                           SILENTLY to another, and a render that picks its own
#                           font per deploy does not reproduce.
#
# Not version-pinned, for the reason the tesseract layer gives. Both versions are
# printed into the build log, and the build FAILS if either binary or either face
# is missing — here, rather than in a paid render at runtime. (The worker also
# logs both versions at boot: see `startup` in `agent/worker.py`.)
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        libimage-exiftool-perl \
        fonts-inter \
        fonts-noto-core \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version \
    && exiftool -ver \
    && sh -euc 'for family in "Inter" "Noto Sans"; do \
          if ! fc-list : family | grep -q "$family"; then \
            echo "FATAL: font family \"$family\" is not installed."; exit 1; \
          fi; \
          echo "font family \"$family\" — present"; \
        done'

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
