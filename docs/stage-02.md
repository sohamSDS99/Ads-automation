# Stage 02 — the Campaign Planning Agent

The runbook. What the stage does, who decides what, what each failure looks
like from a screen, and how to run every gate locally.

Stage 02 turns an **accepted research report** into a **frozen campaign plan**:
an envelope, an allocation, a campaign tree, a measurement plan and a ranked
test backlog, every figure of which resolves to a calculation somebody can
open. It writes nothing to Google Ads — that is Stage 04.

Read this alongside `PRD files/prd-campaign-planning.md`. Where the two
disagree, the PRD is the contract and this file is the mistake.

---

## The shape of it

```
research run ──(a human accepts it)──> ResearchAcceptance
                                            │
                                            │  PlanInput  (§4.3, built once)
                                            ▼
                    ┌──────────── 20-node plan DAG, 12 waves ───────────┐
                    │  2.1 targets ⛳G1 ⛳G2                              │
                    │  2.2 budget  ⛳G3                                  │
                    │  2.3 slate   ⛳G4                                  │
                    │  2.4 structure                                     │
                    │  2.5 measurement + tests                           │
                    │  2.6 synthesis + critique                          │
                    └────────────────────┬───────────────────────────────┘
                                         ▼
                                   CampaignPlan  (draft)
                                         │  PLAN_FREEZE
                                         ▼
                                   CampaignPlan  (frozen, immutable)
```

Four gates, no more. Everything else runs unattended, and nothing auto-approves.

---

## Accepting research

**Who:** anyone with `APPROVAL_DECIDE` (admin or approver).
**Where:** the research report, then the Campaign Planning tab.
**API:** `POST /runs/{research_run_id}/accept`.

Eligibility is eight named blockers (E1–E8), each of which renders its own
sentence rather than a disabled button:

| Code | What it means | What to do |
|---|---|---|
| `no_accepted_research` | Nothing has been accepted for this project | Open the report and accept it |
| `research_schema_unsupported` | The report was written by an older contract | Re-run research; the error names both versions |
| `research_says_no_go` | The verdict is `no_go` | An **admin** may accept it with a written reason, which is stored on the acceptance and printed on the plan's cover page |
| `plan_in_flight` | A plan run already holds the project lock | Open the running plan; only one at a time |
| `frozen_plan_exists` | This acceptance already produced a frozen plan | Accept newer research, or read the frozen one |
| `research_gates_open` | A research gate is undecided | Decide it; a report with an open gate is not a finished report |
| `run_unfinished` | The research run did not succeed | Re-run it |
| `no_report` | The run finished without writing a report | Re-run it |

Exactly one acceptance per project is *current*, enforced by a partial unique
index. Accepting a second research run supersedes the first — see **When the
research moves on**.

---

## Starting a plan

**Who:** `PLAN_EXECUTE` (admin or operator).
**API:** `POST /projects/{project_id}/plan/runs` → `202 {run_id}`.

The run takes a Redis project lock. A second start returns `409` naming who
holds it. `PlanInput` is assembled once at start, hashed into `Run.input_hash`,
and passed read-only to every node: no plan node re-reads the `Report` row and
none re-runs a research node.

Cost is capped per run at **`max_plan_cost_usd`, default $8** — a separate
ceiling from research's `max_run_cost_usd`, and deliberately so: raising the
research cap must not quietly raise what a plan may spend. Set it per workspace
under **Settings → Workspace → Budget cap per campaign plan**, or per project in
`Project.settings`; narrowest scope wins. The console's spend meter draws
against `GET /runs/{id}`'s `cost_cap_usd`, which is the number the executor will
actually enforce. Hitting it cancels the remaining nodes, persists the completed
ones and marks the plan `blocked` with `budget_cap_reached`.

---

## The four gates

| Gate | Node | Question | Who decides |
|---|---|---|---|
| **G1** | 2.1.3 | Are these the right targets, and are they affordable? | Marketing lead |
| **G2** | 2.1.4 | Is this what a qualified lead is? | Sales lead |
| **G3** | 2.2.4 | Is this the budget, and this the split? | Budget owner |
| **G4** | 2.3.1 | Is this the channel slate? | Marketing lead |

All four need `APPROVAL_DECIDE`. An `operator` gets `403`. Deciding a gate
resumes the branch **inline** — the decision enqueues the run in the same
request, which is why §17 PF2's five seconds is achievable and why moving
resumption onto a poller would break it.

G3 is the one with an editor. An approver can change the split; the recalc goes
through the server (`POST /approvals/{id}/recalc`) because no forecast
arithmetic exists in TypeScript. A split that does not sum to the envelope is
refused client-side by the remainder chip and server-side with a `422` naming
the delta. Pushing a campaign below its learning threshold is allowed, warned
about inline, and carried into the plan as a risk.

Two approvers deciding the same gate: the loser gets `409` and the UI names who
decided.

---

## Reading the plan

`GET /plans/{plan_run_id}` returns the row plus the payload as opaque JSONB.
Where the row and the payload disagree, **the row wins**: `payload.plan_status`
is what node 2.6.2 wrote, `status` is what the freeze transaction locks on.

The Plan Viewer renders whatever the payload holds and names what is absent, so
a run halted at G3 still opens — with objectives, a media plan, no structure,
and the gate decisions somebody came to read.

Every headline figure is a `Number`: `{value, unit, calc_evidence_id,
confidence, label}`. Click through to `GET /plans/{plan_run_id}/calcs` for the
`PlanCalc` row behind it — formula id, inputs, inputs hash, result.

---

## Freezing

**Who:** `PLAN_FREEZE`.
**API:** `POST /plans/{plan_run_id}/freeze` with `{confirm_version}`.

One transaction: assert all four approvals are `approved`, assert the critique
returned no blocking issue, mint `version = max(version) + 1` for the project,
copy the four approval ids onto the row, stamp `frozen_at`/`frozen_by`, mark any
prior frozen plan `superseded`, write an `AuditLog` row.

A frozen plan is immutable in `payload`, `markdown` and `version` — a database
trigger refuses the `UPDATE`, and there is no way round it. Changes mean a new
version from a new plan run.

Refusals come back as `409` with a list of `blockers`, each with a code:

| Code | Meaning |
|---|---|
| `gate_undecided` | A gate is still pending; the blocker names it and its assignee |
| `gate_rejected` | A gate was refused; the plan is `blocked`, not merely unfinished |
| `blocking_critique` | 2.6.2 found a blocking issue; the first three are quoted |
| `plan_blocked` | The row says `blocked` and the payload disagrees — refused rather than resolved in favour of either |
| `source_superseded` | Newer research has been accepted since; re-run the plan first |

Freezing an already-frozen plan at the same version is an idempotent `200`. At
a different version it is a `409` — the screen is stale, reload it.

Unfrozen plans are **version 0**, and the Viewer renders that as an em dash
rather than "v0". `version > 0` is the test for "has been frozen".

---

## Exports

`POST /plans/{plan_run_id}/export?format=…` → `202 {job_id}`, generated in the
worker, downloaded through `GET /exports/{id}/download`.

| Format | What it is for |
|---|---|
| `PDF` | The document that gets circulated and signed |
| `DOCX` | The same, editable, with a real TOC field |
| `MD` | The source of truth the PDF and DOCX render from |
| `JSON` | The machine handoff to Stage 03 |
| `EDITOR_CSV` | A ZIP of `campaigns/ad_groups/keywords/negatives.csv` for Google Ads Editor — imports with zero errors and counts matching the plan |
| `XLSX` | The media plan with **live formulas**, so finance can flex a number and watch the total move |

Every export of a non-frozen plan is watermarked `DRAFT — NOT APPROVED` on
every page. Exporting a frozen plan twice produces byte-identical output.

---

## When the research moves on

§4.4. A newer accepted research run **does not invalidate a frozen plan**. It
marks every plan built from the older acceptance `source_superseded`:

* the frozen plan stays valid, downloadable and exactly as it was signed;
* the Plan Viewer shows a banner, with a link to plan against the current
  research;
* a **draft** with a superseded source cannot be frozen — the freeze blocker
  above is what refuses it.

The flag is **derived, not latched**: it is exactly "the acceptance this plan
names is no longer the current one". Withdrawing a newer acceptance and
re-accepting the older run clears it again, and an audit row is written in both
directions (`plan.source_superseded`, `plan.source_restored`).

Withdrawing the only acceptance also marks its plans — there is no current
acceptance, so the plan's source is not it.

Three places recompute it, and all three are needed:

| When | Why |
|---|---|
| An acceptance is made or withdrawn | The transition itself (`planning/staleness.refresh_for_project`) |
| A plan row is written | A run takes minutes; research can be re-accepted while it runs, and the refresh above finds no row for a run that has not written one yet |
| A freeze is attempted | The gate decides on a value it computed, never on a stored one nobody has checked |

`source_superseded_reason` says what the project has **now**, not what happened:
`replaced` means some other acceptance is current and there is research to plan
against; `withdrawn` means nothing is current and there is not. The banner keys
its offer off that, because "plan against the current research" is an offer only
one of them can honour.

---

## Failure modes

The ones an operator will actually meet, and what each looks like.

| Symptom | Cause | What to do |
|---|---|---|
| The Campaign Planning tab is locked | No accepted research | Accept the report; the tab unlocks immediately |
| Start returns `409` naming somebody | The project lock is held | Open the running plan |
| Start returns `422` about a schema version | The report predates the current `PlanInput` contract | Re-run research. Zero tokens were spent |
| The budget card says a source is degraded | The Google Ads forecast service was unavailable | Nothing to fix. `forecast.traffic_v1` fell back to derived arithmetic on Stage 01 demand; the band is wider and the substitution is named on the card and carried into the plan |
| `structure_collision_check: skipped` | No Google Ads account is connected | Expected. 2.4.1 emits no collision report; a name collision becomes a **blocking** issue at freeze time, so connect the account before freezing if you care |
| A gate card shows the ceiling as unknown | The CRM close-rate data is too thin for `max_cpa` | The approver enters a target by hand; it is recorded as `basis: human_supplied` |
| 2.2.3 returns `infeasible` | The envelope cannot fund the slate | It names the minimum viable envelope. Cut markets or channels — it will not spread thin silently |
| The plan is `blocked` with a duplicate keyword listed | A term reached two ad groups; 2.6.1 re-ran once and it survived | Re-run the plan. `structure.grouping_v1` assigns each term to one group |
| An edit at G3 returns `422` | The split does not sum, or it fails `output_model` | The message names the field or the delta. The gate stays pending; nothing partial is written |
| A run is `failed` with a stale heartbeat | The worker was killed | Resumable from the last checkpoint. Zero completed nodes re-execute and decided gates are not re-asked |
| The plan states no envelope | 2.2.4 produced no `allocation.split_v1` row | The figure is **omitted rather than stated without its calculation**. The critique says so, and the gap is upstream of the plan |

---

## The rules that are not negotiable

These are enforced in code and a change here is a PRD change, not a patch.

1. **Stage 02 never writes to Google Ads.** Plan connectors subclass
   `ReadOnlyConnector`; instantiating a mutate service under `stage='plan'`
   raises.
2. **The LLM never does arithmetic.** Every number comes from a registered
   `@formula` in `calc/`, is persisted as a `PlanCalc` row plus a `derived`
   Evidence row, and is cited by `calc_evidence_ids`. A figure with no
   calculation fails schema validation — `Number.calc_evidence_id` is required.
3. **Google thresholds and benchmarks live in `planning_constants.yaml`** with
   a source and a `reviewed_at` date. A constant without a source fails
   startup, naming the key.
4. **No raw CRM row reaches a prompt or the payload.** `calc/` reads rows; the
   model reads aggregates. `tests/test_pii_canary.py` plants canaries in five
   CRM columns and looks for them in the prompts, the payload and all six
   exports.
5. **Gate 1.5.3 is authoritative over consent.** A market it refused carries no
   audience channel, no audience test and no audience-list dependency, and an
   offline upload with no recorded lawful basis is a blocking critique issue.
6. **A frozen plan is immutable.** Enforced by a trigger. Do not work around it.

---

## Running the gates locally

Everything below runs from the repo root.

```bash
# The stack. From a worktree, use your own compose project and subnet.
COMPOSE_PROJECT_NAME=ads-s2p7 \
  COMPOSE_FILE=docker-compose.yml:docker-compose.worktree.yml \
  WORKTREE_SUBNET=172.31.216.0/24 WORKTREE_WEB_PORT=3107 \
  docker compose up -d
make migrate

make test-api            # the unit suite — no database needed
make test-integration    # the DB+Redis suite, inside the compose network
make eval                # Stage 01's ten node fixtures + Stage 02's five plan fixtures
make guards              # route guards + the arithmetic-isolation grep
make typecheck lint      # mypy strict + ruff, and tsc + eslint for the web app

make coverage-calc       # PQ2: >= 85% on calc/ and planning/
make coverage-plan       # PQ2: >= 80% on nodes/plan, planning/ and export/, each on its own

./scripts/verify-s2p7.sh # the phase gate: all of the above plus the §17 index
```

**The integration suite cannot run on the host.** `postgres`, `redis` and `api`
publish no host port, and running the suite against a local Postgres fails
~14/19 with asyncio "Future attached to a different loop" — anyio drives the
tests on a per-test loop while `pytest_asyncio` fixtures use the session loop.
Docker is the only path; `make test-integration` is the door.

**Browser checks** run inside the `worker` image, which already carries
Playwright and its browsers. Use `/app/.venv/bin/python` — `uv run --with
playwright==1.49.0` installs a second Playwright that addresses a chromium
build the image does not have, and it breaks on the next image rebuild.

```bash
make browser-s2p6a   # the Plan Console
make browser-s2p6b   # the budget gate's allocation editor
make browser-s2p6c   # the Plan Viewer, structure tree, freeze dialog and diff
make browser-s2p7    # the staleness banner and the re-plan offer
```

---

## Where things live

| What | Where |
|---|---|
| The crossing contract | `apps/api/src/agent/schemas/plan_input.py` |
| The plan contract (§12) | `apps/api/src/agent/export/plan_contract.py` |
| The nodes | `apps/api/src/agent/nodes/plan/stage_2_*.py` |
| The formulas | `apps/api/src/agent/calc/` |
| The non-LLM planning logic | `apps/api/src/agent/planning/` |
| The ten assertions | `apps/api/src/agent/planning/critique.py` |
| The freeze | `apps/api/src/agent/planning/freeze.py` |
| Staleness (§4.4) | `apps/api/src/agent/planning/staleness.py` |
| The constants | `apps/api/src/agent/planning/planning_constants.yaml` |
| The golden fixtures | `apps/api/tests/eval/plan/` |
| The §17 index | `apps/api/tests/test_nfr_stage_02.py` |
