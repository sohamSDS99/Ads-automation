# Security review — S3-P8

**Branch:** `feat/s3p8-frontend-b` vs `origin/main` · **Reviewed:** 2026-09-23
**Scope:** only what this phase introduces. Pre-existing issues are out of scope and are noted separately where they bear on a finding.

> The `/security-review` skill's harness produced an empty diff (most of this
> phase is untracked files) and asks for parallel sub-agents, which this session
> does not permit. The analysis below was done directly against the code.

## Verdict

**No HIGH or MEDIUM severity vulnerabilities introduced.** Two LOW
defense-in-depth issues were found and fixed during the review (§3). The
findings that would have mattered most — the step-up flow and the hash change —
were examined closely and are sound; §2 records why, because "we checked and it
holds" is the auditable part.

---

## 1. Attack surface added

Thirteen routes, four of which cross a privilege boundary:

| Route | Guard | Boundary |
|---|---|---|
| `POST /human-tasks/{id}/submit` | `ATTEST_SUBMIT` + assignee identity + step-up | non-delegable |
| `POST /human-tasks/{id}/attachments` | `ATTEST_SUBMIT` + assignee identity | file write |
| `POST /human-tasks/{id}/reassign` | `USER_MANAGE` | voids signatures |
| `PUT /projects/{id}/signoff-matrix` | `SETTINGS_WRITE` | voids signatures |

The remaining nine are `READ` or `GUIDELINE_PUBLISH` and mutate nothing outside
their own aggregate. `scripts/check_route_guards.py` passes: 104 guarded, 6
public, 110 total.

## 2. Examined and sound

**2.1 The step-up proof is never stored.** `reauth()` returns the token, the
caller passes it straight into the signing call, and it leaves scope. It is
never React state, never a query-cache entry, never a log line. Verified by
reading every reference: `claims.ts`, `step-up-dialog.tsx`, `task-card.tsx`.
The browser check asserts the DOM holds no password value after the ceremony.

**2.2 Identity is asserted before the token is spent.** `submit_human_task`
refuses a non-assignee with `403` *before* `tokens.consume()`, mirroring
`sign_claims`. Without that order, anybody holding `ATTEST_SUBMIT` could burn a
single-use proof belonging to the person the task is actually assigned to.

**2.3 `reauth_token_id` does not reach a browser.** `_redacted()` strips it from
`submitted_payload` on every render path. Asserted in
`test_submitting_with_a_step_up_completes_and_hides_the_token_id`.

**2.4 The register-hash change does not weaken the anti-race guarantee.**
This is the change most worth scrutinising, because it removes a field from a
security-relevant hash. `register_hash` covers `claim_id`, `normalized_text`,
`surface_forms`, `market_scope` and `languages` — every field a
`GUIDELINE_EXECUTE` holder can edit, which is the whole threat: `operator` and
`admin` can widen what a signature licenses and are precisely the roles denied
`CLAIM_SIGN`. All five remain in the material. What was removed is `decision`,
which is the signer's own input, travels in the same request as the hash, and
cannot be changed by a third party between read and submit. The stored
`ClaimSignature.set_hash` still includes decisions, so the signature record and
its idempotency remain decision-sensitive. Covered from both sides by
`test_a_set_with_a_rejection_in_it_can_actually_be_signed` and
`test_editing_the_register_under_the_signer_still_409s`.

**2.5 Attachment upload cannot escape its prefix.** `_safe_name()` takes the
last path segment, then reduces to `[alnum-_.]`, then strips leading/trailing
`-` and `.` — so `..` collapses to `""` and falls back to `attachment`. The key
is `human-tasks/{task_uuid}/{uuid4hex}-{name}`, both UUIDs server-generated.
No separator can survive the filter, so no traversal.

**2.6 Storage keys are not a download channel.** `attachment_paths` are returned
to any `READ` caller, which looked worth checking. There is no route in this API
that serves an arbitrary storage key: `download_export` takes an `export_id` row
id scoped through `ExportRepo(db, me.workspace_id)`, and `worker_files.signed_url`
is called only server-side with a signed token. Exposing the keys leaks nothing.

**2.7 The linter is side-effect free.** `POST /guidelines/{id}/lint` is `READ`,
makes no model call, and writes nothing — asserted by row-count in
`test_a_viewer_can_lint_and_the_route_writes_nothing`. Target count is bounded
at 200 by the schema.

## 3. Found and fixed

**3.1 `javascript:` URL reachable through an `href` — LOW**
`components/amendments/inbox.tsx` rendered `amendment.source_url` directly into
an `href`. That value is `PolicySource.url`, configuration written under
`SETTINGS_WRITE`. React escapes text but does **not** sanitize `href`, so a
stored `javascript:…` would execute on click for anyone reading the inbox.
Not currently reachable — no `/policy-sources` write route exists yet — but the
sink is live and the writer is planned for S3-P9.
*Fixed:* both link sites now go through `httpUrl()` in `lib/utils.ts`, which
returns a `URL` only for `http:`/`https:` and `null` otherwise.

**3.2 A malformed authority reference crashed the findings list — LOW**
`Authority` guarded with `/^https?:\/\//` and then called `new URL(reference)`.
A value passing the regex can still throw (`https://[`), and an uncaught throw
in a list item takes down the whole playground result pane.
*Fixed:* same `httpUrl()` helper, which catches and degrades to plain text.

## 4. Observations, not findings

**4.1 Admin → legal owner remains reachable, and pre-exists this phase.**
An `admin` holds `USER_MANAGE` and `APPROVAL_DECIDE`. They could promote an
account to `approver`, name it legal owner, and sign — defeating law 23. This is
not introduced here: before S3-P8 the same path existed through deciding G6,
which an admin already could. `assert_eligible` blocks the direct version (a
non-`approver` cannot be named), so the path costs a separate, audited
role change. Worth a decision in S3-P9 — a self-role-change guard, or requiring
two admins — rather than a fix in this phase.

**4.2 A person-task's `submitted_payload` is readable by every workspace member.**
Deliberate: a task blocks a publish or a launch, and one whose existence is
hidden from the people it blocks is one that stalls with nobody able to say why.
Contents are the assignee's own note and reference, not credentials.

## 5. Checks run

| Check | Result |
|---|---|
| `scripts/check_route_guards.py` | pass — 104 guarded, 6 public |
| `scripts/check_calc_isolation.py`, `check_guardrails_purity.py` | pass |
| `ruff check` / `ruff format --check` | clean |
| `mypy` | clean, 258 files |
| `tsc --noEmit` / `eslint` | clean |
| Secrets scan of the diff | no keys, tokens or credentials added |
