# ads-research-agent

Self-hosted **Paid Ads Research Agent** — Stage 01 of the SDS Manager marketing
pipeline. It runs a deterministic 21-node research DAG over four evidence
sources and emits a versioned, citation-backed Research Report.

One workspace, many users, invite-only, four roles. See `PRD files/prd-research.md`
for the full specification.

> **Status: Phase P4 (Stages 1.3 + 1.4).** Everything P0/P0b/P1/P2/P3 shipped,
> plus nine more nodes: the competitor set, the Playwright creative corpus with
> stored screenshots, spend estimation, the second gate (1.3.4), and the demand
> half — a ≥2,000-term keyword universe, batched intent classification, the
> DataForSEO demand join, the negative blocklist and the keyword→page map.
> Seventeen of the twenty-one nodes now exist. Stage 1.5 and the report itself
> are P5. Phase table: PRD §17.

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
make verify-p4     # P4's exit criteria end to end (needs a live OpenRouter key)
make verify-p6     # P6's acceptance list against the running stack
make browser       # render the auth screens in Chromium and assert on them
make browser-p6    # drive the P6 screens as admin and as operator, 1440 + 390
make lint          # ruff + eslint
make migrate       # alembic upgrade head
make psql          # psql shell
make contracts     # regenerate packages/contracts from the Pydantic models
make logs          # tail everything
make clean         # stop and delete volumes
```

## The DAG today

Seventeen nodes, four stages, two gates, six waves:

```
1.1.1 offer_economics        1.2.1 historical_performance
1.1.2 icp_profile            1.2.2 search_term_pnl
1.1.3 negative_icp           1.2.3 failed_experiments
1.1.4 market_coverage     ← 1.1.2
1.1.5 compliance_guardrails ← 1.1.1                  ⛳ gate → approver (legal)

1.3.1 competitor_set        ← 1.1.2, 1.2.2
1.3.2 creative_corpus       ← 1.3.1
1.3.3 spend_estimation      ← 1.3.2, 1.3.1
1.3.4 differentiation_claim ← 1.3.2, 1.1.1, 1.1.5    ⛳ gate → approver (marketing)

1.4.1 keyword_universe      ← 1.1.1, 1.2.2, 1.3.2
1.4.2 intent_classification ← 1.4.1
1.4.3 demand_metrics        ← 1.4.1
1.4.4 negative_blocklist    ← 1.4.2, 1.1.3, 1.2.2
1.4.5 keyword_to_page_map   ← 1.4.2, 1.4.3
```

Four things about them are worth knowing before reading the code.

**No node writes a number.** PRD §18 law 3 puts every CPA, ROAS, share-of-
revenue, overlap score, spend range, seasonality index and relevance score in
`nodes/frames.py`, `nodes/pnl.py`, `nodes/creatives.py` and `nodes/keywords.py`.
Most nodes therefore run in two halves: Python computes the table, the model is
shown it and asked only for labels, and `reason()` merges the labels onto the
computed rows. A model that miscopies a cost figure cannot put a wrong number in
the report, because the figure never passes through it. Two nodes — `1.4.3` and
`1.4.4` — call no model at all, because a join and a merge are not questions.

**An empty source is an answer.** A node with no evidence returns empty arrays
without calling a model at all, and records what it could not read in its
`coverage` field (PRD §16: never hallucinate history).

**Big jobs are batched, and say how they went.** `1.3.2` reads its corpus 25 ads
at a time and `1.4.2` labels terms 100 at a time, through `nodes/batching.py`.
A minority of failed batches degrades rather than fails, and the output carries
`batches`, `failed_batches` and `unread_ads` — a node returning a fifth of its
work must not look like one that finished.

**Estimates state their method.** `1.3.3` never presents a spend figure as fact:
every range carries the `method` that produced it, the `basis` it used and a
confidence that drops to `low` when the only signal is how many ads we saw. With
no signal at all it returns `insufficient_evidence` rather than a number.

## Approval gates

Two of the seventeen nodes are gates: `1.1.5 compliance_guardrails` (legal —
what we may claim) and `1.3.4 differentiation_claim` (marketing — what we will
claim). They run in that order because 1.3.4 depends on 1.1.5: a differentiator
the approved guardrails prohibit is a disapproval waiting to happen, so the
second gate is shown the first one's decision rather than left to guess. A run
covering both therefore stops twice.

When a gate produces its proposal the
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

A run needs two things the API cannot invent: a project and an OpenRouter
credential. Both have screens now — `/` → **New project** → the setup wizard,
and Settings → **OpenRouter key**. The API underneath is below.

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

## Known limits of P4

- **Four of the twenty-one nodes do not exist yet.** Stage 1.5 (readiness) and
  the two report nodes are P5. A "full" run today is stages 1.1 through 1.4.
- **`overlap_basis` can never say `auction`.** PRD §10 names it first, but the
  Google Ads API exposes no auction-insights resource — it is a UI-only report.
  The value stays in the vocabulary so a future source slots in without a schema
  change; nothing emits it today, and `paid_keywords` and `serp` are what the
  evidence actually supports.
- **A creative's `screenshot_path` is the grid it was captured from**, not a crop
  of the ad. One full-page capture per advertiser is written to the worker's
  Volume through `StorageBackend`; serving it is P5's file server.
- **Evidence only appears if its source is reachable.** Nodes pull through
  `nodes/gather.py` when the evidence store has nothing of a kind and the node
  names a connector; without the secret — or without Chromium, for the
  Transparency Center — they degrade to empty and say so in `coverage`. That is
  correct behaviour and also a trap when testing: seed the evidence, or every
  assertion passes for the wrong reason.
- **Approval SLAs and reminders are P8.** `Approval.due_at` is left NULL, so
  nothing nags an approver; the inbox badge is the only prompt.
- The run console, approvals inbox and report viewer are P7. `make verify-p4`
  drives P4's whole acceptance path through the API, both gates included.
- `STORAGE_BACKEND=s3` raises `NotImplementedError` by design.

## The interface (P6)

Everything a workspace needs before a run has a screen. An admin can invite all
four roles, store and test keys, choose models, assign the three gates and
launch a run without touching the API; an operator sees the same app with the
two admin-only steps read-only rather than hidden behind a locked door.

| Screen | Route | Who |
| --- | --- | --- |
| Project list | `/` | anyone; **New project** for `project_write` |
| Overview | `/projects/{id}` | anyone |
| Setup wizard | `/projects/{id}/setup` | `project_write` |
| Run history | `/projects/{id}/runs` | anyone |
| Workspace settings | `/settings` | `settings_write` |
| Members | `/settings/members` | `settings_write` |
| Audit log | `/settings/audit` | `audit_read` |
| Account | `/account` | anyone |

Three things are worth knowing before changing any of it.

- **`requirements` is the server's answer to "can this run yet".** The wizard's
  last step and `POST /projects/{id}/runs` read the same field, so the screen
  cannot say *ready* while the API refuses the launch.
- **A save carries `If-Match`, not `If-Unmodified-Since`.** Every project
  response includes an opaque `version`; sending it back is what turns a lost
  update into a 412 and a prompt. The HTTP-date header is still accepted, and
  still cannot tell two saves in the same second apart — which is why `version`
  exists.
- **A credential goes in and never comes out.** There is no read endpoint and no
  update: replacing a key writes a new row, so a half-typed replacement cannot
  leave the old one partly overwritten. `POST /credentials/{id}/test` answers
  `200` with `ok:false` for a bad key — the request succeeded, the key did not.

## Known limits of P6

- The run console, report viewer, evidence explorer and approvals inbox are P7.
  `/approvals` and `/evidence` render what they are and when they arrive rather
  than 404ing out of a navigation item every role can see.
- **The overview shows setup readiness, not the report's `GO` / `NO-GO`.** That
  verdict is part of the `ResearchReport` contract, which P5 writes; reading it
  early would mean guessing at a key that does not exist yet.
- `GET /models` needs `settings_write`, because the only screen that consumes it
  is the admin-only routing step. An operator's read-only view of that step
  renders the ids already stored on the project.
- **SMTP is reported, not edited.** It is deployment configuration
  (`SMTP_HOST`, `SMTP_FROM`), so Settings says whether it works and what
  happens when it does not — invites degrade to a copyable link.
- The cost estimate is labelled `assumed` until three full runs have finished,
  then `measured`. It is never presented as a measurement it is not.
