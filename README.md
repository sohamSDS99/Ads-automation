# ads-research-agent

Self-hosted **Paid Ads Research Agent** — Stage 01 of the SDS Manager marketing
pipeline. It runs a deterministic 21-node research DAG over four evidence
sources and emits a versioned, citation-backed Research Report.

One workspace, many users, invite-only, four roles. See `PRD files/prd-research.md`
for the full specification.

> **Status: Phase P1 (Orchestrator + LLM gateway).** Everything P0/P0b shipped,
> plus the engine: an auto-discovered node registry, a validated DAG, a
> wavefront executor with retries, repair passes, checkpoints, cancellation and
> a budget cap, the OpenRouter gateway with strict structured output, and the
> run API with its SSE feed. Two dummy nodes stand in for the real DAG —
> connectors arrive in P2, the 21 research nodes in P3–P5. Phase table: PRD §17.

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
make verify-p5a    # P5a's acceptance list: five formats, rendered and downloaded
make browser       # render the auth screens in Chromium and assert on them
make lint          # ruff + eslint
make migrate       # alembic upgrade head
make psql          # psql shell
make contracts     # regenerate packages/contracts from the Pydantic models
make logs          # tail everything
make clean         # stop and delete volumes
```

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

## Reports and exports

A finished run has one `ResearchReport` (PRD §11) — a validated object, not
prose. Every format is a projection of it, which is what guarantees the PDF, the
DOCX and the JSON cannot disagree: the model fills the object, and a
deterministic template renders it.

```bash
curl -b cookies.txt localhost:3000/api/v1/reports/$RUN_ID          # the report + its markdown

curl -X POST -b cookies.txt -H "X-CSRF-Token: $CSRF" \
     "localhost:3000/api/v1/reports/$RUN_ID/export?format=pdf"     # 202 {job_id}

curl -b cookies.txt localhost:3000/api/v1/exports/$JOB_ID          # queued|running|ready|failed
curl -b cookies.txt -OJ localhost:3000/api/v1/exports/$JOB_ID/download
```

* **Generation is a worker job, never a request.** A 200-creative PDF takes tens
  of seconds, and the Volume attaches to `worker` — `api` has no disk to write
  to. The export row is created at enqueue time and its id *is* the job id.
* **`api` cannot read the Volume either.** It signs a capability for one storage
  key, fetches the object from the worker's internal file server over the
  private network, and relays the bytes. Nothing on that network can read an
  object without a signature.
* **Exports are for everyone.** PRD §4.1 gives read and export to all four
  roles, `viewer` included.
* **Long tables say they are truncated.** The prose formats preview the top 50
  keywords; the CSV is the complete list, and the document says so rather than
  letting a cut table read as a full one.

## Layout

```
apps/api      FastAPI + SQLAlchemy 2.0 + Alembic + arq       (uv-managed)
  src/agent/export/    the report contract, five renderers, templates
  src/agent/fileserver.py   worker-only; serves the Volume to `api`
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

## Known limits of P1

- The DAG is two dummy nodes (`0.1`, `0.2`). The real 21 arrive in P3–P5 and
  delete `agent/nodes/dummy.py`.
- `gather()` always returns nothing: connectors and the evidence store are P2,
  so no node has real evidence to cite yet.
- **Gate nodes are refused at registration.** The approval machinery lands in
  P3; registering a `gate=True` node now would halt a branch nothing could
  resume.
- There is no `POST /projects`, `POST /credentials` or `GET /models` — PRD §14
  lists them but no phase before P6 owns them, so a run's project and OpenRouter
  key are inserted directly for now.
- The run console, approvals inbox and report viewer are P7. P1's surface is the
  API and the SSE feed.
- `STORAGE_BACKEND=s3` raises `NotImplementedError` by design.

## Known limits of P5a

- **Nothing writes a report yet.** Nodes 1.5.\*, `report_synthesis` (1.6.1) and
  `report_critique` (1.6.2) are P5b, so `GET /reports/{run_id}` answers with a
  404 naming the run's status until one exists. `scripts/verify-p5a.sh` seeds a
  report to prove the rest of the path.
- **The charts do not use matplotlib.** PRD §12 names its svg backend;
  `export/charts.py` builds the SVG directly, because matplotlib embeds font
  metrics and glyph paths and the same report would render to different bytes on
  different machines. Reverting is one module. Flagged for a ruling.
- **The PDF embeds 18 screenshots, not 200.** §12 sizes the file at 15 MB and
  200 base64 PNGs exceed that before the text is counted. The gallery prints how
  many it omitted.
- **`export.ready` can be published after `run.completed`.** §7.3 enumerates
  eight event types and §12 asks for this ninth on the same channel. A console
  that closes on the terminal event will miss it; `GET /exports/{job_id}` is the
  reliable answer.
- **The Report Viewer is P7.** This phase is the API and the files.
