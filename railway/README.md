# Railway provisioning runbook

One Railway project, five services, one Volume, one public domain. Docker
Compose in the repo root is the local mirror of exactly this topology — the
only thing that differs between the two is variable **values**.

> **Phase note.** This is documentation. Provisioning actually happens in Phase
> P9; nothing here has been executed yet.

## Topology

| Service | Source | Public | Notes |
| --- | --- | --- | --- |
| `web` | `apps/web/Dockerfile` | **yes** | Next.js standalone. Owns the custom domain. |
| `api` | `apps/api/Dockerfile` | no | uvicorn on `$PORT`, bound to `::`. |
| `worker` | `apps/api/Dockerfile.worker` | no | arq + Playwright. Owns the Volume. |
| `postgres` | image `pgvector/pgvector:pg16` | no | Self-managed. Not a managed add-on. |
| `redis` | image `redis:7-alpine` | no | Sessions, arq queue, run locks. |

## Order of operations

1. **Create the project**, environment `production`.

2. **`postgres`** — deploy from the image `pgvector/pgvector:pg16`. Attach a
   Volume at `/var/lib/postgresql/data`. Set `POSTGRES_USER`, `POSTGRES_PASSWORD`,
   `POSTGRES_DB`, `PGDATA` and the composed `DATABASE_URL` (see `variables.md`).
   No public domain.

3. **`redis`** — deploy from `redis:7-alpine` with start command
   `redis-server --appendonly yes --requirepass $REDIS_PASSWORD`. A Volume at
   `/data` is optional but recommended so the queue survives a restart. No
   public domain.

4. **`api`** — deploy from this repo. Set **Root Directory** to `apps/api` so
   `railway.json` is picked up (`dockerfilePath: Dockerfile`). Set the variables
   from `variables.md`. Railway runs `alembic upgrade head` as the
   `preDeployCommand` and waits for `/api/v1/health`. **No public domain.**

5. **`worker`** — deploy from this repo, **Root Directory** `apps/api`, and point
   the service at `railway.worker.json` (`dockerfilePath: Dockerfile.worker`).
   Attach the **Volume at `/data`**. No healthcheck, no public domain.
   Give it ≥2 GB RAM — Chromium needs it, and `/dev/shm` is 64 MB, so every
   browser launch must pass `--disable-dev-shm-usage`.

6. **`web`** — deploy from this repo, **Root Directory** `apps/web`. Set
   `API_INTERNAL_URL` to `http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000` and
   `HOSTNAME` to `::`. **Generate a domain** — this is the only service that
   gets one. Attach the custom domain here when it is chosen.

7. **Verify** (see the checklist below) before pointing anyone at the URL.

## Why the private network shapes everything

- Railway's private network is **IPv6-only**. A process bound to `0.0.0.0` is
  invisible to its siblings. `api` binds `::`; `web` sets `HOSTNAME=::`.
- The browser only ever talks to `web`. `next.config.ts` rewrites
  `/api/v1/:path*` to `API_INTERNAL_URL`, so the app is same-origin and the
  session cookie stays first-party `Secure; SameSite=Lax`. Exposing `api`
  publicly would force `SameSite=None` and widen the attack surface for nothing.
- Railway Volumes attach to **one** service and container filesystems are wiped
  on every deploy. Screenshots and exports are therefore written by `worker`
  only, through `agent.storage`, onto `/data`.

## Verification checklist

```bash
# 1. only `web` answers publicly
curl -sS -o /dev/null -w '%{http_code}\n' https://<web-domain>/            # 200
curl -sS --max-time 5 https://<api-domain-if-you-made-one>/api/v1/health   # must not exist

# 2. health through the rewrite, not the api directly
curl -sS https://<web-domain>/api/v1/health                                # {"status":"ok",…}

# 3. migrations ran in the pre-deploy step, not at startup
railway logs --service api | grep -i "alembic"

# 4. the volume survived the deploy
railway ssh --service worker -- ls -la /data
```

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
