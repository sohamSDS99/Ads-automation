# AI design contract — S3-P6, synthesis, publish, versioning and exports

Scope: the model-facing surface of Stage 03 phase S3-P6 only. What each node may
ask a model for, what it must never accept back, and how that is measured.

This phase is where Stage 03 stops drafting and starts *asserting*. Everything
before it produced sections; S3-P6 assembles them into one artifact, decides
whether that artifact may be sealed, seals it, and hands Stage 04 a compiled
program. So **law 22 — the LLM drafts, it never adjudicates — is under more
pressure here than anywhere else in the stage**, because the thing being decided
is now "may this be published" rather than "what should this section say".

The answer is the same as Stage 02's: the decision is Python, the prose is the
model, and the two never swap jobs. `publish.py` contains no model call at all.

---

## 1. Which nodes call a model, and which deliberately do not

| Node | Task class | Model call | Why |
|---|---|---|---|
| 3.5.2 `legal_review_triggers` | `CLASSIFY` | **yes** | Deciding that "clinically proven" should route to legal is a judgement over a rubric. The *trigger* is drafted; the matcher it compiles to is checked by `guardrails/` |
| 3.6.1 `guideline_synthesis` | `SYNTHESIZE` | **partial** | The model writes the executive summary and nothing else. Every one of the fourteen sections is a projection of an upstream node's stored output, assembled in `guidelines/synthesis.py` |
| 3.6.2 `guideline_critique` | `CRITIQUE` | **partial** | The ten §11 assertions are computed in `guidelines/critique.py`. The model is asked only for what a predicate cannot see |
| `publish.py` | — | **no** | A transaction. The single most consequential write in the stage, and a model has no part in it |
| the six exporters | — | **no** | Rendering. A model in an exporter would make two exports of one published version differ, which §14 acceptance 2 forbids outright |

`publish.py` and the exporters are listed precisely because they are empty. This
phase adds the first code in Stage 03 whose output is *legally* load-bearing — a
signed claims register circulating as a PDF — and the place a model must never
appear is the path between a signature and the document that reports it.

---

## 2. Per-node contract

### 3.5.2 `legal_review_triggers` — what should never ship unread

§21 assigns this node to no phase. §11 requires it, §12.1's `Governance`
section is built from it, and 3.6.1 depends on `{all}`. It is built here.

**The model drafts** `triggers[]{id, pattern_kind, pattern, why, reviewer_role,
severity}` and `always_review[]`, from 3.2.2's substantiated claims and 3.3.1's
applicable policy areas.

**Refused on the way back in:**

- a `pattern_kind` outside `{term, claim_type, campaign_type, market, asset_type}`;
- a `pattern` that does not compile to a matcher — the same rule 3.1.2's lexicon
  lives under, and for the same reason: a trigger nobody can evaluate is a note,
  not a rule;
- a `reviewer_role` naming an identity rather than a role. Routing to a *person*
  is `SignOffMatrix`'s job and it is non-delegable; a trigger that named one
  would be a second, weaker path to the same decision;
- `severity` outside the `Rule` severity set.

**Degradation.** No substantiated claims and no applicable policy areas ⇒ an
empty `triggers[]` with `always_review=[]` and a stated reason. Not an error:
a project with nothing risky to say has nothing to route.

### 3.6.1 `guideline_synthesis` — assembly, with one paragraph of prose

The model is asked for **`executive_summary` (≤ 250 words) and nothing else**.

Everything else in `ContentGuideline` is a projection of node outputs already on
disk, assembled by `guidelines/synthesis.py` in the same shape
`planning/plan_synthesis.py` assembles a `CampaignPlan`. This is not a stylistic
preference. §12.1 invariant 2 requires every `RuleDraft.authority` to resolve and
every non-`internal` rule to carry ≥1 `evidence_id`; a model that *wrote* the
rules would be sourcing facts, which is law 1.

**Refused on the way back in:**

- a summary over 250 words (§12.1);
- any `evidence_id` not in the gathered set — the executor's existing subset
  check, unchanged;
- any prose `Claim` without ≥1 resolvable `evidence_id` (§12.1 invariant 4,
  Stage 01 law 1, unchanged);
- a `status` field. **3.6.1 may not write status.** It runs before the critique,
  so `ready_to_publish` is not a state it is entitled to name. The rule is
  `guidelines.synthesis.status_for` and it is a function, not a judgement —
  the exact precedent `planning.plan_synthesis.status_for` sets.

**The `ContentGuideline` row is written here.** Nothing in the repository writes
one today; S3-P0 created the table and the trigger, and left the writer to this
phase. The row lands `draft` or `blocked`, never `published`.

### 3.6.2 `guideline_critique` — ten computed assertions, one reading

§11: "the critique's checklist is **fixed and asserted in tests, not left to the
model's discretion**." All ten therefore live in `guidelines/critique.py`, as
predicates over the finished `ContentGuideline`:

| # | Assertion | Decidable because |
|---|---|---|
| 1 | every rule's `authority` resolves; non-`internal` carries evidence, `internal` carries a `constants_key` | set membership |
| 2 | every matcher compiles and the set round-trips to an identical hash twice | `compiler.compile` is already deterministic (S3-P1) |
| 3 | every `approved` claim has an unexpired signature whose `set_hash` matches | a hash comparison |
| 4 | no `unsupported`/`rejected`/`expired` claim is licensable, and each produces a blocking rule naming it | set difference |
| 5 | no term is both `always` and `never` in one `(market, language, surface)` scope | `lexicon.conflicts[]` is already computed by 3.1.2 |
| 6 | every in-scope asset type has a spec with a bound and a launch-minimum entry | set difference |
| 7 | every `applicable` policy area produces ≥1 rule or an explicit `no_rule_needed` | set difference |
| 8 | every open `HumanTask` appears in `open_dependencies[]` with a non-null assignee | a join |
| 9 | disclosure rules cover every surface generated content is permitted on | set difference |
| 10 | no brand-book binary, customer record, CRM field, email or personal name anywhere in the payload | the existing canary + redaction pass (law 30) |

A model asked "does every approved claim have a valid signature" will usually
say yes, will occasionally say yes when it does not, and will never say *which
one*. Assertion 10 in particular is the one place a model's answer would be
actively dangerous: a false "no PII here" is how PII ships.

**What the model is asked for** — and only this: prose that oversells a degraded
rulebook, an executive summary that describes rules the register does not
contain, a voice profile that contradicts the lexicon, a rule whose `message`
tells a writer to do something a different rule forbids. Contradictions between
sections that no single predicate spans.

**One blocking issue buys exactly one re-synthesis, and it runs inside 3.6.2.**
The executor is a wavefront over a DAG with no facility for a node to send
another node round again; building one for a single documented case would be a
large change to the most load-bearing file in the repo. `stage_1_6` and
`stage_2_6` both made this call and the cost lands on 3.6.2's `NodeRun`.

**3.6.2 decides `status`.** Same reason 2.6.2 does.

---

## 3. What has no model surface at all

### `publish.py`

Reads like `planning/freeze.py` and must: assert G5 and G6 `approved`; assert H1
`completed` with an unexpired signature covering **every** claim in the register;
assert the critique returned no blocking issue; compile the `RuleSet` and assert
the compile reproduces; mint `version_major = max+1, version_minor = 0`; write
the immutable `RuleSet` row; supersede the prior published version; write an
`AuditLog` row. One transaction.

The refusal path returns `409` **listing what is outstanding** — §21's exit
criterion. A publish that fails with "not ready" and no list is a screen a person
cannot act on.

### The six exporters

`MD` (Jinja2) is the source of truth; `PDF` and `DOCX` render from it; `JSON` is
the payload; `RULESET_JSON` is the compiled artifact with its hash; `XLSX` is
the spec sheet and claims register. §14 acceptance 2 — two exports of a published
version are byte-identical — is only achievable because none of them calls a
model and none of them reads a clock they did not receive as an argument.

---

## 4. Eval strategy

### 4.1 Adversarial fixtures — each must be refused, not merely scored

| Fixture | The model returns | Expected |
|---|---|---|
| `summary_over_length` | a 400-word executive summary | `NodeContractError` |
| `synthesis_invents_status` | `status="ready_to_publish"` | field ignored; `status_for` decides |
| `synthesis_uncited_claim` | a prose `Claim` with `evidence_ids=[]` | `NodeContractError` |
| `synthesis_foreign_evidence` | an `evidence_id` from another run | `NodeContractError` (existing subset check) |
| `critique_denies_real_defect` | `verdict="pass"` on a payload with an expired signature | assertion 3 fires anyway; verdict overridden |
| `critique_invents_defect` | a blocking issue citing a section that does not exist | dropped; a re-synthesis is not bought by an unlocatable finding |
| `triggers_name_a_person` | `reviewer_role="<uuid>"` | rejected at validation |
| `triggers_uncompilable_pattern` | `pattern="([unclosed"` | rejected at compile |

The two critique fixtures are the important pair. They test the same property
from both sides: **the model's verdict never overrides a computed assertion, in
either direction.** A model that says "pass" cannot unblock a real defect, and a
model that says "fail" cannot manufacture one.

### 4.2 Invariants asserted independent of model output

1. A full run with every model call stubbed to minimum-valid output still emits a
   `ContentGuideline` that passes all five §12.1 invariants.
2. Publish is refused on every one of: gate undecided, gate rejected, H1
   incomplete, H1 signature expired, H1 signature covering a stale `set_hash`,
   blocking critique issue. Six refusals, each naming itself.
3. Publishing twice at the same `confirm_version` is idempotent; at a different
   one it is `409`.
4. The `RuleSet` row is byte-identical when compiled twice from the same payload,
   in two processes with different `PYTHONHASHSEED`.
5. `GET /guidelines/published/ruleset` returns `404` on a project with only a
   draft — never an empty ruleset. §16 rule 4: Stage 04 must handle the absence
   rather than fall back to "no rules", and an empty ruleset *is* "no rules".
6. `GET /rulesets/{version}` resolves a superseded pin.
7. A draft export is watermarked on page 1 **and on every subsequent page**.
8. Two exports of one published version are byte-identical, for all six formats.

### 4.3 The publish loop (the phase's one end-to-end path)

A full DAG run on a seeded project → 3.6.1 emits a `ContentGuideline` → 3.6.2
computes ten assertions and returns `ready_to_publish` → `POST publish` mints
`v1.0` and a `RuleSet` → `GET published/ruleset` returns it → a second run on the
same project publishes `v2.0` and marks `v1.0` `superseded` → `GET
/rulesets/{v1.0 pin}` still resolves, byte-identical to what it was.

That last step is the whole reason the ruleset is pinned rather than resolved,
and it is asserted, not assumed.

---

## 5. Model routing

Unchanged from PRD §9.8 — `llm/router.py`, per-task-class, runtime-hydrated model
ids. 3.6.1 is `SYNTHESIZE`, 3.6.2 is `CRITIQUE` and therefore cross-family by
construction, 3.5.2 is `CLASSIFY` at `temperature=0, top_p=1`. This phase adds no
routing behaviour and hardcodes no model id.

---

## 6. Provenance of this document

The `/gsd-ai-integration-phase` skill would normally produce this as `AI-SPEC.md`
inside a GSD phase directory, via its framework-selector → ai-researcher →
domain-researcher → eval-planner chain. That skill ships only `SKILL.md`; the
three files its execution context `@`-includes (`~/.claude/gsd-core/workflows/
ai-integration-phase.md` and two references) are not installed, and this
repository is not GSD-structured — no `.planning/`, no `ROADMAP.md`. The contract
it would have specified is written here instead, against PRD §11, §12, §14 and
laws 1/22/23/30. Same provenance as `ai-spec-s3p3.md`.
