# UI-SPEC — S3-P7, Frontend A

The design contract for Stage 03's read-and-publish surfaces. Written before
the code, verified against it afterwards. PRD §15.1, §15.2, §15.3 A/B/E/H and
§15.4; phase scope from §21.

---

## 0. Mode and direction

| | |
|---|---|
| **Mode** | *Operate* for the landing, the console and the publish dialog. *Read* for the Rulebook Viewer and the asset spec sheet. |
| **Visual world** | The incumbent one, unchanged. §22 fixes the stack and `styles/tokens.css` fixes the palette, the 8px grid, the 10px radius, the type scale and the one authored motion moment. |
| **Where this stage differentiates** | Information design, not a new look. |

A new visual direction is not on the table and asking for one would be the
wrong instinct: every Stage 03 surface sits one click from a Stage 01 or
Stage 02 surface, and a rulebook that looks like a different product than the
run console it was produced by reads as a bolt-on, not as the stage that
governs the other six.

**The one pattern this phase commits to: nothing in the rulebook is an
assertion.** Every rule, every spec figure and every policy area renders with
two chips — a **severity** and an **authority** — and the authority chip is a
control that opens its source. A constant shows its `content_constants.yaml`
key and its `reviewed_at`; a policy area that does *not* apply is still listed,
collapsed, with the reason it does not. This is the whole point of the stage
(C15, §15.3 E), it is the thing a person is being asked to sign, and it is what
makes these screens worth building rather than exporting a PDF.

Anti-goals, carried from the craft floor and named so the review can check
them: no card grid standing in for structure, no hero metric, no eyebrow above
a heading, no section numbers as decoration (`3.1` is a node id and earns its
place), no gradient text, no monospace as a costume — monospace is for hashes,
versions, node ids, constant keys and measured figures only.

---

## 1. Scope

**In (§21, S3-P7):**

| # | Surface | PRD |
|---|---|---|
| 1 | Left-panel rail: status chip + two badges | §15.1 |
| 2 | `/guidelines` — landing with the bindings panel | §15.3 A |
| 3 | `/guidelines/runs/[runId]` — Guideline Console + Rules tab | §15.3 B |
| 4 | `/guidelines/runs/[runId]/rulebook` — draft Rulebook Viewer | §15.3 E |
| 5 | `/guidelines/published` — the living rulebook | §15.3 E |
| 6 | `/guidelines/specs` — asset spec sheet | §15.2 |
| 7 | Publish dialog | §15.3 H |

**Out — S3-P8 owns these and this phase must not scaffold them:** the claims
register table and the signature drawer, person-task cards, the `/approvals`
two-tab inbox, the linter playground, the amendment inbox, the sign-off matrix
editor, and `/compare`. Where a P7 surface must *point* at one of them, it
points at the route and says what will be there — it does not build a
placeholder screen.

**Node count.** §21 says 19 nodes; the shipped DAG is **18** (3.5.3 is S3-P9's).
Nothing in this phase may hardcode either number — the rail counts what
`GET /runs/{id}` returns, exactly as Stage 01's does.

---

## 2. The one API addition, and why

§15.1 rule 3 puts two badges on the rail: red when the current user has an open
person-task on this project, amber when claims expire within 30 days or
unreviewed substantive amendments exist. §15.3 A's *Attention* block asks the
same three questions.

`HumanTask` and `PolicyAmendment` are shipped tables with shipped service
layers, and **neither has an HTTP route**. `/human-tasks` and
`/policy-amendments` are listed in §16, but their UIs are S3-P8's and building
those list APIs here would be scaffolding a future phase.

So this phase adds exactly one read, shaped to the two surfaces that consume it
and to nothing else:

```
GET /projects/{id}/guidelines/attention        READ
  -> { my_open_tasks: [{task_id, task_key, title, blocking_for, status}],
       open_tasks_total: int,
       expiring_claims: int, earliest_expiry: date|null,
       unreviewed_amendments: int,
       signature_stale: bool }
```

One request, one render, no client-side join across three endpoints. It is a
deviation from §21's file list and is flagged for a ruling in the PR body. The
alternative — shipping the rail without its badges — leaves a scope line
unbuilt and hides the only two states on this stage that need a person today.

Everything else reads endpoints that already exist:
`/guidelines/eligibility`, `/projects/{id}/guidelines`, `/guidelines/{id}`,
`/guidelines/published/ruleset`, `/guidelines/{id}/claims`, `/runs/{id}`,
`/runs/{id}/events`, `/guidelines/{id}/publish`, `/guidelines/{id}/export`.

---

## 3. Surface contracts

### 3.1 Stage rail (§15.1)

- The `03 Content guidelines` row is **enabled whenever the project exists**.
  There is no code path that can disable it — not a prop, not a ternary. Any
  lock is a bug (§15.1 rule 1), so the way to keep it true is to give the
  component nothing to compute a lock from.
- Chip states: `Draft v2.0` / `Published v1.3` / `Amendments pending` /
  `Signature stale`. `Not started` renders as **no chip** — the rail is 240px
  and a chip that says "there is nothing here" truncates the row it describes.
  (Shipped behaviour from S3-P0; kept, and now documented as intentional.)
- Badges are dots, not counts: red `--status-failed` for *your* open
  person-task, amber `--status-gate` for expiry or amendments. Each dot carries
  an accessible name stating the count and the reason; the colour is never the
  only carrier.
- Precedence when both a chip and badges exist: chip truncates first, dots
  never truncate. At the narrow rail the row is icon-only and the dots ride the
  icon.

### 3.2 Landing (§15.3 A)

Four stacked blocks, in the order a person arrives with the questions. Blocks 1
and 2 ship; 3 and 4 are completed here.

1. **Status** — published version, when, by whom, `ruleset_version`, mode, and
   `unbound_inputs` stated plainly rather than tucked behind a tooltip.
2. **Action** — Start, enabled by default, with the bindings panel. `blockers[]`
   is the only array that may disable anything; `running_unlinked` renders as an
   informational note beside an enabled button. Two arrays from the server, no
   severity filter on this side.
3. **Attention** — from §2's endpoint. Person-tasks assigned to *anyone* with
   the assignee named, claims expiring in 30 days with the earliest date, and
   unreviewed amendments. Each row links to the S3-P8 route that will action it
   and says so. Empty state is *"Nothing is waiting on a person"*, not a blank
   card.
4. **History** — newest first: version, status, mode, rule count, claim count,
   published by, created. A compare checkbox per row, at most two selected,
   linking to `/compare` (S3-P8). The checkbox ships; the destination is P8's.

`version DESC` alone is not an order — every unpublished draft is `0.0`. Tie
break on `created_at`, and never print `v0` (the `versionLabel` em dash,
already shipped, holds).

### 3.3 Guideline Console (§15.3 B)

`<RunConsole stage="guideline">`. The rail, the DAG and the node panel already
render whatever the API returns, so this route is thin by design (the Plan
Console is 46 lines and this should be comparable).

Two additions:

- `STAGE_TITLE` gains `3.1`–`3.6`, in the voice of the existing entries — what
  the stage decides, said out loud, never repeating the id.
- The node panel gains a sixth tab, **Rules**, on `stage === "guideline"` only.
  It lists the `Rule` objects this node produced, each with:
  its `message` (written to the person who is blocked, so it renders as the
  row's sentence — not the `id`), a **severity chip**, an **authority chip**
  showing `source`/`reference` with `reviewed_at`, its scope when narrower than
  everything, and **its matcher rendered in plain language**.

  Plain language means one sentence per matcher `kind`, derived from the
  discriminated union (`term_set`, `regex`, `length`, `count`, `ratio`,
  `enum_allow`, `claim_licence`, `offer_binding`, `disclosure`) — *"Forbids the
  terms guaranteed, risk-free (exact, en)"*, *"Headlines are 30 characters or
  fewer"*. A matcher kind the UI does not know renders its `kind` and its JSON
  rather than an empty row: an unrenderable rule is still an enforced rule.

  **No rule is evaluated here** (§15.4 rule 2). This tab describes matchers; it
  never runs one. "Test this rule" deep-links to the playground route with the
  rule prefilled — the playground itself is S3-P8.

### 3.4 Rulebook Viewer (§15.3 E)

One component, two routes: a draft at `/runs/[runId]/rulebook` and the
published rulebook at `/published`. Same sections, different source and a
different header — the draft is **watermarked** and the published one carries
the Publish control's absence.

- Sticky TOC left, rulebook right, header pinned: version, status,
  `ruleset_version`, mode, unbound inputs, **Publish** (only for
  `GUIDELINE_PUBLISH`, only at `ready_to_publish`) and the Export split-button.
- Sections: **Brand rules** (voice words as cards with do/don't examples quoted
  from real ads, lexicon in two columns with per-entry severity) · **Claims
  register** (grouped by status, expiry countdowns) · **Policy profile**
  (applicable areas as cards with obligations; `not_applicable` collapsed but
  present) · **Asset specs** (the matrix, see 3.5) · **Governance** (sign-off
  matrix, review triggers, learned rules).
- Every rule renders severity + authority. Clicking authority opens the source:
  a policy URL, a signature receipt, or a disapproval event.
- The payload is opaque JSONB on the wire and **every field is resolved from a
  candidate list, not a path**. A missing section renders a named absence, never
  a blank. The rulebook must stay readable on a run halted at G5 — that is the
  state somebody opens it in.
- `critique_issues` on the payload drives the critique verdict. Derive the
  verdict from the list; never read a label that can disagree with the list
  under it.

### 3.5 Asset spec sheet (§15.2)

A matrix, campaign type × asset type, from `asset_specs.sheet`. Each cell shows
the limit and, on hover and in the accessible name, the `content_constants.yaml`
key and its `reviewed_at`. A constant whose source is `unverified` is marked
**in the cell**, not in a footnote — C15 says these ship unverified until a
human checks them, and a spec sheet that looks equally authoritative in both
states is the failure mode the flag exists to prevent.

`scope: scoped | unscoped` is stated at the top: an unbound run emits every
campaign type, a plan-bound run only the slate's. Launch minimums render below
the matrix with `blocking_for_launch` distinguished.

Wide table, so: one `overflow-x: auto` container, and the page body never
scrolls horizontally at 390px.

### 3.6 Publish dialog (§15.3 H)

Lists, before anything is typed: G5 and G6 with decider and timestamp; the H1
signature with signer, set hash and expiry; H2's status and what it blocks; the
critique verdict; the rule count by category and severity; and the
`ruleset_version` about to be minted. Requires **typing the version** to
confirm. States plainly that the published payload is immutable.

A `409` returns **every** blocker, not the first — the API is built that way on
purpose — so the dialog renders the whole list with each `fix_url`, and stays
open. A `version_conflict` names what was expected and what was submitted.

The publish response is a **receipt**, not a guideline: `{guideline_id, version,
status, ruleset_version, rule_count, superseded[], already_published}`. It is
never written into the guideline read cache with `setQueryData` — invalidate.

---

## 4. Non-functional (§15.4)

1. The rule browser renders 800 rules at ≤16 ms, virtualised. One virtualiser
   over a flattened list, `estimateSize`/`getItemKey` memoised, leaves
   fixed-height and unmeasured. Assert virtualisation by counting DOM rows.
2. **No rule evaluation in TypeScript.** Not in the Rules tab, not in the
   viewer, not "just for the preview".
3. Publish, start and export carry `X-CSRF-Token`; SSE reconnects with
   `Last-Event-ID` and reconciles via `GET /runs/{id}`.
4. `viewer` reads every surface in this phase and changes none of it. Controls
   are wrapped in the permission they need; the API refuses the rest regardless.
5. Every figure read off the payload goes through one accessor. A blank figure
   is indistinguishable from an absent one, so absence is rendered as a named
   absence.

## 5. Accessibility and states

- Tabs that change the URL are links with `aria-current`, never `role="tab"`.
  `role="tree"` promises arrow keys and selection this product does not have —
  use labelled disclosure buttons carrying `aria-expanded` on the control.
- Every interactive element ships default, hover, focus, active, disabled,
  loading, error. Skeletons for loading, never a centred spinner.
- Empty states teach the surface: *"Nothing published yet. Until a version is
  published, Stage 04 has no ruleset to lint against."*
- Colour is never the only carrier — severity chips carry their word, badges
  carry an accessible name.
- 1440 and 390, light and dark, `operator` and `viewer`. Long words in claim
  text and policy references wrap; the body never scrolls horizontally.

## 6. Done means

1. An `operator` starts a guideline run on a project with no research and no
   plan, entirely from the UI, and watches every node stream to completion.
2. They open the draft rulebook, read all five sections, and publish it once
   G5, G6 and H1 are done — the dialog having told them, before they typed the
   version, exactly what was outstanding.
3. A `viewer` reads all of it and can change none of it; the signature and
   publish controls are absent, not disabled.
4. The `03` tab is never rendered in a locked state, in any state of any
   project.
5. `tsc`, `eslint`, `next build`, the route-guard check and the impeccable
   detector are clean; the browser check passes at 1440 and 390 for both roles.
