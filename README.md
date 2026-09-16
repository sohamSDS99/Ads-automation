# ads-research-agent

Self-hosted **Paid Ads Research Agent** — Stage 01 of the SDS Manager marketing
pipeline. It runs a deterministic 21-node research DAG over four evidence
sources and emits a versioned, citation-backed Research Report.

One workspace, many users, invite-only, four roles. See `PRD files/prd-research.md`
for the full specification.

> **Status: Phase P0b (Auth & workspace).** Schema, scaffold, and the whole
> sign-in surface: Argon2id passwords, Redis sessions, four roles, invites,
> CSRF, and the audit trail. The orchestrator, connectors, nodes, report and
> exports arrive in later phases — the phase table lives in PRD §17.

## Setup

```bash
git clone git@github.com:sohamSDS99/Ads-automation.git && cd Ads-automation
cp .env.example .env            # optional; Compose has working dev defaults
make up                         # builds, starts five services, runs migrations
make health                     # {"status":"ok","db":"ok","redis":"ok",…}
open http://localhost:3000      # sign in as BOOTSTRAP_ADMIN_EMAIL
```

That is the whole setup. `make up` is idempotent; `make clean` destroys the
local volumes.

The first boot against an empty database creates the workspace and its only
admin from `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`. After that
there is no path to an account except an invite — change the dev password from
the account menu, and set a real one before deploying anywhere.

## What runs where

| Service | Port | Public | Purpose |
| --- | --- | --- | --- |
| `web` | 3000 | **yes** | Next.js 15 App Router. The only thing a browser talks to. |
| `api` | 8000 | no | FastAPI. Reached only through the `web` rewrite. |
| `worker` | — | no | arq + Playwright. Owns the storage volume at `/data`. |
| `postgres` | 5432 | no | Self-managed `pgvector/pgvector:pg16`. |
| `redis` | 6379 | no | Sessions, job queue, run locks. |

This mirrors the Railway topology exactly (see `railway/README.md`). `api` and
`worker` publish no host port here for the same reason they get no public domain
there.

## Everyday commands

```bash
make up            # start everything and migrate
make health        # health through the web rewrite, as a browser sees it
make test          # unit + integration + route guards + mypy + tsc
make test-integration  # the DB+Redis suite, inside the compose network
make guards        # fail if any route lacks its require(Permission)
make verify        # PRD §19.1's acceptance list against the running stack
make browser       # render the auth screens in Chromium and assert on them
make lint          # ruff + eslint
make migrate       # alembic upgrade head
make psql          # psql shell
make contracts     # regenerate packages/contracts from the Pydantic models
make logs          # tail everything
make clean         # stop and delete volumes
```

## Layout

```
apps/api      FastAPI + SQLAlchemy 2.0 + Alembic + arq       (uv-managed)
apps/web      Next.js 15 + Tailwind v4 + shadcn/ui           (pnpm)
packages/contracts   JSON Schema emitted from Pydantic, consumed as zod
railway/      provisioning runbook + the full env-var table
```

## Rules this repo enforces

- **The frontend calls relative paths only.** `next.config.ts` rewrites
  `/api/v1/*` to the API over the private network, so the app is same-origin
  everywhere and the session cookie stays first-party. No browser-visible env
  var carries an API address — `tests/test_deployment_config.py` fails the build
  if one appears.
- **Nothing outside `agent/storage/` touches the filesystem.** Container
  filesystems are ephemeral; durable artifacts go through `agent.storage` onto
  the worker's volume. `tests/test_filesystem_boundary.py` enforces it.
- **Every query is workspace-scoped** through `agent.db.repo.WorkspaceScopedRepo`.
  Subclassing it for a model without a `workspace_id` raises at import.
- **Secrets are AES-256-GCM sealed** with a key that exists only in the
  environment. `APP_ENCRYPTION_KEY` must decode to exactly 32 bytes or the
  process refuses to start.
- **Migrations never run at startup** — only as Railway's `preDeployCommand`,
  because `api` and `worker` boot concurrently.
- **Every route declares a permission.** `require(Permission)` is the only way
  a route is reachable; `scripts/check_route_guards.py` walks the router modules
  and fails the build on any route that declares neither a permission nor a
  place on the six-entry public allowlist.
- **Authorization is never cached in the session.** The `user` row is read on
  every request, so a demotion or a disable takes effect on the next call
  rather than whenever a session happens to be rebuilt.
- **Every mutating action writes an `AuditLog` row in the same transaction** as
  the change it describes, so the two cannot come apart.
- **Secrets never reach a log.** A structlog processor redacts any field whose
  name mentions a password, secret, token, authorization, ciphertext or cookie,
  ahead of the renderer.
- **No `if RAILWAY` branch anywhere.** Environments differ by variable values.

## Known limits of P0

- The nav links to Approvals, Evidence and Settings resolve to a 404 page —
  those screens are built in P6/P7.
- There is no login. The shell is deliberately unguarded until P0b.
- `STORAGE_BACKEND=s3` raises `NotImplementedError` by design.
