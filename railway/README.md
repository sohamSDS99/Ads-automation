# Railway provisioning runbook

One Railway project, five services, one Volume, one public domain. Docker
Compose in the repo root is the local mirror of exactly this topology — the
only thing that differs between the two is variable **values**.

> **Status.** Provisioned and live (2026-09-18) in project `Ads-automation`,
> environment `production`. The public URL is
> <https://web-production-808ac.up.railway.app>.

> **Config as Code is dead — read this before you trust the `railway.json`s.**
> Railway deprecated `railway.json` / `railway.toml`, and **new services cannot
> opt into it at all** (existing ones stop being read on 2026-12-01). The three
> files in this repo are therefore *documentation of intent*, not configuration:
> nothing on Railway reads them. Every build and deploy setting below is set on
> the service itself. Change a `railway.json` and nothing happens — change the
> service setting.

## Topology

| Service | Source | Public | Notes |
| --- | --- | --- | --- |
| `web` | `apps/web/Dockerfile` | **yes** | Next.js standalone. Owns the custom domain. |
| `api` | `apps/api/Dockerfile` | no | uvicorn on `$PORT`, bound to `::`. |
| `worker` | `Dockerfile` (repo root) | no | arq + Playwright. Owns the Volume. |
| `postgres` | image `pgvector/pgvector:pg16` | no | Self-managed. Not a managed add-on. |
| `redis` | image `redis:7-alpine` | no | Sessions, arq queue, run locks. |

## Order of operations

1. **Create the project**, environment `production`.

2. **`postgres`** — deploy from the image `pgvector/pgvector:pg16`. Attach a
   Volume at `/var/lib/postgresql/data`. Set `POSTGRES_USER`, `POSTGRES_PASSWORD`,
   `POSTGRES_DB`, `PGDATA` and the composed `DATABASE_URL` (see `variables.md`).
   No public domain.

3. **`redis`** — deploy from `redis:7-alpine`. A Volume at `/data` is optional
   but recommended so the queue survives a restart. No public domain.

   The start command is
   `redis-server --appendonly yes --requirepass <the literal password> --dir /data`.

   **Write the password literally.** Railway does **not** run the start command
   through a shell — it splits the string and execs it — so
   `--requirepass $REDIS_PASSWORD` sets the password to the seven characters
   `$REDIS` … literally, and nothing warns you. Redis starts, logs "Ready to
   accept connections", and every client that authenticates with the real
   password is rejected. It surfaces far away from the cause, as
   `{"status":"degraded","redis":"error"}` on the api's health endpoint.

4. **`api`** — deploy from this repo. Set **Root Directory** to `apps/api`;
   Railway then auto-detects `apps/api/Dockerfile`. Set these on the service
   (not in `railway.json` — see the banner above):

   - Pre-deploy command: `alembic upgrade head`
   - Healthcheck path: `/api/v1/health`, timeout 60

   Set the variables from `variables.md`. **No public domain.**

5. **`worker`** — deploy from this repo, **Root Directory** `apps/api`. Attach
   the **Volume at `/data`**. No healthcheck, no public domain. Give it ≥2 GB
   RAM — Chromium needs it, and `/dev/shm` is 64 MB, so every browser launch
   must pass `--disable-dev-shm-usage`.

   **Leave Root Directory empty** — the worker builds from the repo root, where
   its `Dockerfile` lives. Leave Dockerfile Path unset too; auto-detection finds
   it.

   That placement is not cosmetic. Railway selects the build file by composing
   Root Directory + `Dockerfile` and, on this project, honours nothing else:

   - the `RAILWAY_DOCKERFILE_PATH` service variable was ignored in all three
     forms (`Dockerfile.worker`, `./Dockerfile.worker`,
     `apps/api/Dockerfile.worker`);
   - the service's own **Dockerfile Path** build setting was ignored too —
     including on a brand-new service configured before its first build ever ran.

   So `api` and `worker` cannot both be rooted at `apps/api`: only one of them
   can own the file named `Dockerfile` there. `api` keeps `apps/api`, the worker
   takes the repo root.

   Two traps worth knowing anyway:

   - **The failure is silent.** A worker that built the api's Dockerfile starts
     `uvicorn` and Railway still reports SUCCESS. The authoritative check is the
     FIRST line of the build log — `load build definition from <path>`. Image
     digests will not tell you: they are identical across cache hits.
   - **Railway caches builds**, and the key ignores the Dockerfile Path. Changing
     only a variable frequently hands back the previous image. Changing the Root
     Directory or pushing a commit moves the key.

6. **`web`** — deploy from this repo, **Root Directory** `apps/web`. Set
   `API_INTERNAL_URL` to `http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8080` (8080,
   not 8000 — see "Ports" below) and `HOSTNAME` to `::`. Set the healthcheck
   path to **`/login`**, not `/`: middleware redirects a signed-out probe, and
   Railway treats the 307 as a failure, so `/` can never pass.
   **Generate a domain** — this is the only service that gets one. Attach the
   custom domain here when it is chosen.

7. **Verify** (see the checklist below) before pointing anyone at the URL.

## Ports, binds, and the private network

Four facts, each of which cost a failed deploy to learn. They are listed before
the design notes because they are what actually breaks.

**Railway injects `PORT=8080`.** Not 8000. Do not set `PORT` yourself, and do
not assume the number: `api` therefore listens on **8080**, which is why
`API_INTERNAL_URL` ends in `:8080`. Setting `PORT=8000` makes uvicorn listen
where nothing is looking, and the healthcheck fails as "service unavailable".

**Python services must bind `0.0.0.0`, not `::`.** The old advice here — the
private network is IPv6-only, so bind `::` — is no longer true, and following it
now guarantees a failed deploy:

- Environments created after **2025-10-16** resolve `*.railway.internal` to both
  an A and an AAAA record. IPv4 listeners are reachable by siblings.
- Railway's healthcheck connects over **IPv4** (observed source `100.64.0.2`).
- asyncio sets `IPV6_V6ONLY` on every `AF_INET6` socket, so a uvicorn bound to
  `::` **refuses IPv4 outright**. No request ever arrives; Railway reports
  "service unavailable" and you go looking for a dependency problem that isn't
  there.

`web` stays on `HOSTNAME=::` because Node binds dual-stack and accepts both.
That asymmetry is a property of the two runtimes, not an oversight.

**Start commands are not run through a shell.** Railway splits the string and
execs it. `--port ${PORT:-8000}` arrives at uvicorn as the literal string
`${PORT:-8000}`; `--requirepass $REDIS_PASSWORD` sets the password to
`$REDIS_PASSWORD`. Prefer the image's own `CMD` (shell form, so it expands), and
where a start command is unavoidable, write literals.

**`web`'s proxy target is baked at build time.** `next.config.ts` resolves
`rewrites()` during `next build` and writes it into `routes-manifest.json`; the
standalone server never re-reads the config. Railway passes a service variable
into a Docker build only when the Dockerfile declares it as an `ARG`, so
`apps/web/Dockerfile` declares `ARG API_INTERNAL_URL`. Without it the build
falls back to the compose default `http://api:8000`, ships that to production,
and every `/api/v1` call dies with `ENOTFOUND` behind a blank 500.

## Why the private network shapes everything

- The browser only ever talks to `web`. `next.config.ts` rewrites
  `/api/v1/:path*` to `API_INTERNAL_URL`, so the app is same-origin and the
  session cookie stays first-party `Secure; SameSite=Lax`. Exposing `api`
  publicly would force `SameSite=None` and widen the attack surface for nothing.
- Railway Volumes attach to **one** service and container filesystems are wiped
  on every deploy. Screenshots and exports are therefore written by `worker`
  only, through `agent.storage`, onto `/data`.

## Verification checklist

```bash
WEB=https://web-production-808ac.up.railway.app

# 1. `web` answers, and `/` redirects a signed-out visitor to /login
curl -sS -o /dev/null -w '%{http_code}\n' $WEB/login                       # 200
curl -sS -o /dev/null -w '%{http_code}\n' $WEB/                            # 307

# 2. the api answers THROUGH the rewrite — this is the one that proves the
#    build-time API_INTERNAL_URL, the private network and both dependencies at
#    once. `degraded` here means redis or postgres, and says which.
curl -sS $WEB/api/v1/health                 # {"status":"ok","db":"ok","redis":"ok",…}

# 3. only `web` is public: no other service may have a domain
#    (check the Networking tab of api / worker / postgres / redis)

# 4. migrations ran in the pre-deploy step, not at startup
#    api's deploy log shows `alembic.runtime.migration` BEFORE `Uvicorn running`

# 5. the worker is running arq, not uvicorn — the silent-failure check
#    worker's deploy log must NOT contain `Uvicorn running on`, and its build
#    log's first line must read `load build definition from Dockerfile`
```

The `railway` CLI cannot link to this project (it reports "Project is deleted"
while `railway list` shows it and the API serves it happily), so `railway logs`
and `railway ssh` are unavailable here. Use the dashboard or the Railway MCP
server for logs and status.

## Operations

- **Migrations** run only as `preDeployCommand`. Never at app startup: `api`
  and `worker` boot concurrently and would race.
- **Rollback**: redeploy the previous deployment from the service's Deployments
  tab. A failed healthcheck rolls back on its own without dropping the running
  version.
- **Backups**: a nightly arq job writes `pg_dump -Fc` into `/data/backups/` and
  keeps 14 days (built in P8). Railway Volume snapshots are the second line of
  defence, not the first.
- **Restore**: `railway ssh --service worker`, copy the dump out of
  `/data/backups/`, then
  `pg_restore -d $DATABASE_URL --clean --if-exists <dump>` from a shell that can
  reach `postgres` on the private network.
- **Scheduling** is DB state polled by the worker, not Railway Cron. Do not add
  a Railway cron job.
