# AI design contract — S3-P3, the claims register and the non-delegable signature

Scope: the model-facing surface of Stage 03 phase S3-P3 only. What each node may
ask a model for, what it must never accept back, and how that is measured.

The governing rule this whole document exists to serve is **law 22**: the LLM
drafts, it never adjudicates. Every enforcement verdict in this phase comes from
`guardrails/`, deterministically, citing a `rule_id`. Nothing below changes that
— it constrains what the drafting half is allowed to produce.

---

## 1. Which nodes call a model, and which deliberately do not

| Node | Task class | Model call | Why |
|---|---|---|---|
| 3.2.1 `claim_harvest` | `EXTRACT` | **yes** | Finding claim-shaped language across site copy and ad history is extraction over text nobody has indexed |
| 3.2.2 `claim_substantiation` | `CLASSIFY` | **yes** | Typing a claim and tiering its risk is judgement over evidence that already exists |
| 3.2.3 `legal_claim_signoff` | — | **no** | It creates a `HumanTask`. A person decides. A model call here would be the exact failure law 23 forbids |
| 3.2.4 `offer_integrity_rules` | `EXTRACT` | **partial** | The model drafts the human-readable `requirement` prose. Every verdict and every `live_violations[]` row is computed against `OfferRecord` by `matchers/offers.py` |

3.2.3 is listed precisely because it is empty. A node sitting in an agent DAG
invites a model call by default, and the one place that must never happen is the
node whose entire purpose is that a named human decided.

---

## 2. Per-node contract

### 3.2.1 `claim_harvest` — harvest what we already say

**Asked for:** candidate claims observed in supplied evidence.

**Given:** `site_pages`, `creative_history`, `brand_book_span` evidence, and —
only when bound — `differentiation_claim` (1.3.4) and `competitor_creative`
(1.3.2).

**Refused on return:**

1. **A candidate with no `observed_on` entry.** Every candidate must name a real
   URL or ad id drawn from the supplied evidence. A claim the model produced
   without seeing it is invention, and this node harvests rather than proposes.
2. **An `observed_on` reference not present in the gathered evidence.** Checked
   by set membership, not by trusting the string.
3. **A `claim_type` outside the eight enum values.** One schema-repair pass,
   then node failure.
4. **Model-supplied `normalized_text`.** The model returns `claim_text`; the
   normalized form is computed by `guardrails/normalize.py`. A normalization the
   model performed would not match the one the linter performs, and the licence
   pass would silently stop licensing.

**Prompt hygiene (law 30):** email addresses, phone numbers and postal addresses
are stripped from creative history by a deterministic pass *before* composition.
The brand-book binary never enters a prompt; only spans do.

### 3.2.2 `claim_substantiation` — bind each claim to its evidence, or don't

**Asked for:** which evidence substantiates each harvested claim, the claim's
risk tier, and the basis for its expiry.

**Refused on return:**

1. **`status: approved`.** The model may return only `unsupported` or
   `pending_signoff`. `approved` is reachable through exactly one path — a named
   human's step-up-authenticated signature. This is law 24 enforced at the node
   boundary rather than only at the linter.
2. **An `evidence_id` that does not resolve** to an `Evidence` row in this
   project. A fabricated id is the highest-value failure to catch here: it makes
   an unsupported claim look substantiated, which is the one lie this stage
   exists to prevent.
3. **`pending_signoff` on a claim with zero resolving evidence ids.** A claim
   with no evidence is `unsupported` and stays `unsupported`. The model cannot
   talk it up.
4. **A model-chosen `proposed_expires_at`.** The model selects the *basis*
   (qualitative vs quantified); the date is computed in code from
   `claims.default_expiry_days` / `claims.quantified_expiry_days`. Stage 02's
   law 2 — the LLM never does arithmetic — applies to dates.

### 3.2.4 `offer_integrity_rules` — the model writes prose, not verdicts

The model drafts `requirement` text and names the `construction`. It is never
shown a price and never asked whether an offer is compliant.
`live_violations[]` is produced by evaluating `matchers/offers.py` against
`OfferRecord` rows. A from-price mismatch is arithmetic over live data, and
arithmetic does not go to a model.

---

## 3. Eval strategy

Determinism first: every assertion below runs against a stubbed LLM returning a
fixed payload, so what is measured is the node's *rejection* behaviour, not the
model's mood.

### 3.1 Adversarial fixtures — each must raise `NodeContractError`

| Fixture | The lie it tells |
|---|---|
| `harvest_invents_claim` | A candidate whose `observed_on` names a URL absent from the gathered evidence |
| `harvest_unobserved` | A candidate with an empty `observed_on` |
| `substantiation_fabricates_evidence` | An `evidence_id` that is a well-formed UUID resolving to nothing |
| `substantiation_promotes_unsupported` | `pending_signoff` on a claim whose evidence list is empty |
| `substantiation_self_approves` | `status: approved` straight from the model |

These are the eval. A node that passes its happy path and fails these is a node
that will manufacture a licence the first time a model has a bad day.

### 3.2 Invariants asserted independent of model output

1. `normalized_text` for a given `claim_text` is byte-identical to
   `guardrails.normalize` applied directly — the node does not have its own copy.
2. `proposed_expires_at` is a pure function of (basis, constants version, run
   start), asserted without a model in the loop.
3. The prompt canary: a marker string planted in a fixture brand book and a
   fixture email address in creative history appear in **zero** composed
   prompts and zero node payloads (law 30, PRD CP2).

### 3.3 The licence loop (PRD CP1)

The end-to-end assertion that makes this phase worth building:

- a claim `approved` with an unexpired signature **licenses** its surface forms
  in `linter.lint()` — the finding disappears;
- the same claim with an expired signature is **blocking** again;
- the same claim never signed is **blocking**;
- a signature voided by legal-owner reassignment makes it **blocking** from that
  moment.

Unlicensed, unsigned, rejected and expired all behave identically. That equality
is the test, not four separate near-duplicates.

---

## 4. Model routing

Unchanged from PRD §9.8 — `llm/router.py`, per-task-class, runtime-hydrated
model ids, `temperature=0` and `top_p=1` on `EXTRACT` and `CLASSIFY`. This phase
adds no routing behaviour and hardcodes no model id.

---

## 5. Provenance of this document

The `/gsd-ai-integration-phase` skill would normally produce this as
`AI-SPEC.md` inside a GSD phase directory. That skill's execution context
(`~/.claude/gsd-core/`) is not installed and this repository is not
GSD-structured — no `.planning/`, no `ROADMAP.md`. The contract it would have
specified is written here instead, against PRD §11, §17 and laws 22/23/24/30.
