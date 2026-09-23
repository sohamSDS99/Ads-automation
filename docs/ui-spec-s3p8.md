# UI-SPEC — S3-P8 Frontend B

**Phase:** S3-P8 · **Stage:** 03 Content Guidelines · **Source of truth:** `PRD files/prd-content-guidelines.md` §15.2–15.4
**Status:** contract — agreed before implementation, audited after (see `docs/ui-review-s3p8.md`)

This is a design contract, not a suggestion. Every rule below is either traceable
to a PRD clause or to a server behaviour that already exists. Where this document
and the PRD disagree, the PRD wins and this file is wrong.

---

## 0. The one idea this phase is organised around

Seven of these screens are ordinary. One is not.

The claims register carries a **legal signature that an administrator cannot
forge**, and the interface is the last place that promise can be broken. Every
design decision in §3 exists to make one sentence true: *the person whose name is
on the receipt read the exact set of words that got hashed.* If a control, a
spinner, an optimistic update or a stale cache can put those two things out of
step, it is a defect at the same severity as a server-side auth bypass.

That is why §3.4 forbids optimistic rendering in the signature path, and why the
submit control is **absent** rather than disabled for everyone else.

---

## 1. Design system — what is already decided

Tokens live in `apps/web/styles/tokens.css`. This phase adds **no new tokens** and
**no new colour values**. Anything not expressible in the existing scale is a
signal the design is wrong, not that the palette is short.

| Concern | Token | Never |
|---|---|---|
| Page / raised surface | `--bg`, `--surface`, `--surface-raised` | a hardcoded hex |
| Hairlines | `--border`, `--border-strong` | `border-gray-200` |
| Body / secondary / hint | `--fg`, `--fg-muted`, `--fg-subtle` | a fourth grey |
| The single accent | `--accent`, `--accent-hover`, `--accent-soft` | a second brand colour |
| Type | `--text-xs … --text-2xl` (12/14/16/20/28/36) | arbitrary `text-[13px]` |
| Rhythm | `--space-1 … --space-6` on an 8px grid | odd-pixel padding |
| Overlays only | `--shadow-overlay` | shadow on an inline card |

**Status colour is a vocabulary, not decoration.** Stage 01/02 established it and
Stage 03 inherits it unchanged:

| Meaning | Token | Used here for |
|---|---|---|
| in flight | `--status-running` | a lint running, a run streaming |
| settled well | `--status-success` | `approved`, `applied`, `signed` |
| waiting on a person | `--status-gate` | `pending`, `awaiting signature`, `unclassified` |
| refused / dangerous | `--status-failed` | `rejected`, `blocking` findings, `signature_affecting` |
| not applicable | `--status-skipped` | `not_applicable`, `dismissed`, expired |

**Colour never carries meaning alone** (WCAG 1.4.1). Every status chip pairs its
colour with a word. Every lint finding pairs its severity colour with a severity
label and an icon. A red row also says why it is red.

---

## 2. Cross-cutting laws

These are the rules a reviewer should check first, because breaking one is cheap
to do and expensive to find.

1. **No rule evaluation in TypeScript** (PRD §15.4.2). The playground posts to the
   server on every evaluation. A matcher, a severity ladder or a claim-licensing
   check reimplemented in the client is a defect — it is precisely how a writer
   and the pipeline come to disagree. The client may highlight spans the server
   returned; it may not decide where they are.
2. **Absent, not disabled** (PRD §15.4.3, §15.3 D). For the signature submit and
   for person-task controls, someone who may not act does not see a greyed
   control. They see a sentence naming who may. A disabled button teaches people
   to hunt for the enabled state.
3. **The server decides permission; the client only renders it.** Use `can_*`
   fields the API already computes (the pattern `approvals.ts` established) and
   `<Can>` / `<Guarded>` for chrome. Never re-derive "approver, or assignee, or
   admin" client-side.
4. **Every mutation carries `X-CSRF-Token`** — `apiFetch` already does this; do not
   hand-roll `fetch`.
5. **RFC 9457 problems are rendered, never re-worded.** The API writes sentences
   meant for the person reading them ("Only the person named as this project's
   legal owner may sign its claims"). Show `ApiError.detail`. Inventing a
   friendlier message loses the only actionable part.
6. **No secret ever reaches a component.** No response carries `password_hash`,
   `ciphertext`, `nonce`, `token_hash` or a re-auth token (PRD §16 rule 6). The
   re-auth token exists only as a local `const` inside one submit handler.
7. **Virtualise anything unbounded.** 500 claims and 800 rules at ≤16 ms frame
   budget (PRD §15.4.1) via `@tanstack/react-virtual`, already a dependency.

---

## 3. The claims register and signature flow

Route `/projects/[id]/guidelines/claims` · PRD §15.3 C

### 3.1 The table

Virtualised rows over `GET /guidelines/{id}/claims` → `{claims[], set_hash, legal_owner_id}`.

Columns, in this order: **claim text** (truncated to two lines, full text on
expand — never on hover alone, that is unreachable by keyboard), **type**, **risk
tier**, **status chip**, **evidence count**, **market scope**, **expiry
countdown**, **signer**.

- Expiry countdown is a relative phrase with the absolute date available:
  `in 24 days` with `2026-10-17` as its `title` and in the expanded row. Under 30
  days it takes `--status-gate`; expired takes `--status-skipped` and reads
  `expired`, not `-24 days`.
- Evidence count links into the Evidence Explorer. Zero evidence on a
  `substantiated` claim is itself worth showing in `--status-failed` — it means
  the register disagrees with itself.
- Sort and filter are client-side over the loaded set (the whole register is one
  response); they never re-order a set being signed. See 3.4.

### 3.2 Who sees what

Driven by `legal_owner_id` from the response compared against `useSession().user.id`.

| Viewer | Sticky bar |
|---|---|
| The named legal owner, claims pending | **`N claims awaiting your signature`** + `Review and sign` |
| Anyone else | `Awaiting signature from {legal owner name}` — no control |
| `admin` | the same sentence, plus `Reassign legal owner` (see 3.6) |
| No matrix yet | `This project names no legal owner. Decide G6 first.` linking to the gate |

The last row matters: the server answers `409 No sign-off matrix`, and a screen
that renders a signature button in that state is teaching the user to click into
an error.

### 3.3 The signature drawer

A right-hand drawer, not a modal — the register stays visible, because "what am I
signing against" is the question being answered.

Per claim row, all visible without a second click: the claim, its **surface
forms** (these are what the signature actually licenses), the evidence inline and
expandable, where we already say it, and the proposed expiry.

Per claim controls: **Approve** / **Reject**, an optional note, and an editable
expiry date.

- **Bulk approve exists; "approve all unseen" does not** (PRD §15.3 C.2). Bulk
  approve applies only to rows the signer has scrolled into view and expanded —
  the row carries a `touched` flag set on intersection + expansion, not on render.
- A running counter — `12 of 50 decided` — blocks submit while any selected claim
  is undecided. The counter is the primary affordance, not a toast on failure.

### 3.4 The step-up dialog — the part that must not be clever

Opens on submit. Contains, in this order:

1. The **exact attestation statement** being signed, in full, as prose. Not a
   summary, not a link to it.
2. The counts: `47 approved · 3 rejected`.
3. The **`set_hash` in `--font-mono`**, with a copy button.
4. A password field.

Rules:

- The password field is `type="password"`, `autoComplete="off"`, is never written
  to state that outlives the dialog, and is cleared on unmount (PRD §15.4.4).
  Hold it in a ref; do not put it in a form library.
- Submit posts `POST /auth/reauth` then `POST /guidelines/{id}/claims/sign` with
  `{decisions, statement, set_hash, reauth_token}`. The re-auth token is a local
  `const` — never state, never a query cache, never logged.
- **`set_hash` is echoed from the list response, never computed client-side.** The
  server recomputes and answers `409 Register has moved` if it drifted. That 409
  renders as a full-drawer interstitial: *the register changed while you were
  reading it*, with a `Re-read the register` action that refetches. It does not
  render as a toast — a signer must not be able to dismiss it and try again.
- **No optimistic UI anywhere in this path.** The drawer shows a spinner and the
  table does not change until the server has answered.
- `401 Password incorrect` renders inline under the field and leaves every
  decision intact. Repeated failures hit the same lockout as sign-in; render
  `retry_after_seconds` from the problem.
- The endpoint is **idempotent on `set_hash`** — a re-submit of an identical set
  returns `200` with the existing signature. The UI therefore treats a duplicate
  submit as success, not as a double-signature.

### 3.5 The receipt

Rendered in place of the drawer body on success: signer, timestamp, method,
`set_hash` (mono), expiry, approved/rejected counts, and **Export signature
record**. Reachable afterwards at `GET /claims/{id}/signature` so it is not a
one-time view — a receipt you can only see once is not a receipt.

### 3.6 Reassigning the legal owner (admin)

A confirm dialog that states, **before** the confirm control is reachable, the
exact number of signatures the reassignment will void and the number of claims it
re-queues — counted by the server, not estimated by the client. Requires a typed
reason. Copy names the consequence in plain words: *"3 signatures by Dana Okafor
will be voided. 47 claims return to `pending` and must be signed again."*

---

## 4. Person-task cards

PRD §15.3 D. Rendered in the `/approvals` second tab and inline on the Stage 03 landing.

Contents: title, instructions, required artifacts as a **checklist**, a file-upload
area, a reference field, an expiry date, the assignee's name **prominently**, and
`blocking_for` as a chip — `Blocks publish` or `Blocks launch`.

- For anyone who is not the assignee **every control is absent** — not disabled —
  and the card reads `Assigned to {name}` (PRD §15.3 D).
- Upload posts multipart to `POST /human-tasks/{id}/attachments`. Show per-file
  progress, per-file failure, and never a single aggregate bar that hides one
  failed file among five.
- The checklist is the submit gate: submit stays absent until every required
  artifact has an attachment.

---

## 5. `/approvals` — two tabs, one badge

PRD §15.2. Tabs: **Decisions** (Stage 01–03 gates, unchanged) and **Signatures &
attestations** (person-tasks). One sidebar badge summing both. No third inbox.

**Correction this phase must make:** `/approvals` is currently wrapped in
`<Guarded permission="approval_decide">`. A person-task assignee holds
`claim_sign` / `attest_submit` and an `approver` holds all three — but the guard as
written would refuse a future role that has tasks and no gates. Move the guard
down: the **page** is reachable by anyone signed in; the **Decisions** tab is
guarded on `approval_decide`; the **Signatures** tab renders the caller's own
tasks. A tab with nothing in it says so; it does not disappear, because a
vanishing tab is indistinguishable from a bug.

The badge sums `GET /approvals?mine=true` and `GET /human-tasks?mine=true&status=open`.
If one call fails the badge renders the half it has and does not show `0` — a
false zero on an inbox is worse than no number.

---

## 6. Linter playground

Route `/projects/[id]/guidelines/lint` · PRD §15.3 F

The single best adoption lever in the stage: it turns the rulebook from a document
people are supposed to have read into something they can ask a question of in four
seconds. Design for that sentence.

- Inputs: a copy textarea, surface, campaign type, market, language, optional
  image drop.
- **Available to every role including `viewer`** — it has no side effects.
- Posts `POST /guidelines/{id}/lint` (text) or `/lint/image`, **debounced 400 ms**,
  with the in-flight request aborted on the next keystroke via `AbortController`.
  Budget: findings visible **< 1.5 s** from last keystroke (PRD §21 exit criteria).
- A verdict chip sits at the top: `pass` / `findings` / `blocked` / `indeterminate`.
  `indeterminate` is a first-class state and must never be styled as a pass — it is
  what the server returns when OCR is unavailable.
- Findings render **inline with the offending span highlighted** in the copy, plus
  rule name, severity and authority. Clicking the authority opens its source — a
  policy URL, a signature receipt, or a disapproval event.
- Span highlighting uses the server's character offsets. Render by slicing the
  original string on those offsets — never by regex-matching the rule back onto
  the text, which is rule evaluation in TypeScript wearing a hat.
- Empty state is a worked example, not "no findings yet": pre-fill a headline that
  trips a real rule so the first thing a new user sees is the tool working.

---

## 7. Amendment inbox

Route `/projects/[id]/guidelines/amendments` · PRD §15.3 G

Rows: origin, detected date, source, **class chip**, a rendered diff, proposed rule changes.

| Class | Rendering | Controls |
|---|---|---|
| `mechanical` | already `auto_applied`, states the minor version it produced | none |
| `substantive` | `--status-gate` | **Apply** / **Dismiss with reason** for `guideline_publish` |
| `unclassified` | `--status-gate`, labelled `needs triage` | same |
| `signature_affecting` | **red** (`--status-failed`), full-width | same, plus the naming below |

A `signature_affecting` row **names every voided signature and every re-queued
claim** before the person acts — not a count, the names. This is the row that
takes a legal signature away from someone, and it should read like it.

Dismiss requires a reason; the reason field is the dialog's focus on open.

---

## 8. Sign-off matrix editor

PRD §21 S3-P8 scope · `GET`/`PUT /projects/{id}/signoff-matrix`

Assigns the three ownership slots (brand, legal, performance) to named people.

**The void-count warning is this screen's whole reason for existing.** Changing
the legal owner voids signatures. Therefore:

- The save control states the exact number of signatures the change will void
  **before confirmation**, from the server's count.
- Zero-void changes (brand, performance, or naming a legal owner where there was
  none) save without ceremony — a confirmation on a harmless action trains people
  to click through the dangerous one.
- The diff between current and proposed is shown as `Dana Okafor → Priya Raman`,
  not as two dropdowns the user must compare.

---

## 9. Accessibility — non-negotiable

1. Every interactive element reachable and operable by keyboard. The drawer and
   both dialogs trap focus, restore it on close, and close on `Escape` —
   **except** the step-up dialog, which closes on Escape but clears the password
   first.
2. Visible focus rings everywhere. Never `outline: none` without a replacement.
3. The virtualised table is a real `<table>` with `role="row"`/`aria-rowindex`, so
   a screen reader gets position among 500, not among the 20 in the DOM.
4. Status is never colour-only (see §1).
5. Live regions: the lint verdict and the decision counter are `aria-live="polite"`.
   The `Register has moved` interstitial is `aria-live="assertive"`.
6. Contrast ≥ 4.5:1 for body text in both themes — the tokens already satisfy this;
   do not introduce a fourth grey that does not.
7. Every icon-only control has an accessible name.

## 10. Responsive

Target ≥ 1024 px for the register and playground; both must remain *usable*, not
merely unbroken, to 390 px. Specifically: the claims table collapses to a card
list below 768 px (a horizontally scrolling 8-column table is not a mobile
design), and the signature drawer becomes a full-screen sheet. No page scrolls
horizontally at 390 px.

## 11. Loading, empty, error — every screen has all three

| State | Rule |
|---|---|
| Loading | Skeletons matching final layout. No spinner-on-blank-page for anything with a known shape. |
| Empty | Says what would put something here, and links to it. Never "No data". |
| Error | The problem's `detail` sentence + a retry. Never a stack trace, never "Something went wrong". |
| Forbidden | `<NoAccess>` naming the missing permission, as the existing component does. |

---

## 12. Acceptance — how this phase is judged

Straight from PRD §21's S3-P8 exit criteria. Each is a check, not an opinion.

| # | Criterion | Verified by |
|---|---|---|
| A1 | The named legal owner signs a 50-claim set end to end from the UI, including a per-claim rejection and an edited expiry, and gets a receipt | browser run |
| A2 | Nobody else sees the signature control **at all** | browser run as `operator`, `viewer`, `admin` |
| A3 | The playground returns findings with highlighted spans in **under 1.5 s** | measured, not asserted |
| A4 | The reassign dialog states the **exact** number of signatures it will void before confirmation | browser run |
| A5 | A `set_hash` that moved under the signer produces the interstitial and writes nothing | integration test + browser |
| A6 | Person-task controls are absent, not disabled, for a non-assignee | browser run |
| A7 | `/approvals` shows both tabs with one summed badge | browser run |
| A8 | No password or re-auth token appears in any React state, query cache, or log | code review + `/security-review` |

**Out of scope, deliberately:** the version diff view (`/compare`). Its backend
(`guidelines/diff.py`) is assigned to S3-P9 by PRD §21; building it here would
duplicate that phase's work. Recorded so its absence reads as a decision.
