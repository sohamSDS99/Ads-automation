# Environment variables

Every value below is set **per service** in the Railway dashboard (or via
`railway variables --set`). Where a value comes from another service, use the
reference expression — never a copy-pasted literal. Railway re-resolves
references on each deploy, so a rotated Postgres password propagates on its own.

Legend: **ref** = Railway reference expression · **manual** = typed once by a
human · **fixed** = a literal that never changes.

## `postgres` (image `pgvector/pgvector:pg16`)

| Variable | Kind | Value |
| --- | --- | --- |
| `POSTGRES_USER` | manual | `agent` |
| `POSTGRES_PASSWORD` | manual | generate 32+ random chars |
| `POSTGRES_DB` | manual | `agent` |
| `PGDATA` | fixed | `/var/lib/postgresql/data/pgdata` |

Volume mounted at `/var/lib/postgresql/data`.

Railway does not synthesise `DATABASE_URL` for a plain image service, so set it
explicitly on `postgres` itself and reference it everywhere else:

```
DATABASE_URL=postgresql://${{POSTGRES_USER}}:${{POSTGRES_PASSWORD}}@${{RAILWAY_PRIVATE_DOMAIN}}:5432/${{POSTGRES_DB}}
```

## `redis` (image `redis:7-alpine`)

| Variable | Kind | Value |
| --- | --- | --- |
| `REDIS_PASSWORD` | manual | generate 32+ random chars |
| `REDIS_URL` | ref | `redis://default:${{REDIS_PASSWORD}}@${{RAILWAY_PRIVATE_DOMAIN}}:6379/0` |

Start command:
`redis-server --appendonly yes --requirepass <literal password> --dir /data`.

**The password must be written out literally.** Railway execs the start command
without a shell, so `$REDIS_PASSWORD` is passed through verbatim and becomes the
password. Redis then starts cleanly and rejects every real client, which shows
up only as `{"status":"degraded","redis":"error"}` on the api's health endpoint.
Keep the `REDIS_PASSWORD` variable in step with whatever literal you use here —
`REDIS_URL` is built from it.

## `api` (Dockerfile `apps/api/Dockerfile`)

| Variable | Kind | Value |
| --- | --- | --- |
| `PORT` | — | injected by Railway as **8080**; never set it yourself |
| `DATABASE_URL` | ref | `${{postgres.DATABASE_URL}}` |
| `REDIS_URL` | ref | `${{redis.REDIS_URL}}` |
| `APP_ENCRYPTION_KEY` | manual | 32 random bytes, base64. Set once. Still required — sessions and download tokens derive from it |
| `FILE_TOKEN_SECRET` | manual | 32 random bytes, base64 |
| `WORKER_INTERNAL_URL` | ref | `http://${{worker.RAILWAY_PRIVATE_DOMAIN}}:8081` |
| `RAILWAY_DOCKERFILE_PATH` | — | **`api` only**: leave unset. `worker` needs it — see below |
| `SESSION_COOKIE_NAME` | fixed | `ara_session` |
| `SESSION_TTL_DAYS` | fixed | `30` |
| `COOKIE_SECURE` | fixed | `true` |
| `APP_BASE_URL` | manual | `https://<your web domain>` — **invite links are built from this; a wrong value ships dead links** |
| `BOOTSTRAP_ADMIN_EMAIL` | manual | the first admin's address |
| `BOOTSTRAP_ADMIN_PASSWORD` | manual | ≥12 chars; change it after first login |
| `OPENROUTER_BASE_URL` | fixed | `https://openrouter.ai/api/v1` |
| `STORAGE_BACKEND` | fixed | `local` |
| `STORAGE_DIR` | fixed | `/data` |
| `MAX_RUN_COST_USD` | manual | `15.00` |
| `APP_ENV` | fixed | `production` |
| `LOG_LEVEL` | fixed | `INFO` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `SMTP_FROM` | manual | optional; without them invites are copyable links |
| `OPENROUTER_API_KEY` | manual | **required** — every node is a call through it, so a run cannot start without it |
| `DATAFORSEO_API_KEY` | manual | optional; without it keyword volume and CPC are missing from the report |
| `WEBSHARE_API_KEY` | manual | optional; without it every crawl goes out directly and nothing degrades |
| `GOOGLE_ADS_DEVELOPER_TOKEN` | manual | optional as a set: these five together, or none at all. Mint the OAuth three with `make google-ads-oauth` |
| `GOOGLE_ADS_CLIENT_ID` | manual | ↑ |
| `GOOGLE_ADS_CLIENT_SECRET` | manual | ↑ |
| `GOOGLE_ADS_REFRESH_TOKEN` | manual | ↑ |
| `GOOGLE_ADS_CUSTOMER_ID` | manual | ↑ the 10-digit account the research reads |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | manual | only when that account is reached through a manager (MCC) account |
| `WEBSHARE_COUNTRY` / `WEBSHARE_PROXY_*` | — | leave unset. Shape only; every one has a working default |

**Every source key is set here and nowhere else.** The interface has no form to
type one into: Settings → Connections switches a source on or off for a
workspace and reads its credential from these variables. A source whose
variables are unset cannot be connected, and the screen names the missing ones.
Changing any of them needs a redeploy — settings are read once at startup.

Generate both secrets with:

```bash
python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
```

## `worker` (Dockerfile `Dockerfile`, at the repo root)

Same variables as `api`, minus `PORT` and the bootstrap pair. The worker owns
the Volume, mounted at `/data`.

`worker` shares the `apps/api` build context with `api` but must build the other
Dockerfile, and Railway auto-detects only a file named exactly `Dockerfile`:

There is no variable for this, and no setting either — both were tried and
ignored (`RAILWAY_DOCKERFILE_PATH` in all three path forms, and the service's
Dockerfile Path build setting, even on a service configured before its first
build). The worker's Dockerfile therefore sits at the **repo root**, where plain
auto-detection finds it:

```
Root Directory:  (empty — the repo root)
Dockerfile Path: (unset)
```

Railway also caches builds and the key ignores the Dockerfile Path, so changing a
variable alone often returns the previous image. If the worker's deploy log says
`Uvicorn running on …` rather than starting `arq`, it built the api's Dockerfile:
read the FIRST line of the build log, `load build definition from <path>`, which
is the only reliable readout of what Railway chose.

| Variable | Kind | Value |
| --- | --- | --- |
| `DATABASE_URL` | ref | `${{postgres.DATABASE_URL}}` |
| `REDIS_URL` | ref | `${{redis.REDIS_URL}}` |
| `APP_ENCRYPTION_KEY` | manual | **the same value as `api`** |
| `FILE_TOKEN_SECRET` | manual | **the same value as `api`** |
| `STORAGE_DIR` | fixed | `/data` |

## `web` (Dockerfile `apps/web/Dockerfile`)

| Variable | Kind | Value |
| --- | --- | --- |
| `PORT` | — | injected by Railway |
| `HOSTNAME` | fixed | `::` |
| `API_INTERNAL_URL` | ref | `http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8080` |

`API_INTERNAL_URL` is read by `next.config.ts` at **build** time, not at server
start: `rewrites()` is resolved by `next build` and written into
`routes-manifest.json`, and the standalone server never re-reads the config.
Railway only passes a service variable into a Docker build when the Dockerfile
declares it, so `apps/web/Dockerfile` carries `ARG API_INTERNAL_URL`. Changing
this variable therefore requires a **rebuild**, not a restart — and a service
variable alone is not enough if that `ARG` is ever removed.

The port is **8080**, because that is what Railway injects as `PORT` and what
`api` therefore listens on.

It is **not** public: the browser never sees it, and no browser-visible env var
carries an API address. If you ever find yourself adding one, the rewrite is
broken — fix the rewrite.

## Rules

1. `api` and `worker` must resolve `DATABASE_URL`/`REDIS_URL` through reference
   expressions, so rotating a password is a one-place change.
2. `APP_ENCRYPTION_KEY` must be identical on `api` and `worker`, and must never
   be logged, echoed in a build step, or returned by an endpoint.
3. Nothing reads `RAILWAY_*` variables in application code. Environments differ
   by **values only** (PRD §15 NF8c).
4. `APP_BASE_URL` on `api` must be the public domain of `web`. It is the only
   place an invite link's origin comes from, and it cannot be derived: `api` has
   no public domain of its own to infer one from.
5. `BOOTSTRAP_ADMIN_EMAIL`/`_PASSWORD` are read once, on the first boot against
   an empty database. Leaving them set afterwards is harmless — the bootstrap
   path refuses to run a second time — but rotating the password there does
   **not** change the admin's password. Use the app for that.
