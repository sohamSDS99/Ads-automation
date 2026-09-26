# Phase gate — Stage 04, the Copy & Creative Agent

| | |
|---|---|
| Phase | Stage 04 (`PRD files/prd-copy-creative.md`), phases S4-P0 … S4-P24 |
| Evaluated | branch `feat/s4-p24` at `45cf40e4`, over `main` at `c4cab8e7` (S4-P0 … S4-P23 all merged; confirmed with `git log origin/main`) |
| Date | 2026-09-27 |
| Runtime | Python 3.12.13 · PostgreSQL 16.15 · Redis 7.4.11 · ffmpeg 4.4.2 · tesseract 4.1.1 · exiftool 12.40 (worker test image `s4p24-wtest`) · Node 24.19.0 · pnpm 9.1.0 · Next 15.5 |
| Stack | `docker compose -p s4p24` (postgres + redis, 172.31.124.0/24, no host ports). The suites ran inside the worker test image. Browser harnesses ran on `s4p24-NN` stacks. |

Legend: ✅ measured pass · 🟡 built and green, needs a live run · ⬜ organizational, no code gap · ❌ under bar

---

## Verdict: **NO GO**

S4-P24's own exit criteria are **met**:
- every §17 threshold has a named test that fails if it regresses;
- CC5, CC9, CC10 and CC11 are green;
- all five coverage floors are met.

Stage 04 as a whole is not. Three §17 thresholds were **measured under the
bar**:
- **CC2: the hard cost caps can be exceeded.** $40.1125 against the $40 media
  cap. At the recorded wan billing ratio the worst case is $85.00, with 400 jobs
  in flight.
- **CC1: the copy track cannot finish independently of video and of the G8
  decision.** The executor runs waves.
- **CC3: wan-3.0's estimate is off by 112.5%.** The Settings view that should
  track estimate accuracy does not exist.

Also:
- one §18 failure mode is not implemented: no per-ad-group
  `blocked: no_licensed_claim`, so 4.2.2 fails the whole node;
- 18 more §18 rows are only partly implemented;
- an api crash between a gate decision and its enqueue leaves a run stuck
  until someone uses the API.

Each of these is written down with its measured number, pinned by a strict
xfail and a ratchet so it can neither be forgotten nor quietly worsen, and
waiting on a ruling (`docs/stage-04-questions.md` § S4-P24). None is a pass,
so the verdict cannot be GO. The failures are measured, not unmeasurable, so it
cannot be CONDITIONAL GO either.

What *is* now proven:
- Three golden fixtures run through all 24 nodes against cassettes and pass
  all 13 blocking checks.
- The same input gives a byte-identical `package_hash` in separate processes.
- No canary leaks anywhere.
- No role can reach a route it should not.
- A real `kill -9` at any submit state never double-bills a video.

This phase found and fixed **8 determinism defects, 4 crash-recovery defects
and 3 frontend defects** in shipped Stage 04 code (§5).

---

## 3. Exit criteria

### S4-P24 (PRD §21.3, verbatim)

> Every §17 threshold has a test that fails if regressed; CC5, CC9, CC10, CC11 green; coverage gates met

| Criterion | Verdict | Evidence |
|---|---|---|
| Every §17 threshold has a test that fails if regressed | ✅ | `apps/api/tests/test_nfr_stage_04.py`: CC ids parsed from §17; 60+ holders, each checked to exist; every NOT MET threshold is a strict xfail carrying its number **plus** a ratchet (88 pass on host; 80 + 8 skipped in the image) |
| CC5 green | ✅ | `test_s4p24_kill9.py::test_cc5_*`: real SIGKILL at queued, submitting, after_post, submitted, in_progress (+ submitting_committed, completed): ≤ 1 video POST; image re-POST only without a completed row. 17/17, three runs |
| CC9 green | ✅ | `test_s4p24_determinism.py::test_one_creative_input_and_its_cassette_give_one_package_hash_in_two_processes` × 3 fixtures: A = B byte-identical, C (other row ids) identical once relabelled. 5/5 |
| CC10 green | ✅ | `test_s4p24_authz_matrix.py` 104 cells + ownership/coverage tests; `test_authz_matrix.py` (red on main, green here); `test_s4p14_exceptions.py` ×4; `test_s4p4_brief_g7.py` ×2. 418 passed, 165 skipped |
| CC11 green | ✅ | `test_s4p24_canaries.py`: 4 canaries planted and proven present, then absent from requests, rows (incl. Stage 04's own evidence), read routes and rendered logs. Mutation-checked 3 ways |
| Coverage gates met | ✅ | creative pure 99.04 · media 95.05 · nodes/creative 93.53 · preview 93.01 · export 94.44 (floors 85/85/80/80/80); `make coverage-creative` (§6) |

### §17 non-functional requirements

| CC | Verdict | Evidence / measured |
|---|---|---|
| CC1 45 min / copy track 12 min | ❌ copy track · 🟡 live | Machine time with zero provider latency, scaled to 10 ad groups / 3 concepts / video: 27.8–55.7 s against a 270 s budget; copy track 20–40 s against 72 s. **But 4.3.3 runs after 4.4.4 (video) and 4.4.5 (G8) every time (7/7)**; a video may take 900 s. The real-provider wall clock is not measurable offline. xfail `test_cc1_copy_track_does_not_wait_for_video_latency`; ratchet `test_cc1_copy_track_structure.py` |
| CC2 cost | ✅ text · ❌ caps | Text $0.20 / $0.24 / $0.26 per golden run, $2.03 max scaled (≤ $6). Caps: $40.1125 / $50.1125 / worst $85.00. xfails + ratchets in `test_s4p24_cc2_media_caps.py` |
| CC3 estimate accuracy | ❌ wan · ❌ Settings view | wan 1.125 (0 of 1 billed runs within 0.25); flux 0.0218, veo 0, grok 0. "Tracked in Settings" not built |
| CC4 lint coverage | ✅ | executor assertion, check 2, 4.6.4 re-lint |
| CC5 submit once | ✅ | above |
| CC6 geometry | ✅ | 1,000-input properties for sx == sy, ratio and **bytes**; DB CHECK on sx = sy |
| CC7 video | ✅ | real files: brand at 5.5 s, yuvj420p and moov-last each fail verification and never become renditions |
| CC8 offer integrity | ✅ | bindings; mutated offer ⇒ release 409 |
| CC9 determinism | ✅ | above |
| CC10 security | ✅ | above |
| CC11 data hygiene | ✅ | above |
| CC12 resume | ✅ worker · ❌ api-crash unstick | No finished node re-runs; no decided G8 item re-asked; polling resumes. An api killed between decision commit and enqueue: stuck until Cancel + Retry via the API |
| CC13 immutability | ✅ | four DB-trigger tests |
| CC14 frontend | ✅ | LCP worst 864 ms (≤ 2500); lint p95 298–330 ms (≤ 400); longest task 8.6 ms (≤ 16); axe with no serious/critical issue in 48 cells; keyboard-only G8 with a pointer guard |
| CC15 coverage | ✅ | above |
| CC16 golden fixtures | ✅ | 3 fixtures × all 24 nodes × G7/G8/H3, 13/13 blocking checks, replayed from cassettes (twice each; also in 3 processes each for CC9) |

---

## 4. Per-milestone acceptance (S4-P0 … S4-P23)

Every clause of every phase's §21.3 exit criteria (§23/§23.1 for S4-P0/P1),
mapped to the test that measures it. Test ids are verified to exist; `s4pNN:`
is a check label in `apps/web/scripts/s4pNN/browser-check.mjs`.

| Phase | Clause | Test | Verdict |
|---|---|---|---|
| P0 | upgrade → downgrade -1 on a populated Stage 03 DB | none | ❌ finding 1 |
| P0 | enum downgrade raises its message | none (`0019_stage04_enums.py:158` untested) | ❌ finding 1 |
| P0 | gated by two frozen artifacts | `test_creative_entry.py::test_gated_by_two_frozen_artifacts` | ✅ |
| P0 | "both" fixture eligible, 202 | `test_creative_entry.py::test_both_starts_a_run_that_writes_the_brief_and_halts_on_g7` | ✅ |
| P0 | dummy DAG reaches terminal **over SSE** | superseded by S4-P4; no Stage 04 SSE test | ❌ finding 2 |
| P0 | four trigger tests | `test_stage04_schema.py` (8 tests, forbidden + permitted each) | ✅ |
| P0 | uniform-scale CHECK · duplicate idempotency key · plan Run without source | `test_stage04_schema.py::test_a_non_uniform_scale_is_rejected`, `::test_a_duplicate_idempotency_key_is_rejected`, `::test_a_plan_run_without_a_source_is_still_rejected` | ✅ |
| P0 | approver/viewer 403 on start; all 4 roles 200 on eligibility | `test_creative_entry.py::test_e15_every_role_reads_eligibility_and_only_executors_may_start` | ✅ |
| P0 | creative-context 404 when nothing published | `test_creative_entry.py::test_creative_context_is_404_when_nothing_is_published` | ✅ |
| P0/P1 | route guards pass | `tests/test_route_guards.py::test_every_route_declares_a_permission` | ✅ |
| P0/P1 | `mypy src/agent` exits 0 | run by `make typecheck` only (no CI) | ❌ finding 3 (measured 0 at this gate) |
| P1 | coverage on media/ ≥ 85% | `make coverage-creative` exit code (95.05%) | ❌ finding 4 (enforced only by running make) |
| P1 | submit once at all 5 points | `test_media_jobs.py::test_video_submit_once_under_crash`; real kill: `test_s4p24_kill9.py::test_cc5_a_video_killed_at_each_submit_state_is_posted_at_most_once` | ✅ |
| P1 | unsupported aspect_ratio ⇒ 422, no request · default video off allowlist ⇒ blocked · estimate writes no job · admin PUTs allowlist, operator 403 | `test_media_routes.py` (4 named tests) | ✅ |
| P2 | 04 locks with the blocker in a sentence + link | `s4p2: "carries the lock sentence"`, `"names the blocker in a sentence"` (stub API) | ✅ |
| P2 | admin allowlists one image + one video model | `s4p2: "admin allowlists one image and one video model"`; `test_media_routes.py::test_an_admin_can_put_an_allowlist_and_an_operator_gets_403` | ✅ |
| P2 | `pnpm lint` fails on planted gradient / raw hex / Sparkles | rule tests `eslint-rules/design-laws.test.mjs`, `scripts/check_design_laws.test.mjs` | ✅ rules · ❌ finding 5 (wiring untested) |
| P2 | axe clean both themes | `s4p2: "axe clean"` × 2 themes × 2 widths | ✅ |
| P3 | start choosing both models · only supported params · over-cap disables + one-click reduction · 422 renders field + values · keyboard-only | `s4p3:` "the start request carries both chosen models", "shows NO quality control", "over the cap Start is disabled", "renders the field and the value refused", "keyboard: Enter starts the run → 202" | ✅ |
| P4 | halts on G7, ≤ 600 words, every line sourced · operator 403 · media before G7 refused · edit re-hashes · unsourced constant fails startup · `complete_structured(IMAGE_GEN)` raises | `test_s4p4_brief_g7.py` (4), `test_brief.py`, `test_creative_constants.py` (3), `test_task_classes.py::test_complete_structured_refuses_image_gen` | ✅ |
| P5 | 15 headlines meeting quotas · near-duplicate never selected · offer conflict swapped · DKI on default · byte-identical in 2 processes · purity check | `test_select.py`, `test_combinatorics.py`, `test_headline_spread.py`, `test_creative_purity.py`, `test_s4p5_headlines_combinations.py` | ✅ |
| P6 | ≥ 1 licensed claim per description · unlicensed span ⇒ exception candidate · paraphrasing B fails · 4.2.5 not_required on search-only | `test_s4p6_descriptions_variant_b.py` (2), `test_claim_bound_descriptions.py`, `test_variant_b.py`, `test_asset_group_text.py` | ✅ |
| P7 | mismatched H1 · offer below fold per device · 9-field form minimised · GET only | `test_s4p7_landing.py` (3), `tests/preview/test_landing.py::test_the_renderer_issues_nothing_but_get` | ✅ |
| P8 | sitelinks on-domain 2xx unique · offer figures bound · stale ⇒ not_required · privacy URL, no Art. 9, tradeoff evidence | `test_s4p8_extras.py` (5), `test_offer_assets.py`, `test_urlcheck.py` | ✅ |
| P9 | worker boots on Railway with ffmpeg/exiftool | checked once live (branch deploy `744e354f`) | 🟡 |
| P9 | product_depiction 6 cases · third-party ref needs image_right · failing candidate discarded before ranking | `test_references.py`, `test_s4p9_image_masters.py` (2), `test_s4p9_references.py` | ✅ |
| P10 | every ratio native/relaid/crop/gap · 1,000-input sx == sy · no logo on search_image · XMP reads back · 60% crop ⇒ gap | `test_s4p10_image_renditions.py` (2), `test_image_geometry.py`, `test_image_logo.py`, `test_image_encode_stamp.py`, `test_calc_crop_window.py` | ✅ (image) |
| P11 | voiceover covered by captions · supported durations only · kill during polling · timed_out completes on Check again | `test_video_script.py`, `test_calc_shot_plan.py`, `test_s4p11_video_production.py` (2), real kill `test_s4p24_kill9.py::test_cc12_a_worker_killed_while_polling_…` | ✅ |
| P12 | 16:9 + 9:16 verified masters · silent track has AAC · Range 206 | `test_s4p12_video_postprod.py`, `test_video_assemble.py`, `test_video_verify_gates.py`, `test_fileserver_range.py::test_curl_r_0_1023_over_a_real_socket_is_a_206` | ✅ (image) |
| P13 | approve needs 4 ticks · non-allowlisted regen 422 · only regenerate items regenerated · G8b no regen · rejected G8b dropped · draft survives reload | `test_s4p13_asset_review.py` (2), `test_review.py` (3), `s4p22: "a reload restores all 20 decisions"` | ✅ |
| P14 | admin clearing 403 · stale set_hash 409 · cleared claim mints MINOR + pin · rejected claim swaps to fallbacks · withdraw ⇒ not_required | `test_s4p14_exceptions.py` (5) | ✅ |
| P15 | re-lint at final pin · top-3 + longest previews both devices · overflow per element · 31-char overflows and fails | `test_s4p15_final_lint.py` (3), `test_serp.py` (2), `test_preview_combinations.py` | ✅ (31-char test flaky on main too, finding 6) |
| P16 | golden package passes 13 checks · transactional release · mutated offer / expired claim 409 · concurrent release 200 + 409 · released 404 then exact pin | `test_s4p16_package.py` (6) | ✅ |
| P17 | §14 items 1, 2, 4, 5, 6 · unreleased Editor ZIP 409 | `test_s4p16_package.py`, `test_s4p17_exports.py` (3), `test_editor_zip.py` (5), `test_creative_book.py` (4) | ✅ (§14 item 3, the Editor import, 🟡 manual) |
| P18 | owner approves and sees authorised spend · Jobs estimate vs actual + Check again · baselines | `s4p18:` "sees the authorised spend in numbers", "estimate beside actual", "Check again is offered on the timed-out job", visual baselines | ✅ |
| P18 | "everyone else sees no decide control" | `s4p18: "Approve / Reject / Edit are absent"` for viewer/operator/other approver, but admin **has** them (ruled keep, 2026-09-25) | ⬜ finding 7 (waiver recorded; PRD text to amend) |
| P19 | 31 chars ⇒ rose + fail ≤ 400 ms · heatmap → preview · keyboard reserve swap · 200-string parity | `s4p19:` (4 labels), `char-count.test.mjs`, `test_char_count_fixture.py` | ✅ |
| P20 | offers read-only with window · chosen tradeoff point · fold overlay both devices · patch → clipboard | `s4p20:` (5 labels) | ✅ |
| P21 | 500 tiles ≤ 16 ms/frame · aspect-true, no master · regen cost before submit · 0–5 s band, captions, muted · Range seeking | `s4p21:` (8 labels) | ✅ |
| P22 | keyboard-only review of 20 · Approve after 4 ticks · legal owner clears 3 with receipt · control absent for others · withdraw counts | `s4p22:` (8 labels, incl. the new pointer guard + self-test) | ✅ |
| P23 | release v1 by typing · canonical read-only URL · text + side-by-side media diff | `s4p23:` (9 labels) | ✅ |

**Findings (no test or a test that cannot fail):**
1. P0: the migration round trip (`upgrade head` → `downgrade -1` on a
   populated Stage 03 DB) and 0019's downgrade message are untested.
2. P0: creative-run SSE delivery is untested. The S4-P4 test deliberately
   reads the database instead.
3. `mypy src/agent` has no test. Only `make typecheck` runs it, and there is
   no CI. It measured 0 at this gate.
4. The coverage floors are enforced only by running `make coverage-creative`.
   The CC15 unit tests pin the target's text, not the figure.
5. P2: the lint rules are tested, but their wiring into `pnpm lint` is not.
   Dropping them from `eslint.config.mjs` would pass every test.
6. P15: `test_a_31_character_…` is flaky (4.6.4 preview tie-break; failed 1/5
   on main, 2/5 here).
7. P18: the admin override on G7 contradicts "everyone else sees no decide
   control". Soham ruled "keep" on 2026-09-25. The PRD text should be amended.

---

## 5. Defects found and fixed

Each fix landed with a regression test that failed before it: observed red in
this phase's runs, or proven by a mutant.

| # | Defect | Production impact | Why the suite was blind | Fix | Regression test |
|---|---|---|---|---|---|
| 1 | 4.3.1 ordered crawled pages `(fetched_at, id)`; rows crawled in one transaction tie, so UUIDs decided | the same input sent a different sitelinks prompt each run; no recording could replay it | no test ran one input twice | tie-break on content hash | `test_s4p24_determinism.py::test_4_3_1_…content_order_not_uuid_order` (red before) |
| 2 | 4.3.3 read CRM/page evidence in the same UUID order | lead-form prompt varied by run | same | same | `::test_4_3_3_…` (red before) |
| 3 | `Snapshot.included()` sorted shipped assets by `str(id)` | 4.7.2's critique prompt and the package's asset order varied by run | the S4-P16 cross-process test used fixed ids | `shipping_order()`: node, campaign, ad group, surface, text, concept, hash, id | `tests/creative/test_package_order.py` (red before) |
| 4–8 | asset-group media, artifact, job, landing-audit and exception lists ordered by UUID | package content order depended on row ids | same | content keys | CC9 process C (red before each) |
| 9 | `AssetDecision.decided_at` from Postgres `now()` while its gate used the app clock | one decision, two clocks | never compared | from `approvals.utcnow()` | CC9 A = B |
| 10 | a kill between a job's `completed` commit and its Redis reconcile left the estimate reserved | phantom spend eating run headroom and counted against both caps | simulated crashes raised inside the process, so the reconcile still ran | `MediaJobs._settle` on resume | `test_s4p24_kill9.py::test_cc5_*[completed]` (fails with a no-op settle) |
| 11 | 4.4.2's `_stored` ran `int()` on `{job}-{i}_preview.webp` | a crashed 4.4.2/4.4.6 could **never** resume (every retry `ValueError`) | no resume after a master was chosen | read only `{job}-{digits}.{ext}` | `::test_cc12_a_worker_killed_mid_regeneration_…` |
| 12 | the reaper released the research lock key for a creative run | the project stayed locked 3 h after a dead creative run | the reaper was only tested on research runs | `RunLock(redis, run.stage)` | `::test_cc12_a_worker_killed_mid_dag_…` |
| 13 | the never-started sweep matched only `run.launched` | a creative/plan/guideline run whose api died before enqueue was never reaped | same | every launch action | `::test_cc12_an_api_killed_right_after_it_started_a_run_…` |
| 14 | the console threw on any run halted at H3 (`awaiting_human_task` had no status entry) | the Creative Console crashed for every run at the legal stop; s4p20 red **on main** | no harness drove a run to H3 in the console | status entries + gate border | `make browser-stage-04` s4p20 (red before) |
| 15 | the selected row's `fg-subtle` on `accent-soft` measured 4.44:1 | WCAG AA contrast failure on every selected row | axe cells were missing | `fg-muted` (7.1:1) | s4p18 axe cells (red before) |
| 16 | canonical-package links told apart by colour only | axe `link-in-text-block` (serious) | the dark-1280 cell did not exist | underline | s4p23 axe cell (red before) |

The hostile review of this phase's own tests found four more, all fixed in
`b798aaae` and `45cf40e4`:
- the canary scan skipped Stage 04's own evidence rows (a mutant leaking the
  CRM email into 4.3.3's calc evidence now fails);
- NOT MET thresholds had no ratchet;
- CC9 child processes had no timeout;
- the §17 index failed collection inside the image.

---

## 6. Gates as measured

Every result below is judged by its exit code.

| Gate | Result |
|---|---|
| ruff (api `src` + `tests`) | exit 0 |
| mypy `src/agent` | exit 0, 372 files |
| `make guards` (route guards, calc isolation, guardrails purity, creative purity) | exit 0 |
| `scripts/check_route_guards.py` | exit 0 |
| web `pnpm tsc --noEmit` / `pnpm lint` (eslint + stylelint + design laws) / `pnpm test:unit` | 0 / 0 / 0 (50/50) |
| API full suite (unit + integration) in the worker image, `45cf40e4`'s predecessor `bb18b6ce` | **5317 passed, 42 failed, 170 skipped, 5 xfailed, 3 collection errors** (34 min). `main`: 5110 passed, 41 failed, 142 skipped, 2 errors |
| failure diff vs `main` (by test id) | branch-only: `test_s4p15_final_lint::test_a_31_character…` (flaky: 2/5 here, 1/5 on main) and `test_guideline_exports::…moved_clock[pdf]` (flaky: 4/5 here, 5/5 on main). main-only: `test_authz_matrix::test_the_matrix_covers_every_guarded_route_in_the_application` (fixed). The 3rd error was the §17 index in the image, fixed in `45cf40e4` (image: 80 passed, 8 skipped). The other 40 failures are identical on both: Stage 03 `s3p6_publish` ×21, `s3p2_brand_rules` ×12 and others, all pre-existing |
| Files changed after that run (`b798aaae`, `45cf40e4`), re-run | canaries + determinism 6/6; CC2 caps 5 passed + 3 xfailed; index 88 (host) / 80 + 8 skipped (image); CC1 structure 2/2 |
| API unit suite on the host | 3910 passed, 6 failed, 1 error. main: 3819 passed, **the same 6 failed and 1 error** (no exiftool on the host; `test_package_files_job.py` needs app secrets) |
| `make coverage-creative` (run as written at this gate, `COMPOSE_PROJECT_NAME=s4p24`) | **exit 0**, *all five floors met*: creative pure 99% (415 stmts) · media 95% (1,394) · nodes/creative 94% (4,064) · preview 93% (415) · export 94% (3,831). Its own unit + integration pass: 5321 passed, 42 failed (the same ids), 170 skipped, 5 xfailed (41 min) |
| `make browser-stage-04` (run at this gate, one build, 8 harnesses on `s4p24-NN` stacks) | **exit 0**: s4p2 167/167 · s4p3 100/100 · s4p18 137/137 · s4p19 60/60 · s4p20 97/97 · s4p21 85/85 · s4p22 137/137 · s4p23 132/132 = **915 checks**. LCP worst 604 ms; lint p95 289 ms (p50 280); longest main-thread task 6.0 ms over 8,072; keyboard-only G8 0 pointer events. The builder's two earlier runs on the same build: exit 0 |
| Secret scan (diff `c4cab8e7..HEAD`) | one key-shaped string, the deliberately fake canary `sk-or-v1-zzqx-canary-gateway-…`; `.env` gitignored. The repo has no scanner script |
| Migrations | none this phase |
| Dependency changes | none this phase |

---

## 7. Gaps this gate records rather than closes

These are stated limits of this pass, not a clean bill of health.

1. **CC2 caps (❌).** Reserve-at-estimate plus reconcile-at-actual lets any
   job in flight at the cap overshoot by actual − estimate. Ruling owed
   (questions S4-P24 item 2, S4-P1 item 2).
2. **CC1 copy track (❌).** Wave scheduling couples the copy track to video
   and to G8. The live 45- and 12-minute figures cannot be measured offline:
   about 130 sequential calls on the copy track at 10 ad groups means ~5 s per
   call breaks even.
3. **CC3 (❌).** wan-3.0's catalogue price underestimates by 2.125× (one
   billed run on record). No Settings view.
4. **§18.** 1 NOT IMPLEMENTED row (`no_licensed_claim`), 18 PARTIAL
   (`docs/stage-04.md` § Failure modes, with file:line each), including:
   - no "Submit again" for an `unknown_submit_state` video;
   - no catalogue refresh after a 400;
   - no image degrade ladder;
   - no H3 reminders;
   - dead superseded banners;
   - `reuse_cache` that reuses nothing past 4.1.1;
   - no `auth` failure code.
5. **The api-crash unstick is manual and API-only**, and the Cancel dialog's
   "cannot be resumed" is wrong.
6. **No CI.** Every ✅ holds only when someone runs `make test`,
   `make coverage-creative` and `make browser-stage-04`. `make
   test-integration` uses the dev image, which has no ffmpeg, exiftool or
   tesseract, so Stage 04's media integration tests (S4-P10…P13 and this
   phase's golden, CC9 and canary tests) pass only in a worker-based test
   image. This was already true before this phase.
7. **Cassettes are recorded from scripted sources**, not from live
   OpenRouter. They pin *our* side of the conversation exactly, but not a real
   model's answers.
8. **CC9's reading of "identical input" pins row ids and decision times as
   inputs**, through the test harness. Content-derived ids would make that
   unnecessary (ruling item 1).
9. **Live-only:** the worker boot on the current main deploy (P9) and the
   Google Ads Editor import (§14 item 3).

---

## 8. Path to GO

1. **Rule on and fix CC2.** For example, reserve estimate × the model's
   observed billing ratio, or the capability's worst price, so reconcile can
   never cross a cap. Then flip `test_s4p24_cc2_media_caps.py`'s three xfails
   to passing and move the ratchets to zero overshoot.
2. **Rule on CC1.** Either dependency-driven scheduling in `RunExecutor`, so
   4.3.3 runs when its own inputs are done, or a restated copy-track
   threshold. Then flip `test_cc1_copy_track_does_not_wait_for_video_latency`.
3. **Rule on CC3.** Correct wan's price mapping (S4-P1 item 2) and decide
   whether the Settings accuracy view is built (its own phase). Then flip the
   wan xfail.
4. **Implement §18's `blocked: no_licensed_claim` per ad group**, and rule on
   each PARTIAL row in `docs/stage-04.md`.
5. **Add a guarded sweep for the api-crash stuck run** (or ruling item 5), and
   correct the Cancel dialog copy.
6. **Close the milestone findings.** Test the migration round trip and the
   creative-run SSE, test the lint wiring, make the 4.6.4 tie-break
   deterministic, and amend P18's PRD text.
7. **Run the live checks.** Worker boot on the current main deploy, the
   Google Ads Editor import, and one real-provider creative run timed against
   CC1.
8. **Put `make test`, `make coverage-creative` and `make browser-stage-04`
   into CI** on a worker-based test image, so none of the above depends on
   someone remembering to run it.
