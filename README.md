# ads-research-agent

Self-hosted **Paid Ads Research Agent** — Stage 01 of the SDS Manager marketing
pipeline. It runs a deterministic 23-node research DAG over five evidence
sources and emits a versioned, citation-backed Research Report.

One workspace, many users, invite-only, four roles. See `PRD files/prd-research.md`
for the full specification.

> **Status: Phase P5b (Stage 1.5 + the report).** Everything P0 through P6
> shipped, plus the six nodes that finish the DAG: the landing-page audit, the
> tracking probe with its synthetic conversion check, the audience-consent gate,
> opportunity sizing, and the two report nodes — `report_synthesis` writes the
> `ResearchReport`, `report_critique` reads it back with a different model
> family. **The graph is complete: twenty-three registered nodes**, which is
> PRD §10's twenty-one research nodes plus the two report nodes §17's
> "21-node run" does not count. A full run now ends with a stored report and
> five downloadable formats. P7 (the run console, report viewer, evidence
> explorer and approvals inbox) is next. Phase table: PRD §17.

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
make verify-p5a    # P5a's acceptance list: five formats, rendered and downloaded
make verify-p5b    # P5b: the whole DAG, three gates, a report, five exports
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

## Uploading a folder of context

The document uploader takes a `.zip` as well as a file. What a person has is
usually a folder — the pricing sheet, the positioning one-pager, the objection
doc — and whether they compressed it before dragging it in is not a distinction
this product should hold an opinion about. One endpoint, one code path: each
readable member goes through the same extraction, dedupe and passage-writing a
direct upload does.

What differs is the shape of failure. One bad file on its own is the whole
request; one bad file inside an archive is that file's problem and the other
nine still land. Everything refused is **named on the screen** with the reason —
a zip of twelve files that quietly becomes three is worse than an error, because
nothing says the other nine are missing.

An archive is an untrusted description of files that do not exist yet, so three
of its claims are checked rather than believed (`documents/archive.py`):

- **The size it declares.** Both the total and the compression ratio are checked
  *before* anything is decompressed — a zip bomb is a few hundred kilobytes
  describing a few gigabytes, and the way a service finds that out is by
  unpacking it. A folder of PDFs expands three or four times; the ceiling is
  120×. Each member is then read one byte past its cap, so an index that lies
  about a size costs one byte rather than the machine.
- **The paths it declares.** `../../etc/passwd` is a legal entry name. Nothing
  is ever written to disk — members are read into memory and handed to the same
  extractor — so it has nowhere to land, and only the basename is kept.
- **The types it declares.** Filtered to what the extractor can actually read.
  Archives inside archives are not opened, and `__MACOSX/` and `.DS_Store` are
  skipped silently, because telling someone their Mac put those in the zip is
  noise about something they did not do.

Limits live in `config.Settings`: `archive_max_entries` (50),
`archive_max_total_bytes` (200 MB), `archive_max_ratio` (120), with each member
still bound by `document_max_bytes`.

## Connecting Google Ads

`connectors/google_ads.py` is the only source that reads *our own* history:
twenty-four months of campaign P&L, search terms, past creative, the change log,
impression share, conversion actions and audience lists. Four nodes read it, and
PRD §16 says a missing account degrades rather than fails — so an unconnected or
half-connected account costs a thinner report and no error at all. That is why
there is a verification script.

**Google Ads has no API key.** A call carries two separate things:

- a **developer token**, issued once in the manager account under Tools &
  Settings -> API Center, which says this tool may use the API at all;
- an **OAuth access token** for a Google user, which says whose accounts it may
  read. That one is minted from a `client_id`/`client_secret`/`refresh_token`
  trio, and it is the part that cannot be typed from memory.

**The normal way in is a button.** The Sources step shows *Continue with
Google*: whoever owns the Ads account types the developer token, signs in with
their own Google account, approves read access, and the refresh token is sealed
into the vault by the callback. They never see a token, and neither does anyone
else — which matters, because the person with the Ads login is usually not the
person who installed this, and the alternative is asking them to run a terminal.

That needs one thing from the deployment: an OAuth client, in the environment
rather than the vault (it belongs to the installation, not to the workspace —
same rule as SMTP, PRD §18 law 9).

```bash
GOOGLE_ADS_OAUTH_CLIENT_ID=…apps.googleusercontent.com
GOOGLE_ADS_OAUTH_CLIENT_SECRET=GOCSPX-…
```

Register this exact redirect URI on it, or Google refuses before the consent
screen renders:

```
<APP_BASE_URL>/api/v1/credentials/google-ads/callback
```

In production that means an OAuth client of type **Web application**. A
**Desktop app** client also works while `APP_BASE_URL` is a localhost address,
because Google treats that as a loopback redirect — which is why local
development needs no second client.

**The operator's way in is the script**, for when nobody is available to click:

```bash
make google-ads-oauth      # consent in the browser -> refresh token + account ids
make verify-google-ads     # proves the whole path against the live account
```

`scripts/google-ads-oauth.py` opens the same consent screen, catches the
redirect on a loopback port, exchanges the code for an *offline* refresh token,
then asks `customers:listAccessibleCustomers` which accounts that consent
actually reaches and names each one — so the customer id that gets stored is one
you have seen answer, not one copied off a dashboard. The card keeps a *Paste
all values instead* toggle for the five values it prints.

Both paths pick the account the same way: the first accessible account that is
**not** a manager. A manager account holds no campaigns, so connecting one would
report success and then find nothing. Every account the grant reaches is
recorded in the credential's hints, so a workspace with several can see what it
chose between.

Two things that are easy to get wrong, and both fail quietly:

- **The API version is pinned and versions are retired on a schedule.**
  `google_ads_api_version` is `v25` (sunset August 2027). A retired version does
  not answer with a Google Ads error — it answers with the front end's HTML 404,
  which is why the connector names the version in that case rather than passing
  on a parse failure.
- **The change log is windowed by Google, not by us.** Change events must be
  asked for inside the last 30 days and with a `LIMIT` of at most 10,000. A
  wider window is a rejected query rather than a longer history, and because a
  failed pull degrades the run instead of ending it, the only symptom would be a
  report that never mentions what changed in the account.

`scripts/google_ads_pull.py` (inside the `api` container) pulls history on
demand — the same vault credential, connector and evidence store a node's pull
uses, without waiting for a run. `--dry-run` fetches without writing, which is
the quickest way to answer "does this credential actually work".

## The SERP source

`connectors/serp.py` buys one live Google result page per money term through a
Bright Data SERP proxy and keeps four things off it: the organic ranking
(`serp_snapshot`), every ad on the page (`serp_ad`), People Also Ask
(`serp_question`) and the related searches (`serp_related`). Node 1.3.1 asks for
it, 1.3.2 folds the ads into the creative corpus beside the Transparency Center
archive, and 1.4.1 seeds the keyword universe on the last two.

It is the answer to a gap rather than a second opinion: DataForSEO's SERP
endpoint is filtered to organic results, so before this the *advertisers* on a
page we were bidding into never reached the evidence store at all.

Three things to know:

- **The account is a vault credential** (`brightdata`), configured in the setup
  wizard's Sources step like any other. Host and port are optional and default
  to Bright Data's published SERP endpoint.
- **A SERP zone serves search engines, not arbitrary sites.** Asked for a
  competitor's homepage it answers `400 This target URL isn't supported with
  SERP API, use the Web Unlocker product for targeting this URL`. So it does
  not double as an unblocker: `web_crawler` and the Transparency Center still
  go out directly, and giving them a proxy means a second Bright Data zone.
- **TLS is not verified on that one connection.** The proxy terminates TLS with
  its own CA, so a verifying client fails the handshake. The tunnel carries a
  public search query; the account secret goes to the proxy in a
  `Proxy-Authorization` header and never enters it. `SERP_VERIFY_TLS=true` once
  Bright Data's CA is in the image's trust store.

`make verify-serp` proves the whole path against both live accounts — it deletes
the project's SERP evidence first, so the pull has to happen again rather than
reading the previous run's rows back.

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

## Known limits of P4

- ~~Four of the twenty-one nodes do not exist yet.~~ **Closed by P5b**: the DAG
  is complete at twenty-three nodes.
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

## Known limits of P5a

- ~~Nothing writes a report yet.~~ **Closed by P5b**: 1.6.1 writes one at the
  end of every run. `GET /reports/{run_id}` still 404s — correctly — for a run
  that has not reached the report node.
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

## Readiness and the report (P5b)

The last six nodes, and the two ideas they are built on.

**Measurement is not a model's job.** Two of stage 1.5's four nodes make no LLM
call at all. A page's LCP, a conversion action's staleness and a synthetic
probe's verdict are facts with published thresholds, and every one of them lives
in `nodes/readiness.py` — pure functions over evidence payloads, unit-tested
without a database or a browser. The model is asked only what measurement cannot
answer: whether a page keeps the promise its keywords make (1.5.1), and the
lawful basis an audience may be used under (1.5.3). `nodes/synthesis.py` is the
same division at report scale — it *assembles* every section from node output
and asks a model only for the executive summary, the cited claims and the open
questions.

**The launch verdict is computed, not written.** `synthesis.verdict()` derives
`go` / `go_with_fixes` / `no_go` from the findings, and each blocker carries the
evidence that establishes it. A model asked for a go/no-go over twenty pages of
input will occasionally answer `go` under a critical blocker it summarised
correctly two paragraphs earlier; that is the single worst thing this report
could get wrong, so the rubric is code you can read and argue with. Node 1.6.2
still checks that the prose agrees with the verdict — which is the failure that
actually happens — and one blocking issue buys exactly one re-synthesis.

| Node | What it measures | Model call |
| --- | --- | --- |
| 1.5.1 `landing_page_audit` | LCP, CLS, mobile, form length, trust markers, HTTP status of the pages 1.4.5 mapped | message match only |
| 1.5.2 `tracking_probe` | conversion actions, staleness, and a real browser loading the conversion page to see whether a beacon fires | none |
| 1.5.3 ⛳ `audience_consent_check` | list sizes and eligibility from the account | lawful basis, then a data officer decides |
| 1.5.4 `opportunity_sizing` | clicks, conversions, CPA and revenue per budget, capped by the demand that exists | assumptions prose only |
| 1.6.1 `report_synthesis` | assembles §11's contract, renders the markdown, writes the `report` row | summary and claims |
| 1.6.2 `report_critique` | unsupported claims, contradictions, dead citations, verdict consistency | yes, cross-family |

Three things worth knowing before changing any of it.

- **The synthetic check never claims a round trip it did not observe.** Google
  reports conversions hours late, so a probe fired now cannot be confirmed in the
  API now. What the probe *can* confirm is that a beacon fired and that its
  `send_to` matches a conversion action the account is actually receiving
  conversions on. `latency_min` stays `null` unless a conversion genuinely
  post-dates the probe.
- **Node 1.6.1 gathers exactly what the report cites.** The executor fails any
  node citing evidence it did not gather, and the report is a fold of nineteen
  nodes' findings — so 1.6.1 loads the union of their citations, which is what
  makes that check meaningful for the report too.
- **A row that does not fit the contract is dropped, named and counted.** Nineteen
  node output models feed one contract and the two will drift; `dropped_rows` on
  1.6.1's output is where that shows up. Losing one row of one section is always
  better than losing a forty-minute run at its very last node.

## Known limits of P5b

- **The conversion page is a project setting nothing writes yet.** 1.5.2 probes
  `settings["conversion_probe_url"]`; without it the synthetic check is
  `inconclusive` and says which setting to fill in. The wizard field is P7's.
- **`latency_min` is usually `null`,** for the reason above. It is filled in only
  when a later run resolves an earlier probe.
- **1.6.1's re-run happens inside 1.6.2.** PRD §10 says "1.6.1 re-runs once"; the
  executor is a wavefront with no facility for a node to send another node round
  again, so the *function* is re-run and the same `report` row is rewritten. The
  only visible difference is that the second call's cost lands on 1.6.2's
  `NodeRun`.
- **`depends_on` for 1.6.1 is written out, not derived.** It is read at import
  time and the registry is what imports the module. `test_stage_1_6.py` asserts
  the tuple equals every non-report node the registry knows, so a node added in
  a later phase fails the suite rather than silently never reaching the report.
- **Two shipped components disagreed and were reconciled in `synthesis.py`, not
  in either of them**: 1.2.1 reports `metric_delta` as a number where §11 wants a
  label, and 1.3.1's `overlap_score` is a relative rank where §11 wants the 0–1
  the report renders as a percentage. Both are converted at the boundary, with
  the raw figure carried along. Flagged for a ruling.
- **Opportunity sizing refuses to forecast without a measured conversion rate.**
  An account with no history gets no scenarios and a blocker saying why, rather
  than a confident-looking projection built on an invented number.

## The interface (P6)

Everything a workspace needs before a run has a screen. An admin can invite all
four roles, store and test keys, choose models, assign every approval gate and
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
- **The wizard's gate list is the registry's.** `agent/gates.py` derives it from
  the nodes that declare `gate=True` and adds only the copy a `NodeSpec` has no
  field for. Assignees are written to `settings["gate_assignees"]`, which is the
  key `orchestrator/approvals.py` reads — the writer and the reader name the
  same constant on purpose.

## Known limits of P6

- ~~Stage 1.5's gate does not appear in the wizard yet.~~ **Closed by P5b**:
  1.5.3 registered, the wizard's list derives from the DAG, and the gate
  appeared with no change to the frontend. That was the design working.
- **The per-gate SLA is stored and not yet used.** It lands in
  `settings["gate_sla_hours"]`; reminders are P8, which is what will read it.
- The run console, report viewer, evidence explorer and approvals inbox are P7.
  `/approvals` and `/evidence` render what they are and when they arrive rather
  than 404ing out of a navigation item every role can see.
- **The overview shows setup readiness, not the report's `GO` / `NO-GO`.** The
  verdict now exists — `payload.launch_readiness` on a stored report — but the
  screen that surfaces it is the Report Viewer, which is P7.
- `GET /models` needs `settings_write`, because the only screen that consumes it
  is the admin-only routing step. An operator's read-only view of that step
  renders the ids already stored on the project.
- **SMTP is reported, not edited.** It is deployment configuration
  (`SMTP_HOST`, `SMTP_FROM`), so Settings says whether it works and what
  happens when it does not — invites degrade to a copyable link.
- The cost estimate is labelled `assumed` until three full runs have finished,
  then `measured`. It is never presented as a measurement it is not.

## Business-context documents

Step 1 of the wizard takes files as well as text. A PDF, a Word file (`.docx`),
a CSV or plain text/markdown is read on upload, split into passages, and stored
as evidence the research nodes can cite.

| | |
| --- | --- |
| Where | Setup wizard, step 1 — **Background documents** |
| API | `POST/GET /projects/{id}/documents`, `DELETE /projects/{id}/documents/{doc_id}` |
| Permission | `project_write` to add or remove, `read` to see the list |
| Limits | 20MB and 400,000 characters per file, 400 passages, 25 files per project |

Four decisions behind it.

- **It becomes evidence, not configuration.** `product_context` is repeated to
  every node as text nobody may cite, and a 30-page positioning deck cannot go
  there — it would be in twenty-three prompts and quotable in none of them. An
  uploaded file is chunked into ~1,100-character passages with an id each, so a
  claim about the pricing tiers points back at the page it came from.
- **Six nodes ask for it, and an absent library is not a gap.** 1.1.1, 1.1.2,
  1.1.3, 1.1.5, 1.3.4 and 1.4.1 add `Need(BRAND_DOC, optional=True)`.
  `optional` is what stops "no documents uploaded" being reported as a degraded
  source all the way out to the report's `insufficient_evidence`.
- **Extraction failures are loud.** A scanned PDF has no text layer and `pypdf`
  returns empty strings for every page rather than an error; a legacy `.doc` is
  an OLE blob. Both are refused with the sentence that says what to do instead,
  because the alternative is a filename with a tick beside it and a run that
  cannot read a word of it. Anything partly readable is accepted *and* says
  what it lost — pages skipped, characters dropped at the budget.
- **The original bytes are not kept.** The Volume belongs to `worker` (§5.2)
  and `api` receives the upload, so the extracted text is the artifact. The
  same file twice is a `409` keyed on its SHA-256, not a second copy.

`make browser-documents` drives the whole thing in Chromium: a real PDF, a CSV,
a scan that must be refused, the duplicate that must be refused, the review
step's count, and the delete that has to take the passages with it.
