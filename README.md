# ads-research-agent

Self-hosted **Paid Ads Research Agent** — Stage 01 of the SDS Manager marketing
pipeline. It runs a deterministic 21-node research DAG over four evidence
sources and emits a versioned, citation-backed Research Report.

One workspace, many users, invite-only, four roles. See `PRD files/prd-research.md`
for the full specification.

> **Status: Phase P3 (Stages 1.1 + 1.2).** Everything P0/P0b/P1/P2 shipped, plus
> the first eight real research nodes, the gate machinery behind node 1.1.5 and
> the `/approvals` API. The dummy nodes are gone. Stages 1.3–1.6 (the remaining
> thirteen nodes) arrive in P4–P5. Phase table: PRD §17.

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

## The DAG today

Eight nodes, two stages, one gate:

```
1.1.1 offer_economics       1.2.1 historical_performance
1.1.2 icp_profile           1.2.2 search_term_pnl
1.1.3 negative_icp          1.2.3 failed_experiments
1.1.4 market_coverage    ← 1.1.2
1.1.5 compliance_guardrails ← 1.1.1   ⛳ gate → approver
```

Two things about them are worth knowing before reading the code.

**No node writes a number.** PRD §18 law 3 puts every CPA, ROAS, share-of-
revenue and payback figure in `nodes/frames.py` and `nodes/pnl.py`. Five of the
eight nodes therefore run in two halves: pandas computes the table, the model is
shown it and asked only for labels, and `reason()` merges the labels onto the
computed rows. A model that miscopies a cost figure cannot put a wrong number in
the report, because the figure never passes through it.

**An empty source is an answer.** A node with no evidence returns empty arrays
without calling a model at all, and records what it could not read in its
`coverage` field (PRD §16: never hallucinate history).

## Approval gates

`1.1.5 compliance_guardrails` is a gate. When it produces its proposal the
executor writes an `Approval`, leaves the node in `awaiting_approval`, halts
**that branch only** and lets every other branch finish. The run then ends the
pass as `awaiting_approval` and gives the project lock back — a gate can sit for
days, and a project that could not be launched for a week would be worse than
the collision the lock prevents.

```bash
curl -b cookies.txt "localhost:3000/api/v1/approvals?mine=true"
curl -X POST localhost:3000/api/v1/approvals/$ID -b cookies.txt \
     -H "X-CSRF-Token: $CSRF" -d '{"decision":"approve","note":"checked"}'
```

* **`operator` cannot decide a gate.** The role that launches runs is precisely
  the one excluded (PRD §4.1). `admin` and `approver` can; when `assignee_id` is
  set, only that person or an admin.
* **Approve with changes is real.** `edited_proposal` replaces the node's output,
  so everything downstream reads the approved text and not the draft.
* **No auto-approve, ever**, and the decision is audit-logged in the same
  transaction that records it.
* A rejected gate ends the run as `failed` with `error.code=approval_rejected`;
  cancelling a paused run expires its open gate rather than leaving a question
  nobody can act on.

## Running the DAG

A run needs two things the API cannot invent: a `project` row and an OpenRouter
credential. Both get their own screens in P6; until then they are rows.

```bash
# launch, then watch it
curl -X POST localhost:3000/api/v1/projects/$PROJECT_ID/runs \
     -H "X-CSRF-Token: $CSRF" -b cookies.txt -d '{}'
curl -N localhost:3000/api/v1/runs/$RUN_ID/events    # SSE, heartbeats every 15s
```

* **`api` never executes a node.** It writes the `run` row, takes the project's
  run lock and enqueues; `worker` does the rest.
* **Nodes are discovered, not listed.** Drop a module in `agent/nodes/` that
  instantiates a node at module level and it is in the DAG. A class that
  declares a `NodeSpec` and is never instantiated is an error, not a silence.
* **The DAG is derived from `depends_on`** and validated at import — a cycle or
  a dangling dependency stops the process at boot rather than mid-run.
* **Resume is the default.** Re-running a crashed run skips every succeeded
  node; `POST /runs/{id}/retry-failed` re-runs only what failed.

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
- **An LLM never sources a fact.** Connectors write `Evidence`; a node's output
  cites `evidence_ids`, and the executor fails the node if it cites anything it
  did not gather.
- **`ctx.complete()` is the only door to a model.** Routing, the cost ledger and
  the stored prompt all live behind it, so no node can spend money the run's
  budget cap cannot see.
- **A run always reaches a terminal state.** Cancel, budget abort, crash or
  failure — the executor records where it stopped and what it had already paid
  for, and releases the project lock.
- **No `if RAILWAY` branch anywhere.** Environments differ by variable values.

## Known limits of P3

- **Thirteen of the twenty-one nodes do not exist yet.** Stages 1.3 (competition),
  1.4 (demand) and 1.5 (readiness) are P4; the report itself is P5. A "full" run
  today is stages 1.1 and 1.2.
- **Google Ads evidence only appears if a credential is stored.** Nodes pull
  through `nodes/gather.py` when the evidence store has nothing of a kind and
  the node names a connector; without the secret they degrade to empty and say
  so in `coverage`. `web_crawler` is not pulled at all — the site crawl belongs
  to P5's landing-page audit.
- **Approval SLAs and reminders are P8.** `Approval.due_at` is left NULL, so
  nothing nags an approver; the inbox badge is the only prompt.
- There is no `POST /projects`, `POST /credentials` or `GET /models` — PRD §14
  lists them but no phase before P6 owns them, so a run's project and OpenRouter
  key are inserted directly for now.
- The run console, approvals inbox and report viewer are P7. P3's surface is the
  API and the SSE feed; `make verify-p3` drives the whole acceptance path
  through it.
- `STORAGE_BACKEND=s3` raises `NotImplementedError` by design.
