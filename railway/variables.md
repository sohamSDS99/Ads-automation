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

Start command: `redis-server --appendonly yes --requirepass $REDIS_PASSWORD`.

## `api` (Dockerfile `apps/api/Dockerfile`)

| Variable | Kind | Value |
| --- | --- | --- |
| `PORT` | — | injected by Railway; never set it yourself |
| `DATABASE_URL` | ref | `${{postgres.DATABASE_URL}}` |
| `REDIS_URL` | ref | `${{redis.REDIS_URL}}` |
| `APP_ENCRYPTION_KEY` | manual | 32 random bytes, base64. **Set once. Rotating it orphans every stored credential.** |
| `FILE_TOKEN_SECRET` | manual | 32 random bytes, base64 |
| `WORKER_INTERNAL_URL` | ref | `http://${{worker.RAILWAY_PRIVATE_DOMAIN}}:8081` |
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
| `WEBSHARE_*` | — | leave unset. Shape only. The Webshare account is a vault credential (`webshare`) stored from the Sources step, never an environment variable — and it is optional: with no key every crawl goes out directly |

Generate both secrets with:

```bash
python -c "import base64,os;print(base64.b64encode(os.urandom(32)).decode())"
```

## `worker` (Dockerfile `apps/api/Dockerfile.worker`)

Same variables as `api`, minus `PORT` and the bootstrap pair. The worker owns
the Volume, mounted at `/data`.

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
| `API_INTERNAL_URL` | ref | `http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000` |

`API_INTERNAL_URL` is read by `next.config.ts` at server start. It is **not**
public: the browser never sees it, and no browser-visible env var carries an
API address. If you ever find yourself adding one, the rewrite is broken —
fix the rewrite.

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
