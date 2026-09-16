# PRD: Paid Ads Research Agent (Stage 01 — Marketing Intelligence)

**Type:** Internal Tool PRD · **Owner:** Soham Sarker · **Version:** 1.1 · **Date:** 2026-09-16 **Status:** Ready to build **Revision 1.1:** single-user self-hosted → **single-workspace, multi-user** with authentication, RBAC, and real in-app approval routing. **Revision 1.2:** **self-managed Postgres** (own container/service, no managed DB vendor) and **Railway as the deployment target**. Changed: §0 (A7–A9), §5, §5.2 (new), §12, §13.1, §14, §15, §16, §17, §19, §20.

---

## 0\. Assumptions locked in this version

| \# | Assumption | Source |
| :---- | :---- | :---- |
| A1 | Stack \= Next.js 15 (TS) frontend \+ Python 3.12 FastAPI backend, separate services | Confirmed |
| A2 | Single LLM provider surface \= **OpenRouter** (one key, all models), per-task model routing | Confirmed |
| A3 | Deployment \= self-hosted (VPS via Docker Compose), **one workspace, many users**, email+password auth with invite-only accounts. Single deployment serves exactly one workspace — no multi-tenancy. | Confirmed |
| A4 | Data sources \= Google Ads API (own account), DataForSEO (keywords), Playwright crawl of Google Ads Transparency Center, CSV upload \+ website crawl | Confirmed |
| A5 | This PRD covers **Stage 01 Research only**. Stages 02–07 (planning, creative, launch, optimize) are out of scope but the output contract is designed to feed them. | Inferred from diagram |
| A6 | RBAC with 4 roles inside the one workspace. No multi-tenant, no billing, no public signup. | Confirmed |
| A7 | **Database \= self-managed Postgres 16 with pgvector.** A container we run — `pgvector/pgvector:pg16` locally in Compose, the same image as a Railway service backed by a Volume in production. **No Supabase, Neon, RDS or any managed Postgres vendor.** `DATABASE_URL` is the only coupling point. | Confirmed |
| A8 | **Deployment target \= Railway.** Five services in one Railway project: `web`, `api`, `worker`, `postgres`, `redis`. Docker Compose stays the local-dev mirror of that topology. | Confirmed |
| A9 | Railway has **no shared volumes between services** and an **ephemeral container filesystem**. All durable binary artifacts (creative screenshots, exports) live on one Volume owned by `worker`; `api` never touches disk. See §5.2. | Derived constraint |

---

## 1\. Executive Summary

Google Ads research at SDS Manager is manual: pulling account history, eyeballing competitor ads, guessing keyword demand, then writing a deck. It takes days, it's stale the moment it ships, and it's not reproducible.

The Paid Ads Research Agent is a self-hosted web app that runs a deterministic 21-node research DAG over four evidence sources — our Google Ads account, the Google Ads Transparency Center, a keyword API, and our own CRM/site data — and emits a versioned, citation-backed **Research Report** (JSON → Markdown → PDF/DOCX) that campaign planning consumes directly.

Every node is an LLM call with a strict Pydantic output schema, grounded only in retrieved evidence. Three nodes are human-approval gates (legal claims, differentiation claim, data-consent) routed by role — legal and the data officer log in and decide in-app. The app is one shared workspace with invite-only accounts and four roles. Its UI covers OpenRouter key management, per-task model routing, live run console, approvals inbox, evidence explorer, and one-click PDF/Word export.

Target: a full research cycle in **\< 45 minutes unattended**, re-runnable on a schedule.

---

## 2\. Problem & Current Workflow

| Step today | Owner | Time | Failure mode |
| :---- | :---- | :---- | :---- |
| Pull 24mo Google Ads performance, pivot in Sheets | Soham | 3–4h | Stale within a week, no search-term P\&L |
| Manually browse competitor ads in Transparency Center | Soham | 4–6h | Sampled, not exhaustive; no diffing over time |
| Keyword research in Keyword Planner \+ gut | Soham | 3h | No intent classification, no page mapping, no blocklist |
| Check landing pages / tracking | ad-hoc | 1h | Usually skipped; campaigns launch on broken conversion tracking |
| Write it up | Soham | 3h | One-off artifact, never regenerated |

**Total ≈ 14–17 hours per cycle, run roughly never.** Nothing is versioned, nothing is cited, nothing re-runs.

---

## 3\. Proposed Solution

A self-hosted web app (`ads-research-agent`) serving one shared workspace. An `operator` creates a **Project** (brand \+ product context \+ data sources), presses **Run Research**, and watches a DAG of 21 typed agent nodes execute against real evidence. Each node writes a validated JSON artifact and a set of `Evidence` rows with source URLs. A final synthesis node composes the Research Report. The three approval gates halt their branch and land in the assigned approver's inbox — legal, the data officer and the marketing lead each have their own login. Output exports as PDF and DOCX.

**Design principle: the LLM never sources facts.** Connectors source facts into an evidence store; LLM nodes only classify, cluster, rank, and write, with `evidence_ids` required on every claim. Unsupported claims fail schema validation.

---

## 4\. Users, Roles & Permissions

One workspace. Every account belongs to it. Invite-only — there is no public signup route.

| Role | Who | Frequency | Scope |
| :---- | :---- | :---- | :---- |
| `admin` | Soham | Daily | Everything: invite/remove users, set roles, manage credentials and model routing, delete projects |
| `operator` | Marketing team | Weekly | Create/edit projects, launch and cancel runs, upload CSVs, export reports. **Cannot** manage users or write credentials |
| `approver` | Legal, data officer, marketing lead | \~once per market | Decide approval gates routed to them, read reports and evidence. **Cannot** launch runs or edit projects |
| `viewer` | Wider team | Ad-hoc | Read reports and run history, export. No writes anywhere |

### 4.1 Permission matrix

| Action | admin | operator | approver | viewer |
| :---- | :---: | :---: | :---: | :---: |
| Read reports, evidence, run history | ✅ | ✅ | ✅ | ✅ |
| Export PDF/DOCX/CSV/JSON | ✅ | ✅ | ✅ | ✅ |
| Create / edit project | ✅ | ✅ | ❌ | ❌ |
| Launch / cancel / retry run | ✅ | ✅ | ❌ | ❌ |
| Upload CSV, connect data sources | ✅ | ✅ | ❌ | ❌ |
| Decide an approval gate | ✅ | ❌ | ✅ (own gates) | ❌ |
| Write credentials (API keys) | ✅ | ❌ | ❌ | ❌ |
| Set model routing & budget caps | ✅ | ❌ | ❌ | ❌ |
| Invite / remove users, change roles | ✅ | ❌ | ❌ | ❌ |
| Read audit log | ✅ | ❌ | ❌ | ❌ |

**Rule:** credential ciphertext is never readable by any role, `admin` included. Keys are write-and-test-only.

---

## 5\. Architecture

┌────────────────────────────────────────────────────────────────┐

│  apps/web  —  Next.js 15 App Router, TS, Tailwind, shadcn/ui   │

│  :3000   TanStack Query · Zustand · EventSource (SSE)           │

└───────────────────────────┬────────────────────────────────────┘

                            │ /api/v1/\* rewrite → private network

┌───────────────────────────▼────────────────────────────────────┐

│  apps/api  —  FastAPI (Python 3.12, uvicorn)                   │

│   ├─ auth/           sessions, Argon2id, invites, RBAC deps    │

│   ├─ orchestrator/   DAG executor, node registry, checkpoints  │

│   ├─ nodes/          21 agent nodes (Pydantic in/out)          │

│   ├─ connectors/     google\_ads · dataforseo · transparency ·  │

│   │                  web\_crawler · csv\_ingest                  │

│   ├─ llm/            OpenRouter gateway, router, cost ledger   │

│   ├─ evidence/       normalization, dedupe, embedding, search  │

│   ├─ export/         md → pdf (WeasyPrint) · docx (python-docx)│

│   └─ storage/        artifact I/O via StorageBackend (Volume)  │

└────────┬──────────────────┬────────────────────┬───────────────┘

         │                  │                    │

   ┌─────▼─────┐      ┌─────▼─────┐        ┌─────▼──────┐

   │ Postgres  │      │   Redis   │        │  /data     │

   │ 16 \+      │      │  arq queue│        │  volume    │

   │ pgvector  │      │  sessions │        │  (worker)  │

   └───────────┘      └───────────┘        └────────────┘

**Why two services:** Playwright, pandas, and the Google Ads SDK are Python-native; the UI needs React streaming. Split on that line, nothing else.

**Request path (both environments):** the browser only ever talks to `web`. Next.js `rewrites()` proxies `/api/v1/*` to the API over the private network. **`api` and `worker` have no public ingress.** This is not cosmetic — it makes the session cookie first-party, so `SameSite=Lax` works and `SameSite=None` is never needed (see §5.2).

### 5.1 Repo layout

ads-research-agent/

├─ docker-compose.yml          \# local mirror of the Railway topology

├─ .env.example

├─ railway/

│  ├─ README.md                \# service-by-service provisioning runbook

│  └─ variables.md             \# every env var \+ its Railway reference expression

├─ apps/

│  ├─ api/

│  │  ├─ Dockerfile            \# api service  (slim, no browser)

│  │  ├─ Dockerfile.worker     \# worker service (playwright base image)

│  │  ├─ railway.json          \# api: build, healthcheckPath, preDeployCommand

│  │  ├─ railway.worker.json   \# worker: build, restartPolicy, no healthcheck

│  │  ├─ pyproject.toml        \# uv-managed

│  │  ├─ alembic/

│  │  └─ src/agent/

│  │     ├─ main.py            \# FastAPI app factory

│  │     ├─ config.py          \# pydantic-settings

│  │     ├─ db/  models.py session.py

│  │     ├─ crypto.py          \# AES-GCM secret vault

│  │     ├─ auth/  passwords.py sessions.py invites.py deps.py rbac.py

│  │     ├─ notify/ email.py (SMTP, optional) inbox.py

│  │     ├─ llm/  gateway.py router.py ledger.py prompts/

│  │     ├─ orchestrator/  dag.py executor.py registry.py state.py

│  │     ├─ nodes/  n1\_1\_\*.py … n1\_6\_synthesis.py

│  │     ├─ connectors/  base.py google\_ads.py dataforseo.py

│  │     │               transparency.py web\_crawler.py csv\_ingest.py

│  │     ├─ evidence/  store.py normalize.py search.py

│  │     ├─ storage/  backend.py local.py s3.py  \# artifact I/O abstraction

│  │     ├─ fileserver.py      \# worker-only internal file server (private net)

│  │     ├─ export/  markdown.py pdf.py docx.py templates/

│  │     ├─ api/  routes\_\*.py  sse.py

│  │     └─ worker.py          \# arq worker entrypoint

│  └─ web/

│     ├─ Dockerfile            \# next build \--output standalone

│     ├─ railway.json

│     ├─ next.config.ts        \# rewrites(): /api/v1/\* \-\> API private URL

│     ├─ app/(routes)/…

│     ├─ components/

│     ├─ lib/api.ts  lib/sse.ts  lib/schemas.ts (zod, mirrors Pydantic)

│     └─ styles/tokens.css

└─ packages/contracts/         \# JSON Schema emitted from Pydantic, consumed by zod

### 5.2 Deployment — Railway

One Railway project, five services. Docker Compose locally is the mirror of this; nothing is environment-specific except values.

| Service | Source | Public? | Notes |
| :---- | :---- | :---- | :---- |
| `web` | `apps/web/Dockerfile` | **Yes** — the only public domain | Next.js standalone. Must bind `process.env.PORT`. Owns the custom domain |
| `api` | `apps/api/Dockerfile` | No | uvicorn on `PORT`, bound to `::` (Railway private network is **IPv6-only**) |
| `worker` | `apps/api/Dockerfile.worker` | No | arq worker \+ Playwright \+ the Volume \+ the internal file server |
| `postgres` | image `pgvector/pgvector:pg16` | No | Volume at `/var/lib/postgresql/data`. **Self-managed — not a managed DB add-on** |
| `redis` | image `redis:7-alpine` | No | Sessions, arq queue, run locks, rate limits. Volume optional (`appendonly yes` recommended) |

#### Wiring rules

1. **Env vars are Railway reference expressions**, never copy-pasted values: `DATABASE_URL=${{postgres.DATABASE_URL}}`, `REDIS_URL=${{redis.REDIS_URL}}`, `API_INTERNAL_URL=http://${{api.RAILWAY_PRIVATE_DOMAIN}}:8000`, `WORKER_INTERNAL_URL=http://${{worker.RAILWAY_PRIVATE_DOMAIN}}:8081`. `APP_ENCRYPTION_KEY` and `BOOTSTRAP_ADMIN_*` are set once, by hand, and never logged.  
2. **Bind `::`, not `0.0.0.0`.** Railway's private network is IPv6-only; a service bound to IPv4 is unreachable from its siblings. `uvicorn --host :: --port $PORT`.  
3. **Never hardcode a port.** Read `$PORT` in `web` and `api`. The file server on `worker` is fixed at `8081` because nothing external reaches it.  
4. **Same-origin auth.** `next.config.ts` rewrites `/api/v1/:path*` → `${API_INTERNAL_URL}/api/v1/:path*`. The browser sees one origin, so the session cookie is first-party `SameSite=Lax; Secure`. Railway terminates TLS on the `web` domain, so `Secure` works out of the box. **Do not expose `api` publicly** — doing so would force `SameSite=None` and widen the attack surface for nothing.  
5. **SSE through the proxy.** The rewrite must stream: set `Cache-Control: no-cache, no-transform` and `X-Accel-Buffering: no` on the SSE response, and keep the 15s heartbeat — Railway's edge idles out silent connections.  
6. **Migrations run as `preDeployCommand`** in `apps/api/railway.json`: `alembic upgrade head`. Never on app startup — `api` and `worker` boot concurrently and would race.  
7. **Healthcheck:** `api` sets `healthcheckPath: /api/v1/health`, `healthcheckTimeout: 60`. `worker` has none (no HTTP ingress); it uses `restartPolicyType: ON_FAILURE`, `restartPolicyMaxRetries: 10`.  
8. **Bootstrap** runs on `api` startup, guarded by a Postgres advisory lock so two replicas cannot both create the admin.

#### Artifact storage (the constraint that shapes this)

Railway Volumes attach to exactly one service, and container filesystems are wiped on every deploy. Therefore:

- A single Volume mounts at `/data` on **`worker`**. `STORAGE_DIR=/data`.  
- `worker` is the only process that writes creative screenshots and generated exports.  
- **Export generation is an arq job, not an API request.** `POST /reports/{id}/export` enqueues and returns `202 {job_id}`; the client polls `GET /exports/{job_id}` or listens on the run SSE channel. WeasyPrint and python-docx run in `worker`.  
- `worker` runs `fileserver.py` — a minimal FastAPI app on `:8081`, private network only, serving `/files/{path}` with path-traversal rejection and a short-lived HMAC token issued by `api`. `GET /exports/{id}/download` on `api` streams from it.  
- `storage/backend.py` defines `StorageBackend.put/get/open/delete`. `local.py` is the default for both Compose and Railway. `s3.py` (Cloudflare R2, any S3-compatible) is behind `STORAGE_BACKEND=s3` for the day artifacts outgrow a Volume. **No call site touches a filesystem path directly.**

#### Scheduling, backups, resources

- **Schedules** are driven by an `arq` cron in `worker` that polls the `Schedule` table every minute. Do not use Railway Cron Jobs — schedules are DB state edited in the UI, not deploy config.  
- **Backups:** a nightly `arq` job runs `pg_dump -Fc` into `/data/backups/`, keeps 14 days, and writes an `AuditLog` row. Railway Volume snapshots are the second line, not the first. Restore procedure documented in `railway/README.md`.  
- **Resources:** `worker` needs ≥ 2 GB RAM (Chromium). Launch with `--disable-dev-shm-usage` — the container `/dev/shm` is 64 MB and Chromium will crash without it. `api` and `web` run comfortably at 512 MB.  
- **Cost:** Railway bills on usage. `worker` is idle between runs; keep Playwright concurrency at 1 (already specified in §9.2) so peak memory stays inside one instance.

---

## 6\. Data Model (Postgres, SQLAlchemy 2.0 \+ Alembic)

Workspace(id uuid pk, name, created\_at, settings jsonb)

          \# SINGLETON. Created by first-run bootstrap. Exactly one row, enforced

          \# by a partial unique index. Every other table hangs off it.

User(id uuid pk, workspace\_id fk, email citext unique, name,

     password\_hash text,            \# Argon2id, never returned by any endpoint

     role enum\[admin|operator|approver|viewer\],

     status enum\[invited|active|disabled\],

     last\_login\_at, created\_at, updated\_at)

Invite(id uuid pk, workspace\_id fk, email citext, role, token\_hash text,

       invited\_by fk-\>User, expires\_at, accepted\_at null, created\_at)

       UNIQUE(workspace\_id, email) WHERE accepted\_at IS NULL

AuditLog(id, workspace\_id fk, actor\_id fk-\>User null, action text,

         target\_type text, target\_id uuid null, meta jsonb, ip inet,

         created\_at)   \# append-only; no UPDATE/DELETE grant on this table

Project(id uuid pk, workspace\_id fk, created\_by fk-\>User, name, domain,

        created\_at, updated\_at,

        product\_context jsonb,      \# free-text \+ structured offer data

        markets jsonb,              \# \[{country, language, currency}\]

        settings jsonb)             \# model routing overrides

Credential(id, workspace\_id fk, scope enum\[workspace|project|user\],

           project\_id fk null, user\_id fk null,   \# per scope, CHECK-enforced

           kind enum\[openrouter|google\_ads|dataforseo|smtp\],

           ciphertext bytea, nonce bytea, meta jsonb,

           created\_by fk-\>User, created\_at, last\_tested\_at, last\_test\_ok bool)

           \# AES-256-GCM, key from APP\_ENCRYPTION\_KEY env.

           \# Resolution order at call time: user \> project \> workspace.

           \# Never returned in API. meta holds masked hints only (last4, acct id).

Run(id, workspace\_id fk, project\_id fk, triggered\_by fk-\>User null,

    trigger enum\[manual|schedule\], status enum\[queued|running|

    awaiting\_approval|succeeded|failed|cancelled\], mode enum\[full|partial\],

    node\_filter jsonb, started\_at, finished\_at, cost\_usd numeric,

    token\_in int, token\_out int, error jsonb, parent\_run\_id fk null)

    \# triggered\_by NULL only when trigger=schedule

NodeRun(id, run\_id fk, node\_id text, status, attempt int,

        input\_hash text, output jsonb, evidence\_ids uuid\[\],

        model text, token\_in, token\_out, cost\_usd, latency\_ms,

        started\_at, finished\_at, error jsonb)

        UNIQUE(run\_id, node\_id, attempt)

Evidence(id, project\_id fk, run\_id fk, source enum\[google\_ads|dataforseo|

         transparency|web|csv|derived\], source\_url text null,

         fetched\_at, kind text, payload jsonb, content\_text text,

         embedding vector(1536) null, hash text)

         UNIQUE(project\_id, hash)   \# dedupe across runs

Approval(id, run\_id fk, node\_id, status enum\[pending|approved|rejected|expired\],

         required\_role enum\[admin|approver\], assignee\_id fk-\>User null,

         proposal jsonb, edited\_proposal jsonb null, decision\_note text,

         decided\_by fk-\>User null, decided\_at, due\_at null, created\_at)

         \# assignee\_id null \=\> any user holding required\_role may decide

Report(id, run\_id fk unique, schema\_version text, payload jsonb,

       markdown text, created\_at)

Export(id, report\_id fk, format enum\[pdf|docx|md|json\], path text,

       bytes int, created\_at)

Schedule(id, workspace\_id fk, project\_id fk, created\_by fk-\>User,

         cron text, timezone text, enabled bool, last\_run\_id fk, next\_at)

Indexes: `Evidence(project_id, source, kind)`, `Evidence` HNSW on `embedding`, `NodeRun(run_id, node_id)`, `Run(project_id, started_at desc)`, `User(workspace_id, email)`, `AuditLog(workspace_id, created_at desc)`, `Approval(status, required_role)`.

Every read and write is scoped by `workspace_id` through one `WorkspaceScopedRepo` base class. No route builds a query without it.

---

## 6.1 Authentication & Authorization

### Authentication

1. **Bootstrap.** On first boot with zero users, the app creates the Workspace singleton and the first `admin` from `BOOTSTRAP_ADMIN_EMAIL` / `BOOTSTRAP_ADMIN_PASSWORD`. `POST /auth/bootstrap` returns `409` forever after. No other path creates a user without an invite.  
2. **Passwords.** Argon2id via `argon2-cffi` (`time_cost=3, memory_cost=65536, parallelism=4`). Minimum 12 chars, rejected against a common-password list. Transparent rehash on login when params change.  
3. **Sessions.** Server-side in Redis at `session:{sid}`, 30-day sliding TTL, 12-hour idle timeout. Cookie `HttpOnly; Secure; SameSite=Lax; Path=/`, opaque 256-bit `sid`. **No JWTs** — sessions must be revocable instantly.  
4. **CSRF.** Double-submit token: `X-CSRF-Token` header must match the `csrf` cookie on every state-changing request. SSE `GET` exempt.  
5. **Rate limiting.** `POST /auth/login`: 5 attempts / 15 min per (email \+ IP), then lockout \+ `AuditLog` row. Constant-time response whether or not the email exists.  
6. **Invites.** Admin creates → 32-byte token, only its SHA-256 stored, 7-day expiry, single use. Delivered by SMTP when configured; otherwise the UI shows a copyable link. Accepting sets the password and flips `status=active`.  
7. **Disable, never delete.** Removing a user sets `status=disabled` and revokes all their sessions; their `created_by` / `decided_by` history stays intact.

### Authorization

1. One FastAPI dependency: `require(Permission)`. Every route declares one — a route without it fails a CI check that walks the router modules.  
2. Permissions are an enum (`READ`, `PROJECT_WRITE`, `RUN_EXECUTE`, `CREDENTIAL_WRITE`, `SETTINGS_WRITE`, `APPROVAL_DECIDE`, `USER_MANAGE`, `AUDIT_READ`) mapped to roles in a single `rbac.py` table. Routes reference permissions, never role strings.  
3. Gate decisions require `user.role ∈ {approval.required_role, admin}` **and**, when `assignee_id` is set, identity match.  
4. The last active `admin` cannot be demoted or disabled — enforced in a DB transaction, not just the UI.  
5. Every user/credential/project/schedule write, every approval decision, and every run launch writes an `AuditLog` row **in the same transaction** as the change.

### Concurrency (new in multi-user)

- **Run lock.** Redis `SETNX project:{id}:run_lock`, 2-hour TTL. A second launch returns `409` naming the user and run already in flight.  
- **Optimistic edits.** `Project` and `Schedule` carry `updated_at`; `PATCH` requires `If-Unmodified-Since` and returns `412` on a stale write, so two operators can't silently overwrite each other.  
- **Approval race.** Decisions are `UPDATE … WHERE status='pending'`; the loser gets `409` and the UI shows who decided.

---

## 7\. Orchestration Engine

### 7.1 Node contract

Every node implements one interface. No exceptions.

class NodeSpec(BaseModel):

    id: str                       \# "1.3.2"

    name: str

    stage: str                    \# "1.3"

    depends\_on: list\[str\]

    gate: bool \= False            \# human approval required on output

    required\_role: Role | None \= None   \# who may decide this gate

    task\_class: TaskClass         \# EXTRACT | CLASSIFY | SYNTHESIZE | CRITIQUE

    input\_model: type\[BaseModel\]

    output\_model: type\[BaseModel\]

    connectors: list\[str\] \= \[\]

class Node(Protocol):

    spec: NodeSpec

    async def gather(self, ctx: RunContext) \-\> list\[Evidence\]: ...

    async def reason(self, ctx: RunContext, ev: list\[Evidence\]) \-\> BaseModel: ...

`executor.run_node()` does: resolve deps → `gather()` → build grounded prompt → `llm.complete_structured(output_model)` → validate `evidence_ids ⊆ gathered ids` → persist `NodeRun` → emit SSE. Nodes are pure w.r.t. `(input_hash, node version)`; identical hash \+ `--reuse-cache` skips execution.

### 7.2 Execution semantics

1. **DAG** declared in `orchestrator/dag.py` as static edge list; validated acyclic at import.  
2. **Topological wavefront** execution; independent nodes in a wave run concurrently, `asyncio.Semaphore(4)`.  
3. **Retries:** 3 attempts, exponential backoff `2^n * 1.5s`, jitter. Schema-validation failure triggers one **repair pass** (validation error fed back to the model) before counting as an attempt.  
4. **Checkpointing:** every `NodeRun` persisted on completion. Resume \= re-enter executor, skip `succeeded` nodes.  
5. **Gates:** a `gate=True` node writes `Approval(pending, required_role, assignee_id)`, sets `Run.status=awaiting_approval`, and halts that branch only — other branches keep running. The assignee is notified (in-app inbox \+ SMTP email when configured). `POST /approvals/{id}` by an authorized user resumes the branch; the decision is audit-logged with the deciding user's id.  
6. **Cancellation:** cooperative via Redis key `run:{id}:cancel`, checked between nodes.  
7. **Budget guard:** `Run` aborts if `cost_usd > project.settings.max_run_cost_usd` (default 15.00).

### 7.3 Streaming

`GET /runs/{id}/events` → `text/event-stream`. Event types: `run.status` · `node.started` · `node.progress` (free-text status line) · `node.tokens` · `node.completed` · `node.failed` · `approval.required` · `run.completed`. Heartbeat comment every 15s. Frontend reconnects with `Last-Event-ID`.

---

## 8\. LLM Gateway (OpenRouter)

POST https://openrouter.ai/api/v1/chat/completions

Headers: Authorization: Bearer \<key\>, HTTP-Referer, X-Title

**Requirements**

1. **Structured output is mandatory.** Use `response_format={"type":"json_schema","json_schema":{...,"strict":true}}` with the schema emitted by `output_model.model_json_schema()`. If the routed model lacks strict JSON-schema support, fall back to tool-call forcing; if that also fails, fall back to prompt-with-schema \+ `json_repair` \+ Pydantic validate.  
2. **Model routing by task class.** `llm/router.py` maps `TaskClass → model_id`, overridable per project in Settings UI.

| Task class | Default model | Rationale |
| :---- | :---- | :---- |
| `EXTRACT` | `google/gemini-2.5-flash` | High-volume, cheap, long context |
| `CLASSIFY` | `anthropic/claude-haiku-4.5` | Fast, cheap, deterministic |
| `SYNTHESIZE` | `anthropic/claude-opus-4.6` | Report quality, long-form reasoning |
| `CRITIQUE` | `openai/gpt-5.2` | Cross-family adversarial review |

Model IDs are **not hardcoded** — hydrate the picker from `GET /api/v1/models` at runtime and persist the chosen ID. Defaults above are seed values only. 3\. **Cost ledger:** read `usage` from the response, multiply by pricing from `GET /api/v1/models`, write to `NodeRun` and roll up to `Run`. 4\. **Concurrency \+ rate limits:** shared `AsyncLimiter`, respect `429` \+ `Retry-After`. 5\. **Prompt caching:** mark stable system blocks and the project context block as cacheable where the provider supports it. 6\. **Never log the key.** Redact `Authorization` in all log formatters.

---

## 9\. Connectors

All connectors implement `BaseConnector.fetch(params) -> list[EvidenceDraft]` and are independently testable with recorded VCR cassettes.

### 9.1 `google_ads` — own account history

- `google-ads` Python SDK, OAuth2 installed-app flow, refresh token stored in `Credential`.  
- GAQL pulls, 24-month window, segmented by month:  
  - `campaign` \+ `metrics` (cost, conversions, conv\_value, impr, clicks, CTR, CPA, ROAS)  
  - `search_term_view` (search term × campaign × cost × conversions) → **search-term P\&L**  
  - `ad_group_ad` (RSA assets \+ performance labels) → prior creative  
  - `change_event` (last 90d) → what was tried  
  - `keyword_view` with `metrics.search_impression_share`  
- Emits `kind ∈ {campaign_perf, search_term_pnl, creative_history, change_log}`.  
- **Fallback if API access is not yet provisioned:** CSV import of the same reports from the Google Ads UI, mapped to the identical `Evidence.kind`. The rest of the pipeline is source-agnostic.

### 9.2 `transparency` — competitor \+ own live ads (browser)

- Playwright Chromium, persistent context, `playwright-stealth`, 1 concurrent page, randomized 2–5s delays, `--disable-blink-features=AutomationControlled`.  
- Flow: `adstransparency.google.com` → search advertiser → filter region \+ format → scroll-paginate the ad grid → for each ad capture `{advertiser, ad_id, format, first_shown, last_shown, creative_text, image_url, destination_url, regions}` → screenshot written through `StorageBackend` to `creatives/{run_id}/` under `$STORAGE_DIR`.  
- Also crawl competitor landing pages linked from ads (offer, pricing, CTA, form fields).  
- Resilience: selectors in a single `selectors.py` map; on selector miss, fall back to DOM-text heuristics and raise `ConnectorDegraded` (run continues, report flags degraded coverage). Save failure HTML to `debug/` under `$STORAGE_DIR` for selector repair.  
- Politeness: respect robots where applicable, per-domain rate limit, no login-walled content.

### 9.3 `dataforseo` — demand

- Endpoints: `keywords_for_site`, `keywords_for_keywords`, `search_volume`, `keyword_ideas`, `serp/google/organic` (for SERP ownership), `dataforseo_labs/competitors_domain`.  
- Returns volume, CPC, competition index, 12-month `monthly_searches` → seasonality.  
- Emits `kind ∈ {keyword_metrics, serp_snapshot, domain_competitor}`.  
- Adapter interface is provider-neutral (`KeywordProvider`) so SEMrush/Ahrefs can be swapped without touching nodes.

### 9.4 `web_crawler` — own site \+ landing pages

- `httpx` \+ `selectolax`; `sitemap.xml` discovery, max 500 URLs, depth 3\.  
- Per page: title, H1, meta description, primary CTA, form fields, word count, canonical, schema.org blocks.  
- Lighthouse-equivalent signals via Playwright: LCP, CLS, TBT, mobile viewport check, HTTPS, visible trust markers (regex for certification/ISO/customer-logo blocks).

### 9.5 `csv_ingest` — CRM

- Accepts closed-won and closed-lost exports. Column mapping UI (map source column → canonical field: `account_name, industry, employee_count, country, deal_value, close_reason, source, created_at`).  
- Validation: type coercion, required-field check, row-level error report. Store canonical rows as `Evidence(kind=crm_won|crm_lost)`.

---

## 10\. Agent Nodes (the research DAG)

`gate=true` nodes are marked **⛳** and declare a `required_role` plus an optional default `assignee_id`, set per project in the setup wizard. All others run unattended.

### Stage 1.1 — Understand our own business

| ID | Node | Inputs | Output schema (core fields) |
| :---- | :---- | :---- | :---- |
| 1.1.1 | `offer_economics` | product\_context, crm\_won | `products[]{name, price_model, acv, gross_margin_pct, delivery_cost_notes}`, `ltv_estimate`, `target_cac`, `payback_months` |
| 1.1.2 | `icp_profile` | crm\_won | `segments[]{label, firmographics, triggers, jobs_to_be_done, share_of_revenue_pct, evidence_ids}` |
| 1.1.3 | `negative_icp` | crm\_lost | `exclusions[]{persona, disqualifier, observable_signal, suggested_negative_terms[]}` |
| 1.1.4 | `market_coverage` | project.markets, crm\_won, google\_ads | `markets[]{country, language, demand_months[], dead_months[], currency}` |
| 1.1.5 ⛳ | `compliance_guardrails` | product\_context, prior disapprovals (`change_log`), policy docs | `prohibited_claims[]`, `required_disclaimers[]`, `regulated_terms[]{term, rule}`, `confidence` — gate → `approver` (legal) |

### Stage 1.2 — Learn from what we already ran

| ID | Node | Output |
| :---- | :---- | :---- |
| 1.2.1 | `historical_performance` | `winners[]{campaign, metric_delta, period}`, `losers[]`, `structural_findings[]` |
| 1.2.2 | `search_term_pnl` | `profitable_terms[]{term, cost, conv, cpa, roas}`, `wasteful_terms[]{term, cost, conv=0, recommended_action}` — **computed in pandas, LLM only labels/clusters** |
| 1.2.3 | `failed_experiments` | `tried_and_failed[]{what, when, outcome, do_not_repeat_reason}` |

### Stage 1.3 — Study the competition

| ID | Node | Output |
| :---- | :---- | :---- |
| 1.3.1 | `competitor_set` | \`competitors\[\]{domain, name, overlap\_score, overlap\_basis\[auction |
| 1.3.2 | `creative_corpus` | `ads[]{advertiser, headline, description, offer, angle, proof_type, cta, landing_url, first_seen, last_seen, screenshot_path}` \+ `message_clusters[]{theme, frequency, advertisers[]}` |
| 1.3.3 | `spend_estimation` | `estimates[]{competitor, est_monthly_spend_range, method, confidence, peak_months[]}` — must state method; never present as fact |
| 1.3.4 ⛳ | `differentiation_claim` | `whitespace[]{claim, why_unsaid, our_proof, risk}`, `recommended_claim`, `substantiation_required[]` — gate → `approver` (marketing lead) |

### Stage 1.4 — Find the demand

| ID | Node | Output |
| :---- | :---- | :---- |
| 1.4.1 | `keyword_universe` | `keywords[]{term, source, market}` — target ≥ 2,000 deduped seeds |
| 1.4.2 | `intent_classification` | each term → `intent ∈ {transactional, commercial_investigation, informational, navigational, irrelevant}` \+ `funnel_stage` \+ `confidence`. Batched 100/call, `CLASSIFY` class |
| 1.4.3 | `demand_metrics` | `term, volume, cpc_low, cpc_high, competition, seasonality_index[12], trend_yoy` — joined from DataForSEO, not generated |
| 1.4.4 | `negative_blocklist` | `negatives[]{term, match_type, reason, source ∈ {lost_reasons, wasteful_terms, intent_irrelevant}}` |
| 1.4.5 | `keyword_to_page_map` | `mapping[]{term_cluster, best_url, relevance_score, verdict ∈ {good_fit, weak_fit, gap}}`, `content_gaps[]{cluster, required_page_type}` |

### Stage 1.5 — Check we are actually ready

| ID | Node | Output |
| :---- | :---- | :---- |
| 1.5.1 | `landing_page_audit` | `pages[]{url, lcp_ms, cls, mobile_ok, form_fields_count, trust_signals[], issues[], severity}` |
| 1.5.2 | `tracking_probe` | `conversion_actions[]{name, status, last_conversion_at, staleness_days}`, `synthetic_check{fired_at, observed_in_ads_api, latency_min, verdict}`, `alerts[]` |
| 1.5.3 ⛳ | `audience_consent_check` | `lists[]{name, size, consent_basis, markets_allowed[], usable bool, blocker}` — gate → `approver` (data officer) |
| 1.5.4 | `opportunity_sizing` | `scenarios[]{budget_usd_month, est_clicks, est_conv, est_cpa, est_revenue, assumptions[], confidence_interval}` — arithmetic in Python, LLM writes assumptions only |

### Stage 1.6 — Report

| ID | Node | Output |
| :---- | :---- | :---- |
| 1.6.1 | `report_synthesis` | Full `ResearchReport` object (§11), `SYNTHESIZE` class |
| 1.6.2 | `report_critique` | `CRITIQUE` class, different model family. Checks: unsupported claims, contradictions between sections, missing evidence\_ids, launch-readiness verdict consistency. Returns `issues[]{severity, section, fix}`. If any `severity=blocking`, 1.6.1 re-runs **once** with the critique appended. |

**DAG edges**

1.1.1←{}  1.1.2←{}  1.1.3←{}  1.1.4←{1.1.2}  1.1.5←{1.1.1}

1.2.1←{}  1.2.2←{}  1.2.3←{}

1.3.1←{1.1.2,1.2.2}  1.3.2←{1.3.1}  1.3.3←{1.3.2}  1.3.4←{1.3.2,1.1.1,1.1.5}

1.4.1←{1.1.1,1.2.2,1.3.2}  1.4.2←{1.4.1}  1.4.3←{1.4.1}

1.4.4←{1.4.2,1.1.3,1.2.2}  1.4.5←{1.4.2,1.4.3}

1.5.1←{1.4.5}  1.5.2←{}  1.5.3←{1.1.4}  1.5.4←{1.4.3,1.1.1,1.2.1}

1.6.1←{all}  1.6.2←{1.6.1}

---

## 11\. Report Contract

`ResearchReport` is the handoff artifact to Stage 02\. Versioned with `schema_version`.

class Claim(BaseModel):

    statement: str

    evidence\_ids: list\[UUID\] \= Field(min\_length=1)

    confidence: Literal\["high","medium","low"\]

class ResearchReport(BaseModel):

    schema\_version: Literal\["1.0"\]

    project\_id: UUID; run\_id: UUID; generated\_at: datetime

    executive\_summary: str                       \# ≤ 250 words

    launch\_readiness: Literal\["go","go\_with\_fixes","no\_go"\]

    launch\_blockers: list\[Claim\]

    business\_context: BusinessContext            \# 1.1.\*

    account\_learnings: AccountLearnings          \# 1.2.\*

    competitive\_landscape: CompetitiveLandscape  \# 1.3.\*

    demand\_map: DemandMap                        \# 1.4.\*

    readiness: Readiness                         \# 1.5.\*

    priced\_keyword\_list: list\[PricedKeyword\]     \# export-ready, CSV-able

    recommended\_next\_actions: list\[Claim\]

    open\_questions: list\[str\]

    degraded\_sources: list\[str\]                  \# connectors that partially failed

    cost\_usd: float

`markdown` is rendered from this object by a deterministic Jinja2 template — **the LLM does not write the final markdown**, it fills the object. This guarantees PDF/DOCX/JSON never diverge.

---

## 12\. Export System

| Format | Library | Notes |
| :---- | :---- | :---- |
| **PDF** | WeasyPrint (HTML+CSS Paged Media) | A4, cover page w/ project \+ date \+ run id, auto TOC via CSS counters, running header/footer with page numbers, charts pre-rendered as inline SVG (matplotlib `svg` backend), competitor creative screenshots embedded |
| **DOCX** | `python-docx` \+ a `reference.docx` style template | Real Heading 1–3 styles (so Word's TOC field works), native Word tables, `TOC` field code inserted and marked dirty for update-on-open |
| **MD** | Jinja2 | Source of truth |
| **JSON** | `report.payload` | Machine handoff to Stage 02 |
| **CSV** | `priced_keyword_list` | Direct import to Google Ads Editor |

`POST /reports/{id}/export?format=pdf|docx|md|json|csv` **enqueues an arq job** and returns `202 {job_id}` — generation runs in `worker`, which owns the Volume (§5.2). Output lands at `$STORAGE_DIR/exports/{run_id}/`. The client polls `GET /exports/{job_id}` (`queued|running|ready|failed`) or listens on the run SSE channel for `export.ready`. `GET /exports/{id}/download` on `api` streams the file from the worker's internal file server with the correct `Content-Disposition`.

**Acceptance:** PDF ≤ 15 MB for a run with 200 competitor creatives; DOCX opens in Word 2019+ with a working TOC after F9; both contain every section present in the JSON.

---

## 13\. Frontend Specification

### 13.1 Stack

Next.js 15 App Router · TypeScript strict · Tailwind v4 · shadcn/ui · TanStack Query v5 · Zustand (run-console state) · `zod` schemas generated from the API's JSON Schema (`packages/contracts`) · `recharts` for charts · `next-themes` for dark mode.

**API access:** the frontend calls **relative paths only** (`/api/v1/...`). There is no `NEXT_PUBLIC_API_URL` — `next.config.ts` rewrites to the API over Railway's private network (§5.2), so the app is same-origin in every environment. Hardcoding an absolute API URL anywhere in `apps/web` is a bug.

**Auth in the frontend:** session cookie only — no token in JS, nothing in `localStorage`. Next.js `middleware.ts` guards every route except `/login`, `/invite/[token]`. A root-layout server component fetches `GET /auth/me` and puts `{id, name, email, role, permissions[]}` into a `SessionProvider`. A `<Can permission="RUN_EXECUTE">` wrapper hides unauthorized controls — **UI hiding is cosmetic; the API is the enforcement boundary.** A `401` redirects to `/login?next=`; a `403` renders an inline no-access state, never a redirect loop.

### 13.2 Design tokens

- Neutral base (`zinc`), single accent `#2563EB`. Status: running `blue-500`, success `emerald-500`, gate `amber-500`, failed `rose-500`, skipped `zinc-400` — **exactly the green/amber language of the stage diagram.**  
- Type: Inter (UI), `ui-monospace` for IDs/metrics. Scale 12/14/16/20/28/36.  
- Radius 10px, 8px spacing grid, shadow only on overlays.  
- Light \+ dark, defined as CSS variables on `:root` and `[data-theme="dark"]`.

### 13.3 Routes

| Route | Auth | Purpose |
| :---- | :---- | :---- |
| `/login` | public | Email \+ password, error states, rate-limit lockout notice |
| `/invite/[token]` | public | Accept invite: set name \+ password; token-valid / expired / used states |
| `/` | any | Project list \+ "New Project" (button hidden for `approver`/`viewer`) |
| `/projects/[id]` | any | Overview: last run, readiness badge, cost, quick actions |
| `/projects/[id]/setup` | operator, admin | 5-step wizard: **Business context → Data sources → Model routing → Review** |
| `/projects/[id]/runs` | any | Run history table: status, **triggered by**, duration, cost, node failures, diff-vs-previous |
| `/projects/[id]/runs/[runId]` | any | **Run Console** (live); read-only for `viewer` |
| `/projects/[id]/runs/[runId]/report` | any | **Report Viewer** |
| `/projects/[id]/evidence` | any | Evidence Explorer |
| `/settings` | admin | Workspace name, OpenRouter key, global model defaults, storage, budget caps, SMTP |
| `/settings/members` | admin | Member list, invite, change role, disable, pending invites |
| `/settings/audit` | admin | Audit log, filterable by actor / action / date |
| `/approvals` | approver, admin | **Approvals inbox** — every gate awaiting my decision, across projects |
| `/account` | any | My name, password change, active sessions, personal OpenRouter key override |

### 13.4 Key screens

**A. Setup Wizard** (`operator`\+)

1. *Business context* — rich text \+ structured fields (products, pricing, markets, languages). Optional: paste URL → crawler pre-fills.  
2. *Data sources* — four cards (Google Ads, DataForSEO, Transparency Center, CSV upload). Each: connect/test button, green/grey state, "last synced". CSV card opens the column-mapping table.  
3. *Model routing* — OpenRouter key input (password field, `Test key` → hits `/api/v1/models`, shows credit balance). Then a 4-row table: `EXTRACT / CLASSIFY / SYNTHESIZE / CRITIQUE` → searchable model combobox showing `context window · $/M in · $/M out`. Live "estimated cost per full run" recalculates from historical token counts.  
4. *Approvers* — for each of the three gates (1.1.5 legal, 1.3.4 differentiation, 1.5.3 data consent) pick a default assignee from workspace members holding `approver` or `admin`, or leave unassigned so any approver may claim it. Optional SLA in hours per gate.  
5. *Review* — summary \+ `Run Research`. Button disabled with a tooltip when a required credential is missing or a run is already in flight (run lock).

Step 3 (model routing) and the credential fields in step 2 are **admin-only**; an `operator` sees them read-only with an "ask an admin to configure" note and can still complete the rest.

**B. Run Console** — the centerpiece

- **Left rail (280px):** stage accordion 1.1 → 1.6 with the 21 nodes, each a status pill. Mirrors the diagram's card layout. Click a node → right panel.  
- **Center:** DAG canvas (`reactflow`), nodes colored by status, edges animate while a wave is executing. Toggle to "list view" for narrow screens.  
- **Right panel (420px):** selected node → tabs `Output` (JSON tree \+ rendered summary) · `Evidence` (source cards w/ URL, fetched\_at, snippet) · `Prompt` (system+user, collapsed) · `Metrics` (model, tokens, cost, latency, attempts).  
- **Bottom bar:** overall progress, elapsed, live `$` spend vs cap, `Pause` / `Cancel` / `Re-run failed` — rendered only for `operator`/`admin`, and disabled for anyone who is not the run's `triggered_by` unless they are `admin`.  
- **Attribution:** header shows `Triggered by {user} · {relative time}` with an avatar, or `Schedule` for cron runs.  
- **Presence:** viewer avatars from a Redis `run:{id}:viewers` set, TTL 30s, refreshed on the SSE heartbeat. Informational only.  
- **Approval card:** when `approval.required` fires, an amber card pins to the top of the right panel with the agent's proposal, the evidence behind it, an editable proposal field, and `Approve` / `Reject with note`. Actionable only for users satisfying `required_role` (+ assignee match); everyone else sees the same card read-only with `Waiting on {assignee}`. Run resumes on submit with no page reload; a decision made by another user arrives over SSE and swaps the card to a decided state in place.  
- **Log drawer:** collapsible, virtualized, streams `node.progress` lines.

**C. Report Viewer**

- Two-column: sticky TOC ← → rendered report. Every `Claim` renders with a superscript citation chip; hover → evidence popover; click → Evidence Explorer filtered to that id.  
- Top bar: readiness badge (`GO` / `GO WITH FIXES` / `NO-GO`), generated timestamp, run cost, and an **Export** split-button (PDF · Word · Markdown · JSON · Keywords CSV) with a progress toast.  
- Embedded charts: spend-vs-conversions timeline, keyword volume × CPC scatter colored by intent, competitor message-cluster bar, seasonality heatmap.  
- `Compare with previous run` toggle → inline diff of every section (added/removed/changed), driven by `parent_run_id`.

**D. Evidence Explorer** Virtualized table (`@tanstack/react-virtual`), filters by source / kind / run / date, full-text \+ semantic search box (`pgvector`), row expands to raw payload JSON and source link. Creative screenshots render as a gallery for `source=transparency`.

**E. Settings** (`admin`) Key vault (masked, replace-only, never readable back), model defaults, `max_run_cost_usd`, storage usage \+ purge, schedule editor (cron \+ timezone, human-readable preview), SMTP config with `Send test email`, export defaults.

**F. Approvals Inbox** — `/approvals` Cross-project queue of gates awaiting the current user. Row: project, run, gate name, age, SLA countdown, requester. Opening a row shows the same approval card as the Run Console plus the evidence behind the proposal, without leaving the inbox. Explicit empty state ("nothing waiting on you"). A count badge on the sidebar item, polled every 60s and pushed live over SSE when the user is already in a console.

**G. Members** — `/settings/members` (`admin`) Table: name, email, role (inline select), status, last login, invited by. Actions: `Invite` (email \+ role → SMTP send, or a copyable link when SMTP is unset), `Change role`, `Disable` (confirm dialog naming the sessions being revoked). Pending-invites section with `Resend` / `Revoke` and expiry countdown. The last active admin's role select and disable action are disabled with an explanatory tooltip.

**H. Account** — `/account` Name, password change (requires current password), active sessions with device/IP/last-seen plus `Revoke` per session and `Sign out everywhere`, and an optional personal OpenRouter key that overrides the workspace key for runs this user triggers.

### 13.5 Frontend non-functional

1. Run Console keeps ≤ 16ms frame budget with 21 nodes \+ 500 streamed log lines (virtualize the log).  
2. SSE auto-reconnect with `Last-Event-ID` and state reconciliation via `GET /runs/{id}` on reconnect.  
3. Full keyboard nav on wizard and approval cards; all interactive elements reachable by Tab; `aria-live=polite` on run status.  
4. API keys are `type=password`, never echoed by any endpoint, never written to `localStorage`.  
5. Every mutating action has optimistic UI \+ rollback on error toast.  
6. No auth state in `localStorage` or JS-readable cookies; every mutating fetch attaches `X-CSRF-Token`.  
7. Role-gated UI is computed from `SessionProvider`, never guessed client-side; a `403` always renders a real state, never a blank screen.

---

## 14\. API Contract (v1, `/api/v1`)

GET    /health                              \# public

POST   /auth/bootstrap                      \# first-run only; 409 thereafter

POST   /auth/login                          POST /auth/logout

GET    /auth/me                             \# {id, name, email, role, permissions\[\]}

POST   /auth/password                       \# change own password

GET    /auth/sessions                       DELETE /auth/sessions/{sid}

GET    /invites/{token}                     POST /invites/{token}/accept   \# public

GET    /users                               POST /users/invite

PATCH  /users/{id}                          \# role / status — admin only

GET    /audit?actor=\&action=\&from=\&to=      \# admin only

GET    /workspace                           PATCH /workspace               \# admin only

GET    /models                              \# proxied OpenRouter catalogue \+ pricing

POST   /projects                            PATCH /projects/{id}   GET /projects

POST   /credentials                         \# {scope, kind, secret, project\_id?}

POST   /credentials/{id}/test               DELETE /credentials/{id}

GET    /credentials                         \# masked meta only, never ciphertext

POST   /projects/{id}/sources/csv           \# multipart \+ column map

POST   /projects/{id}/runs                  \# {mode, node\_filter?, reuse\_cache?}

GET    /runs/{id}                           \# full state incl. node statuses

GET    /runs/{id}/events                    \# SSE

POST   /runs/{id}/cancel                    POST /runs/{id}/retry-failed

GET    /runs/{id}/nodes/{node\_id}           \# output, evidence, prompt, metrics

GET    /approvals?run\_id=\&mine=true\&status=   \# inbox feed

POST   /approvals/{id}                      \# {decision, note, edited\_proposal?}

PATCH  /approvals/{id}/assignee             \# reassign; admin or current assignee

GET    /reports/{run\_id}

POST   /reports/{run\_id}/export?format=     \# 202 {job\_id}, generated in worker

GET    /exports/{job\_id}                    \# {status, format, bytes, ready\_at}

GET    /exports/{id}/download               \# streams from worker file server

GET    /evidence?project\_id=\&source=\&kind=\&q=\&cursor=

GET    /runs/{id}/diff?against={run\_id}

POST   /schedules                           PATCH /schedules/{id}

Errors: RFC 9457 `application/problem+json`. All list endpoints cursor-paginated.

Served at `/api/v1` on the `api` service, reachable only through the `web` service's rewrite — `api` has no public ingress (§5.2).

**Every route** except `/health`, `/auth/bootstrap`, `/auth/login` and `/invites/{token}*` requires a valid session and declares a `require(Permission)` dependency. Unauthenticated → `401`; authenticated but unauthorized → `403` with `problem.detail` naming the missing permission. No response ever contains `password_hash`, `ciphertext`, `nonce`, or `token_hash`.

---

## 15\. Non-Functional Requirements

| \# | Requirement | Threshold |
| :---- | :---- | :---- |
| NF1 | Full 21-node run, cold cache | ≤ 45 min wall clock |
| NF2 | Cost per full run | ≤ $12 at default routing; hard cap configurable |
| NF3 | Resume after API crash mid-run | Zero completed nodes re-executed |
| NF4 | Connector partial failure | Run completes; report lists `degraded_sources`; never silent |
| NF5 | Secrets at rest | AES-256-GCM; `APP_ENCRYPTION_KEY` from env only; no key in DB, logs, or API responses |
| NF5a | Authn | Argon2id (`t=3, m=64MiB, p=4`); session revocation takes effect on the next request, ≤1s |
| NF5b | Authz | 100% of routes carry a `require(Permission)` dependency — CI fails the build otherwise. Cross-role access tests for all 4 roles × all mutating routes |
| NF5c | Auditability | Every mutating action resolves to an actor: `AuditLog` row written in the same transaction, `actor_id` NULL only for `trigger=schedule` |
| NF5d | Concurrency | Two operators launching the same project concurrently → exactly one run; the other gets `409` naming the holder |
| NF6 | Evidence traceability | 100% of `Claim` objects carry ≥1 resolvable `evidence_id` |
| NF7 | Determinism | `temperature=0`/`top_p=1` for EXTRACT and CLASSIFY; identical input hash \+ cache reuse ⇒ identical output |
| NF8 | Cold start | `docker compose up` → login screen in ≤ 3 min on a clean machine; bootstrap admin usable immediately |
| NF8a | Deploy | `git push` → Railway builds and promotes all three app services with zero manual steps; `alembic upgrade head` runs as `preDeployCommand` exactly once per deploy |
| NF8b | Deploy safety | A deploy loses no data: creative screenshots, exports and backups survive on the Volume; a failed healthcheck rolls back without dropping the previous version |
| NF8c | Portability | The only environment-specific config is env-var **values**. No `if RAILWAY` branch anywhere in application code |
| NF8d | Private network | `api` and `worker` return connection-refused from the public internet; only `web` resolves a public domain |
| NF9 | Test coverage | ≥ 80% on `orchestrator/`, `nodes/`, `export/`, `auth/` |

---

## 16\. Failure Modes

| Failure | Handling |
| :---- | :---- |
| Transparency Center selector change | `ConnectorDegraded`, dump HTML to `debug/` on the Volume, continue run, flag in report, surface a red banner in UI with the failing selector name |
| OpenRouter 429 / 5xx | Backoff \+ retry ×3, then auto-fallback to the next model in the task class's fallback list; record substitution in `NodeRun.model` |
| Model returns invalid JSON | One repair pass with validation error injected; then attempt counter |
| Google Ads API not authorized | Node emits `status=skipped_no_source`; report marks affected sections `insufficient_evidence`; never hallucinate history |
| CSV schema mismatch | Reject upload with a per-row error report; no partial ingest |
| Budget cap hit mid-run | Cancel remaining nodes, persist everything done, emit partial report labeled `partial` |
| Worker killed | Run marked `failed` by a startup reaper (heartbeat \> 5 min stale); resumable from last checkpoint |
| Approval never answered | Run sits in `awaiting_approval` indefinitely; assignee's inbox badge \+ reminder email at 50% and 100% of SLA; admin can reassign. **No auto-approve** — a gate exists because a human must decide |
| Assigned approver disabled or removed | Approval falls back to `assignee_id=NULL` (any holder of `required_role`); UI banner on the run; audit-logged |
| Two operators launch the same project | Redis run lock → second request `409` with the holder's name and run id; UI offers "open the running run" |
| Concurrent project edits | `If-Unmodified-Since` mismatch → `412`; UI shows a "changed by {user}" diff prompt, never a silent overwrite |
| Session hijack / lost laptop | `Sign out everywhere` on `/account`, or admin disables the user — all sessions dropped on the next request |
| SMTP unconfigured or failing | Invites and approval notices degrade to copyable in-app links \+ inbox badges; never blocks the flow, surfaced as a settings warning |
| Brute-force login | 5 attempts / 15 min per (email \+ IP), lockout \+ audit row; constant-time response regardless of email existence |
| Deploy wipes container FS | Nothing durable is stored outside `/data` on the `worker` Volume; `api` and `web` are stateless by construction. A smoke test asserts a pre-deploy export is still downloadable after deploy |
| Service bound to IPv4 | Sibling services get `ECONNREFUSED` on `*.railway.internal`. Startup asserts the bind address is `::` and fails loudly with the fix in the message |
| SSE connection dropped by Railway edge | 15s heartbeat keeps it open; client reconnects with `Last-Event-ID` and reconciles via `GET /runs/{id}` |
| Migration race between `api` and `worker` | Migrations only ever run in `preDeployCommand`; both services assert `alembic current == head` at boot and exit non-zero otherwise |
| Chromium OOM / `/dev/shm` exhaustion | `--disable-dev-shm-usage`, Playwright concurrency 1, `worker` memory floor 2 GB; on crash the connector raises `ConnectorDegraded` and the run continues |
| Volume full | Storage check before each run; nightly retention job prunes screenshots beyond the configured window; admin sees a storage banner in `/settings` |

---

## 17\. Build Phases

Each phase is sized to complete inside **\~70% of one Claude Code context window**. Ship order is strict — every phase ends green and runnable. Start each new session by pasting §18 \+ the phase block. **11 phases total** (P0, P0b, P1–P9). P9 can be pulled forward and run after P0b if you want the deploy path proven early — recommended.

| Phase | Scope | Exit criteria | Est. files |
| :---- | :---- | :---- | :---- |
| **P0 — Foundation** | Monorepo, three Dockerfiles \+ three `railway.json` files, `docker-compose` mirroring the Railway topology (pgvector/pgvector:pg16, redis, api, worker, web), `pydantic-settings` config, Alembic \+ all §6 tables, AES-GCM `crypto.py`, `storage/backend.py` \+ `local.py`, `/health`, Next.js scaffold with tokens \+ shell \+ `/api/v1` rewrite, `contracts` JSON-Schema export script | `docker compose up` → web proxies `/api/v1/health` to a 200, migrations applied, web renders empty project list; all services read `$PORT` and bind `::` | \~30 |
| **P0b — Auth & workspace** | `auth/{passwords,sessions,invites,deps,rbac}.py`, bootstrap flow, session middleware \+ CSRF, login/logout/me, invite create \+ accept, users CRUD, `WorkspaceScopedRepo`, `AuditLog` writer, `notify/email.py` (SMTP, optional), `/login` \+ `/invite/[token]` \+ `SessionProvider` \+ `middleware.ts` \+ `<Can>` on the frontend | Bootstrap admin can log in; invite a user of every role and each lands on the right permission set; CI route-guard check passes; cross-role test matrix green (4 roles × all mutating routes); session revocation effective ≤1s | \~20 |
| **P1 — LLM gateway \+ orchestrator** | `llm/gateway.py` (OpenRouter, strict JSON schema, fallbacks, ledger), `llm/router.py`, `orchestrator/{dag,executor,registry,state}.py`, 2 trivial dummy nodes, arq worker, SSE endpoint, retry/repair/checkpoint/cancel/budget-cap | Unit tests: dummy 2-node DAG runs end-to-end via API, streams SSE, resumes after a forced kill, aborts on budget cap | \~18 |
| **P2 — Connector layer** | `connectors/base.py` \+ all 5 connectors, `evidence/{store,normalize,search}.py`, pgvector embedding \+ hybrid search, VCR cassettes, CSV column-mapping endpoint | Each connector writes deduped `Evidence` rows from a live or cassette fetch; `GET /evidence` filters and searches | \~22 |
| **P3 — Stages 1.1 \+ 1.2** | 8 nodes with Pydantic I/O \+ prompts, pandas search-term P\&L, gate mechanics for 1.1.5 incl. `required_role` routing \+ assignee notification, `/approvals` endpoints with authz | Partial run of stages 1.1+1.2 produces validated outputs, halts on 1.1.5, an `approver` (not an `operator`) can resume it, decision is audit-logged | \~22 |
| **P4 — Stages 1.3 \+ 1.4** | 9 nodes, Playwright creative corpus \+ screenshot storage, batched intent classification, keyword→page mapping, gate 1.3.4 | Partial run produces ≥100 competitor creatives and ≥2,000 classified, priced keywords with page mapping | \~22 |
| **P5 — Stage 1.5 \+ report \+ export** | 4 readiness nodes, synthetic conversion probe, `report_synthesis` \+ `report_critique`, Jinja2 markdown, WeasyPrint PDF, python-docx DOCX, CSV/JSON export **as arq jobs in `worker`**, `fileserver.py` \+ HMAC-token streaming download on `api` | Full 21-node run on one project emits `ResearchReport` and all 5 export formats; PDF has TOC \+ page numbers; DOCX TOC updates in Word | \~20 |
| **P6 — Frontend A** | App shell with role-aware nav, `/settings` \+ `/settings/members` \+ `/settings/audit` \+ `/account`, project CRUD, 5-step setup wizard (incl. approver assignment and admin-only model routing with live cost estimate), CSV mapper UI, run trigger with lock handling | An admin can invite all four roles, store keys, test connectors, pick models, assign gate approvers, and launch a run entirely from the UI; an `operator` sees the same app with writes they lack correctly hidden and `403`\-safe | \~34 |
| **P7 — Frontend B** | Run Console (rail \+ reactflow canvas \+ right panel \+ log drawer \+ SSE \+ attribution \+ presence), role-aware approval cards, `/approvals` inbox with badge, Report Viewer with citation popovers \+ charts \+ export split-button, Evidence Explorer | Live run is fully observable; an approver decides a gate from the inbox without opening the console and the run resumes; a `viewer` can read everything and write nothing; report readable and exportable without touching the API | \~34 |
| **P8 — Hardening** | Schedules (arq cron polling `Schedule`), delta runs \+ `diff` endpoint \+ Report Viewer compare mode, degraded-source banners, reaper job, approval SLA reminders, nightly `pg_dump` \+ retention job, structured logging with secret \+ PII redaction, eval harness (10 golden fixtures × schema+groundedness assertions), security pass (headers, cookie flags, rate limits) | Scheduled run executes unattended with `triggered_by=NULL`; diff view renders; SLA reminder fires; `pg_dump` lands in `/data/backups/`; eval suite green; coverage ≥ 80% on core packages incl. `auth/` | \~24 |
| **P9 — Railway deploy** | Provision the five services, wire reference variables, attach the Volume, set the custom domain, verify `preDeployCommand`, private-network smoke tests, `railway/README.md` runbook \+ `railway/variables.md` | A full 21-node run completes on Railway end to end; an export downloads after a subsequent redeploy; `api` and `worker` are unreachable publicly; rollback tested once | \~8 |

---

## 18\. Global Build Context (paste at the top of every phase session)

PROJECT: ads-research-agent — self-hosted Paid Ads Research Agent.

        ONE workspace, MANY users, invite-only, 4 roles:

        admin | operator | approver | viewer. No multi-tenancy.

STACK: Python 3.12 \+ FastAPI \+ SQLAlchemy 2.0 \+ Alembic \+ arq \+ Playwright,

       self-managed Postgres 16 (pgvector/pgvector:pg16 image), Redis 7\.

       Frontend: Next.js 15 App Router, TS strict, Tailwind v4, shadcn/ui,

       TanStack Query v5, Zustand, reactflow, recharts.

DEPLOY: Railway — 5 services (web, api, worker, postgres, redis).

       Docker Compose locally is a MIRROR of that topology, not a separate design.

       NO managed database vendor. Postgres is our own container.

LLM:   OpenRouter only. Strict JSON-schema structured outputs. Model IDs are

       runtime config, never hardcoded. Per-task-class routing.

LAWS:

  1\. LLMs never source facts. Connectors write Evidence; nodes cite evidence\_ids.

     A Claim without \>=1 evidence\_id fails validation.

  2\. Every node: Pydantic input model, Pydantic output model, registered in

     orchestrator/registry.py, declared in orchestrator/dag.py.

  3\. All arithmetic (CPA, ROAS, P\&L, sizing) happens in pandas/Python.

     The model writes labels and prose only.

  4\. Secrets: AES-256-GCM, env-provided key, never returned by any endpoint,

     never logged.

  5\. Everything resumable. Persist a NodeRun before moving to the next node.

  6\. AUTHZ IS SERVER-SIDE. Every route declares require(Permission). Hiding a

     button in React is cosmetic, never the control. Every query goes through

     WorkspaceScopedRepo. Sessions are server-side in Redis, never JWTs.

  7\. Every mutating action writes an AuditLog row in the SAME transaction,

     with the acting user's id.

  8\. Type hints everywhere. ruff \+ mypy strict on api/, eslint \+ tsc on web/.

     pytest for api/, vitest \+ Playwright for web/. Every new mutating route

     ships with a 4-role authz test.

  9\. RAILWAY RULES, always: read $PORT, bind :: (the private network is IPv6-only),

     call the API only via relative /api/v1 paths through the Next.js rewrite,

     never expose api or worker publicly, never write outside $STORAGE\_DIR,

     and run migrations only in preDeployCommand. No \`if RAILWAY\` branch in

     application code — environments differ by env-var VALUES only.

 10\. The container filesystem is ephemeral. Anything that must survive a deploy

     goes through storage/backend.py onto the worker's Volume.

 11\. No new dependency without a one-line justification in the PR description.

SCOPE DISCIPLINE: build exactly the current phase. Do not scaffold future

phases. Do not add hardening, abstraction, or features not in the phase block.

If something seems missing, list it as an open question instead of building it.

---

## 19\. Claude Code Build Brief — Phase 0

Build Phase 0 of ads-research-agent. Read the global build context above first.

DELIVERABLES

1\. Monorepo at ./ads-research-agent with the layout in PRD §5.1.

2\. docker-compose.yml — a MIRROR of the Railway topology, five services:

   postgres (image pgvector/pgvector:pg16, volume pgdata:/var/lib/postgresql/data),

   redis (redis:7-alpine, appendonly yes), api (uvicorn \--reload \--host ::),

   worker (arq, volume storage:/data), web (next dev).

   Healthchecks on postgres and redis; api depends\_on both healthy.

   Only \`web\` publishes a host port — api/worker are internal, exactly as on Railway.

2b. Dockerfiles:

   \- apps/api/Dockerfile — python:3.12-slim, uv sync \--frozen, CMD uvicorn

     agent.main:app \--host :: \--port $PORT. No browser deps.

   \- apps/api/Dockerfile.worker — FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy,

     same install, CMD arq agent.worker.WorkerSettings.

   \- apps/web/Dockerfile — multi-stage, next build with output:'standalone',

     CMD node server.js, reading $PORT.

2c. railway config as code (do not invent fields; these three only):

   \- apps/api/railway.json: build.builder DOCKERFILE, dockerfilePath Dockerfile,

     deploy.preDeployCommand "alembic upgrade head",

     deploy.healthcheckPath "/api/v1/health", healthcheckTimeout 60,

     restartPolicyType ON\_FAILURE.

   \- apps/api/railway.worker.json: dockerfilePath Dockerfile.worker,

     no healthcheck, restartPolicyType ON\_FAILURE, restartPolicyMaxRetries 10\.

   \- apps/web/railway.json: dockerfilePath Dockerfile, healthcheckPath "/".

2d. railway/README.md \+ railway/variables.md: the provisioning runbook and the

   full env-var table with Railway reference expressions (${{postgres.DATABASE\_URL}}

   etc). Documentation only in this phase — provisioning is P9.

3\. apps/api: uv-managed pyproject (fastapi, uvicorn\[standard\], sqlalchemy\[asyncio\],

   asyncpg, alembic, pydantic-settings, arq, cryptography, argon2-cffi, httpx,

   structlog, pgvector). App factory in src/agent/main.py, CORS for

   localhost:3000 with credentials:true, /api/v1/health returning

   {status, db, redis, version}.

4\. config.py using pydantic-settings: PORT, DATABASE\_URL, REDIS\_URL,

   APP\_ENCRYPTION\_KEY, SESSION\_COOKIE\_NAME, SESSION\_TTL\_DAYS, COOKIE\_SECURE,

   BOOTSTRAP\_ADMIN\_EMAIL, BOOTSTRAP\_ADMIN\_PASSWORD, OPENROUTER\_BASE\_URL,

   STORAGE\_BACKEND (local|s3, default local), STORAGE\_DIR (default /data),

   WORKER\_INTERNAL\_URL, FILE\_TOKEN\_SECRET, MAX\_RUN\_COST\_USD,

   SMTP\_\* (all optional).

   Fail fast with a clear message if APP\_ENCRYPTION\_KEY is missing or not 32 bytes

   base64.

5\. db/models.py: every table in PRD §6 — Workspace, User, Invite, AuditLog,

   Project, Credential, Run, NodeRun, Evidence, Approval, Report, Export,

   Schedule — exactly those columns, with the listed indexes, the HNSW index on

   Evidence.embedding, the partial unique index enforcing the Workspace

   singleton, and the Credential scope CHECK constraint. One Alembic migration

   that creates them plus \`CREATE EXTENSION IF NOT EXISTS vector\` and \`citext\`.

5b. db/repo.py: WorkspaceScopedRepo base class — every query filtered by

   workspace\_id, no exceptions. Schema only in this phase; auth logic is P0b.

5c. storage/backend.py: StorageBackend Protocol (put, get, open, delete, exists,

   url\_for) \+ storage/local.py writing under STORAGE\_DIR with path-traversal

   rejection. s3.py is a stub raising NotImplementedError. No module outside

   storage/ may touch a filesystem path — add a test that greps for \`open(\` and

   \`pathlib\` outside storage/ and export/templates/ and fails on a hit.

6\. crypto.py: encrypt(plaintext)-\>(ciphertext, nonce) / decrypt(...) using

   AES-256-GCM from \`cryptography\`. Unit tests incl. tamper detection.

7\. scripts/export\_schemas.py: dump Pydantic JSON Schemas to packages/contracts/.

   Wire a pnpm script that turns them into zod via json-schema-to-zod.

8\. apps/web: Next.js 15 (App Router, TS strict, Tailwind v4, shadcn/ui init,

   output:'standalone'). next.config.ts rewrites /api/v1/:path\* to

   \`${process.env.API\_INTERNAL\_URL}/api/v1/:path\*\`. lib/api.ts uses RELATIVE

   paths only — no NEXT\_PUBLIC\_API\_URL anywhere in the repo.

   styles/tokens.css with the PRD §13.2 tokens, light \+ dark via next-themes.

   Shell layout: left nav (Projects, Approvals, Evidence, Settings), top bar with

   theme toggle, API-health dot polling /api/v1/health, and a user-menu placeholder.

   Route \`/\` renders an empty-state project list with a disabled "New Project".

   Do NOT implement auth UI here — that is P0b. Leave the nav unguarded.

9\. .env.example, Makefile (make up / make migrate / make test / make fmt),

   README with setup in \<=10 commands.

ACCEPTANCE (binary)

\- \`docker compose up \-d\` on a clean machine, then \`curl localhost:3000/api/v1/health\`

  (through the web rewrite, NOT a direct api port) returns 200 with db:"ok"

  and redis:"ok".

\- No host port is published for api or worker in docker-compose.yml.

\- \`grep \-r NEXT\_PUBLIC\_API\_URL apps/web\` returns nothing.

\- \`docker compose exec api python \-c "import socket"\` style check or a startup

  assertion proves uvicorn is bound to \`::\`, not 0.0.0.0.

\- All three railway.json files parse as valid JSON and name an existing

  dockerfilePath.

\- \`alembic upgrade head\` creates 13 tables; \`\\d evidence\` shows a vector(1536)

  column and an HNSW index; inserting a second Workspace row fails on the

  partial unique index; a Credential with scope='project' and NULL project\_id

  fails the CHECK constraint.

\- \`pytest\` passes with the crypto tests green.

\- localhost:3000 renders the shell in both themes with no console errors and no

  hydration warnings.

\- \`mypy src/agent\` and \`pnpm tsc \--noEmit\` both exit 0\.

START WITH: the three Dockerfiles \+ docker-compose.yml, then

apps/api/src/agent/{config.py,db/models.py,db/repo.py,storage/}, then the

Alembic migration, then health, then the web shell \+ rewrite. Commit after each.

NOTE: Phase 0 is schema \+ scaffold only. Authentication, sessions, RBAC,

invites and the login UI are Phase P0b — do not start them here.

---

## 19.1 Claude Code Build Brief — Phase P0b (Auth & Workspace)

Build Phase P0b of ads-research-agent. Phase 0 is done: schema, migrations,

health, web shell. Read the global build context in PRD §18 first.

DELIVERABLES (backend)

1\. auth/passwords.py — Argon2id via argon2-cffi (time\_cost=3,

   memory\_cost=65536, parallelism=4). hash(), verify(), needs\_rehash().

   Min 12 chars \+ common-password list rejection.

2\. auth/sessions.py — create/read/refresh/revoke sessions in Redis at

   session:{sid} (opaque 256-bit sid, secrets.token\_urlsafe(32)).

   30-day sliding TTL, 12h idle timeout. revoke\_all\_for\_user(user\_id).

3\. auth/rbac.py — Permission enum (READ, PROJECT\_WRITE, RUN\_EXECUTE,

   CREDENTIAL\_WRITE, SETTINGS\_WRITE, APPROVAL\_DECIDE, USER\_MANAGE,

   AUDIT\_READ) and one ROLE\_PERMISSIONS dict matching PRD §4.1 exactly.

4\. auth/deps.py — current\_user() and require(Permission) FastAPI deps.

   401 for no/invalid session, 403 (RFC 9457\) naming the missing permission.

5\. middleware — session resolution, sliding-TTL refresh, CSRF double-submit

   (X-CSRF-Token vs csrf cookie) on all non-GET, SSE GET exempt.

   Security headers: HSTS, X-Content-Type-Options, Referrer-Policy,

   X-Frame-Options DENY.

6\. auth/invites.py — 32-byte token, store SHA-256 only, 7-day expiry,

   single use, UNIQUE(workspace\_id, email) WHERE accepted\_at IS NULL.

7\. Bootstrap — on startup with zero users, create the Workspace singleton and

   the first admin from BOOTSTRAP\_ADMIN\_EMAIL/PASSWORD. POST /auth/bootstrap

   returns 409 once a user exists.

8\. Routes (all under /api/v1): auth/login, auth/logout, auth/me,

   auth/password, auth/sessions (list \+ delete), invites/{token} (GET),

   invites/{token}/accept, users (GET), users/invite, users/{id} (PATCH),

   audit (GET), workspace (GET/PATCH). Every one declares require(...)

   except the public four.

9\. Login rate limit: 5 attempts / 15 min per (email \+ IP) in Redis, lockout \+

   AuditLog row, constant-time response whether or not the email exists.

10\. audit.py — write\_audit(actor, action, target, meta, ip) that participates

    in the caller's transaction. Wire it into every mutating route above.

11\. Last-admin guard: PATCH /users/{id} demoting or disabling the final active

    admin fails inside the transaction with 409\.

12\. notify/email.py — optional SMTP (aiosmtplib). When SMTP\_HOST is unset,

    send\_invite() returns the raw link instead of sending, and the API returns

    it in the response for the admin to copy. Never raise on send failure.

13\. structlog processor that redacts password, secret, token, authorization,

    ciphertext and cookie values from every log line.

DELIVERABLES (frontend)

14\. middleware.ts guarding every route except /login and /invite/\[token\].

15\. Root layout server component fetching GET /auth/me into a SessionProvider

    (React context: user \+ permissions\[\] \+ has(permission)).

16\. \<Can permission="..."\> wrapper component. lib/api.ts sends

    credentials:'include' and attaches X-CSRF-Token on mutations; a 401

    redirects to /login?next=, a 403 renders an inline NoAccess state.

17\. /login page — email \+ password, inline field errors, lockout message,

    honors ?next=.

18\. /invite/\[token\] page — validates token server-side, then name \+ password

    form; distinct UI for expired / already-used / invalid tokens.

19\. User menu in the top bar: name, role badge, Account, Sign out.

    Nav items filtered by permission.

ACCEPTANCE (binary)

\- Fresh DB \+ BOOTSTRAP\_ADMIN\_\* env → admin can log in at /login; a second call

  to POST /auth/bootstrap returns 409\.

\- Admin invites one user of each role; each accepts via /invite/\[token\] and

  lands with exactly the PRD §4.1 permission set.

\- Authz test matrix: for all 4 roles x every mutating route, an unauthorized

  call returns 403 and performs no write. Test file exists and passes.

\- CI check \`scripts/check\_route\_guards.py\` walks every APIRouter and fails if

  any route lacks a require(...) dependency (allowlist the 4 public routes).

\- POST without X-CSRF-Token returns 403\.

\- 6 failed logins in 15 min returns a lockout problem+json and writes an

  AuditLog row.

\- Disabling a user invalidates their session on their next request (\<=1s).

\- Demoting the last active admin returns 409 and leaves the row unchanged.

\- grep the log output for a known password/API key: zero hits.

\- mypy src/agent and pnpm tsc \--noEmit both exit 0\.

START WITH: auth/rbac.py \+ auth/deps.py (the contract everything else depends

on), then passwords/sessions, then the routes, then the CI guard check, then

the frontend. Commit after each.

---

## 20\. Open Questions

1. **Google Ads API developer token** — do we have Basic access on the SDS Manager MCC, or does P2 ship CSV-only first? Blocks node 1.2.\* fidelity.  
2. **DataForSEO vs SEMrush** — which account exists today? The `KeywordProvider` interface is neutral, but P2 needs one working credential.  
3. **Competitor seed list** — does the agent discover competitors purely from SERP/auction overlap, or do we pin a hand-curated list of 5 as a floor?  
4. **~~Approval routing~~** — **Resolved in v1.1.** Approvals happen in-app: legal, the data officer and the marketing lead get `approver` accounts and decide from `/approvals`. SMTP notification is optional; without it they get in-app inbox badges.  
5. **Synthetic conversion probe (1.5.2)** — this fires a real test conversion. Confirm it targets a dedicated test conversion action, excluded from reporting, never a production purchase event.  
6. **Embedding model** — OpenRouter's embedding coverage is thinner than chat. Confirm fallback to a local `bge-small` via `fastembed` if no hosted embedding model is available on the key.  
7. **Screenshot retention** — competitor creative screenshots will grow fast. Retention policy: keep N runs or M GB?  
8. **~~Hosting & TLS~~** — **Resolved in v1.2.** Railway terminates TLS on the `web` service and everything is same-origin behind the Next.js rewrite, so `Secure; SameSite=Lax` works with no reverse proxy of our own. Remaining sub-question: which custom domain (`research.sdsmanager.com`?) — a `*.up.railway.app` subdomain works for P9 until that's decided.  
9. **SMTP sender** — do invites and approval reminders go out via SDS Manager's existing Brevo account, or a separate transactional sender? Without one, invites are copy-paste links.  
10. **SSO** — is Google Workspace SSO needed for the wider team later? v1.1 ships email+password only; OIDC would slot in behind the same session layer but is not specced here.  
11. **Viewer scope** — should `viewer` see the Evidence Explorer and raw competitor data, or reports only? Current spec gives them read access to everything.  
12. **Volume size** — Railway Volumes are provisioned at a fixed size and grow by manual resize. Starting at 10 GB assumes \~200 screenshots/run × weekly runs plus exports and 14 days of `pg_dump`. Confirm, or set a tighter screenshot retention (Q7) instead.  
13. **Environments** — one Railway project (prod only), or a separate staging environment? Staging doubles cost but makes the P9 rollback test non-scary. Current spec assumes prod-only.

