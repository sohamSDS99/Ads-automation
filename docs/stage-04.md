# Stage 04 — the Copy & Creative Agent

The runbook's record of checks a test cannot make. Read alongside
`PRD files/prd-copy-creative.md`; where the two disagree, the PRD is the
contract and this file is the mistake. Rulings and deviations live in
`docs/stage-04-questions.md`, phase by phase.

---

## Exports (§14) — acceptance

| # | Acceptance item | Status | Where it is proved |
|---|---|---|---|
| 1 | JSON validates against its schema; recomputing `package_hash` over the export equals the row | **passes** (S4-P16) | `tests/integration/test_s4p16_package.py::test_the_json_export_validates_and_recomputes_to_the_released_hash`, `test_s4p17_exports.py::test_xlsx_and_md_export_and_the_json_still_verifies` |
| 2 | Every Editor CSV column exists in `editor_columns.yaml`; every media path a CSV references exists in the ZIP; every text cell is within its `asset_specs` limit | **passes** (S4-P17) | `tests/creative/test_editor_zip.py`, `test_s4p17_exports.py::test_a_released_package_exports_an_editor_zip_keyed_by_the_pinned_plan` |
| 3 | The golden-fixture `EDITOR_ZIP` imports into the current Google Ads Editor with zero errors | **OPEN — not run** | Manual. See below. Closes Q4. |
| 4 | Two exports of one released package are byte-identical (sorted ZIP entries, fixed mtimes; PDF dates pinned to `released_at`) | **passes** (S4-P17) | clock-shift tests in `test_editor_zip.py`, `test_creative_book.py`, `test_asset_inventory_xlsx.py`; real downloads compared in `test_s4p17_exports.py` |
| 5 | PDF ≤ 25 MB for 10 ad groups × 2 RSAs, 3 concepts and 4 videos (posters only) | **passes** (S4-P17): 8.4 MB with every image random noise | `test_creative_book.py::test_the_pdf_of_the_section_14_load_is_at_most_25_mb` |
| 6 | A draft PDF is watermarked on page 1 and every page after | **passes** (S4-P17) | `test_creative_book.py::test_a_draft_pdf_is_watermarked_on_page_one_and_every_page_after`, `test_s4p17_exports.py::test_the_creative_book_is_watermarked_until_release_and_identical_after` |

`EDITOR_ZIP` of an unreleased package is `409 package_not_released`, before
anything is queued (`test_s4p17_exports.py::test_an_editor_zip_of_an_unreleased_package_is_409_and_queues_nothing`);
the worker refuses again (`editor_zip.PackageNotReleased`).

### Item 3 — the manual Google Ads Editor import (OPEN)

Nobody has imported an `EDITOR_ZIP` into Google Ads Editor. Until somebody
does, every header, the encoding and the image path in
`apps/api/src/agent/export/editor_columns.yaml` are `source: unverified`
(Q4), and the ZIP's own `README.md` says so. **Do not claim item 3.**

To close it:

1. Release the golden run's package (`scope.campaign_refs = ["c-sds-us"]`, as
   `test_s4p16_package.golden` builds it) and export `format=editor_zip`.
2. In the current Google Ads Editor, open a test account, then
   *Account → Import → From file* for each CSV in the ZIP, keeping `media/`
   beside them.
3. Record here: the Editor version, the date, who ran it, the error and
   warning count per file, and every header Editor renamed or refused.
4. Correct `editor_columns.yaml` from what Editor reported, set `source` to
   the Editor version and `reviewed_at` to the date, and re-run
   `tests/creative/test_editor_zip.py`.

| Editor version | Date | Run by | Result |
|---|---|---|---|
| — | — | — | not yet run |

---

## Thresholds (§17)

Every CC in PRD §17 has named tests that fail if it regresses. The
authoritative map is `apps/api/tests/test_nfr_stage_04.py`. It reads the CC ids
out of §17 itself and checks that every holder exists. Where a threshold
measures as **not met**, its holder is a strict xfail carrying the measured
number. It fails the day the threshold starts passing, and it is never read as
a pass. Figures are from S4-P24's runs.

| CC | Status | Measured / proved by |
|---|---|---|
| CC1 wall clock | **NOT MET: copy track** | Zero-latency machine time scaled to the CC1 shape: 27.8–55.7 s against a 270 s budget; copy track 20–40 s against 72 s. But the executor runs waves, so the copy track waits for 4.4.4 (7/7 runs; 21.3–33.8 s with 8 s of video latency). A video may take up to 900 s, over the 12-minute copy budget. The live threshold is not measurable offline. `test_s4p24_cost_and_time.py` |
| CC2 cost | text **met**; caps **NOT MET** | Text at default routing: $0.20–$0.26 per golden run, $1.14–$2.03 scaled to 10 ad groups (≤ $6). The caps can be passed by actual − estimate: $40.1125 vs $40 and $50.1125 vs $50; $85.00 vs $40 at the recorded wan billing with 400 jobs. It never compounds. `test_s4p24_cc2_media_caps.py`, `test_media_budget.py` |
| CC3 estimate accuracy | **NOT MET for wan-3.0**; Settings view missing | wan ratio 1.125 (0% within 0.25); flux 0.0218, veo 0, grok 0. "Tracked in Settings" does not exist. `test_s4p24_estimate_accuracy.py` |
| CC4 lint coverage | met | executor assertion, check 2, and the 4.6.4 re-lint (`test_s4p4_brief_g7.py`, `test_package_checklist.py`, `test_s4p15_final_lint.py`) |
| CC5 submit once | met | Real `kill -9` at all five states, plus `submitting_committed` and `completed`: at most one video POST; an image is re-POSTed only without a completed row. `test_s4p24_kill9.py` |
| CC6 geometry | met | 1,000-input property tests for sx == sy, ratio and bytes; DB CHECK on sx = sy (none on bytes). `test_image_geometry.py`, `test_image_bytes_property.py` |
| CC7 video | met | Real files: brand after 5 s, yuvj420p and moov-last each fail verification and never become renditions. `test_video_assemble.py`, `test_video_verify_gates.py` |
| CC8 offer integrity | met | every figure is a binding; a mutated offer makes release 409 (`test_s4p8_extras.py`, `test_s4p16_package.py`) |
| CC9 determinism | met | All 3 golden fixtures in 3 processes: A = B byte-identical `package_hash`; C (other row ids) identical once relabelled. Purity check. `test_s4p24_determinism.py` |
| CC10 security | met | 104-cell role × route matrix, ownership 403s, stale set_hash 409, reused token 401, media before G7 refused. `test_s4p24_authz_matrix.py`, `test_s4p14_exceptions.py`, `test_s4p4_brief_g7.py` |
| CC11 data hygiene | met | 4 canaries planted and proven present; none in any provider request, row outside `evidence`, read route or rendered log line; no base64 in a job request. `test_s4p24_canaries.py` |
| CC12 resume | met (worker); api crash needs a manual unstick | No finished node re-runs, no decided G8 item is re-asked, polling resumes. An api killed between a decision and its enqueue needs Cancel + Retry via the API (§18 operational row). `test_s4p24_kill9.py` |
| CC13 immutability | met | DB triggers on brief, asset, package and AssetDecision (`test_stage04_schema.py`) |
| CC14 frontend | met | Console LCP worst 864 ms; lint preview p95 298–330 ms; 500 tiles, longest task 8.6 ms; axe with no serious/critical issue in 48 cells (12 screens × 2 themes × 2 widths). `make browser-stage-04` |
| CC15 coverage | met | creative pure 99.04%, media 95.05%, nodes/creative 93.53%, preview 93.01%, export 94.44%. `make coverage-creative` |
| CC16 golden fixtures | met | 3 fixtures through all 24 nodes and G7/G8/H3 against cassettes; 13/13 blocking checks. `test_s4p24_golden.py` |

### Running the gates

| What | Command |
|---|---|
| Golden fixtures (replay) | the `test` service: `pytest tests/integration/test_s4p24_golden.py` |
| Re-record the cassettes | `GOLDEN_RECORD=1 pytest tests/integration/test_s4p24_golden.py`, then replay twice (`tests/cassettes/README.md`) |
| Determinism, authz, canaries, kill -9, caps | `pytest tests/integration/test_s4p24_{determinism,authz_matrix,canaries,kill9,cc2_media_caps,cost_and_time}.py` |
| §17 index | `uv run pytest tests/test_nfr_stage_04.py` (host; reads the PRD) |
| Coverage | `make coverage-creative` |
| Browser matrix | `make browser-stage-04` (one build; the 8 harnesses run one at a time on `s4p24-NN` stacks) |
| Guards | `make guards` |

---

## Failure modes (§18) — what happens and how to recover

Every row of PRD §18, checked against the code at S4-P24. **Status** says whether
the code does what §18 says: **HANDLED**, **PARTIAL** (the difference is stated),
or **NOT IMPLEMENTED**. Where the product has no control for a step, the
"recover" cell says so. It never names a control that does not exist. Code paths
are relative to `apps/api/src/agent/`, tests to `apps/api/tests/`, and `web/` is
`apps/web/`. Every PARTIAL and NOT IMPLEMENTED row is a ruling owed in
`docs/stage-04-questions.md` § S4-P24.

**Controls used below**

- **New run**: `POST /projects/{id}/creative/runs` (CREATIVE_EXECUTE: admin,
  operator). UI: the Start dialog.
- **Regenerate**: `POST /creative-assets/{id}/regenerate` (CREATIVE_EXECUTE),
  which takes `model_override`/`params_override`. UI: the generation panel in
  the Media Library drawer.
  - Only while G8 is pending and the asset is on the G8 card
    (`api/routes_creative_runs.py:1120`); otherwise `409 not_on_card`.
  - An asset with no file (a gap) is never on the card.
- **Retry**: `POST /runs/{id}/retry-failed` (RUN_EXECUTE: admin, operator).
  UI: "Re-run N failed" (`web/components/run/run-controls.tsx`).
  - It resets failed and skipped nodes only; finished nodes are not re-run.
  - A media job that already failed comes back unchanged under the same
    idempotency key.
- **Cancel**: `POST /runs/{id}/cancel` (RUN_EXECUTE).
- **Check again**: `POST /generation-jobs/{id}/check` (CREATIVE_EXECUTE). UI:
  the Jobs tab.

| Failure (§18) | What the system does | How to recover | Proof | Status |
|---|---|---|---|---|
| Media model removed mid-run | Seen only when a call fails: 404/"no model" → job `failed`, code `model_unavailable` (`media/http.py:34`, `media/jobs.py:377`). Never swaps to another model. | Regenerate with an allowlisted `model_override`. An asset with no file: new run. | `integration/test_media_jobs.py::test_model_unavailable_never_produces_a_request_to_another_model`; `integration/test_s4p13_asset_review.py::test_regenerate_before_g8_checks_model_and_budget_then_takes_the_old_ones_place` | PARTIAL: the code is `model_unavailable`, not `media_model_unavailable`; a gap asset cannot be regenerated |
| Provider rejects a listed parameter (400, catalogue drift) | Job `failed` with `{code: provider_rejected, status, body}`, body verbatim (`media/jobs.py:377`, `:410`). Params are never stripped. | Regenerate with `params_override` (checked against the pinned record) or `model_override`. The provider's text is in `GET /creative-runs/{id}/generation-jobs`. | `media/test_images.py::test_a_4xx_raises_provider_rejected_with_the_body_verbatim` | PARTIAL: no catalogue refresh and no capability re-check (the cache expires after 600 s); the Jobs list reads `error.detail`, which is never written |
| Image generation 502 | Retried 3× (1.5/3/6 s backoff) inside one job, one key (`media/images.py:37`). Then `failed provider_unavailable`, reservation released. | Regenerate. | `media/test_images.py::test_a_502_is_retried_three_times_and_then_given_up_unbilled`; `integration/test_media_jobs.py::test_image_502_502_200_is_one_ledger_entry_and_one_stored_image` | HANDLED |
| Video job failed / cancelled / expired | Terminal, error verbatim; reservation settled to `usage.cost` or released (`media/jobs.py:487`); 4.4.4 records a `generation_failed` gap. Never re-POSTed. | Regenerate if on the G8 card; otherwise new run. | `integration/test_media_jobs.py::test_a_terminal_video_failure_keeps_the_error_verbatim_and_releases_the_budget`; `integration/test_s4p11_video_production.py::test_a_failed_clip_is_a_gap_and_is_never_re_posted` | HANDLED |
| Video exceeds `video_job_timeout_s` | `timed_out`; the OpenRouter job is left alive (`media/jobs.py:459`); 4.4.4 fails naming each job. | Check again (re-polls with a fresh window), then Retry. | `integration/test_s4p11_video_production.py::test_a_timed_out_clip_completes_and_downloads_on_check_again`; `integration/test_s4p18_creative_reads.py::test_check_again_refuses_what_it_cannot_act_on_and_queues_what_it_can` | HANDLED |
| Worker killed while polling | Resume re-polls a row that has `openrouter_job_id`; never re-POSTs (`media/jobs.py:301`). | Automatic once the reaper fails the run (heartbeat over 5 min old), then Retry. | `integration/test_s4p24_kill9.py::test_cc12_a_worker_killed_while_polling_resumes_the_saved_job_and_never_re_posts` | HANDLED |
| Crash after `submitting` committed, before the job id was saved | Row → `unknown_submit_state` (`media/jobs.py:302`); so does a video submit that got a 5xx/network error, keeping its reservation. 4.4.4 records a gap. | Images: Check again re-POSTs under the same key. Video: nothing. `/check` answers `409` and the reservation stays held. Start a new run. | `integration/test_s4p24_kill9.py::test_cc5_a_video_killed_at_each_submit_state_is_posted_at_most_once`, `::test_cc5_an_image_is_re_posted_only_when_no_completed_row_exists` | PARTIAL: **no "Submit again (may double-bill ≈ $x)" route or UI exists** |
| Reservation would breach a cap | `blocked_by_budget` before any HTTP (`media/jobs.py:338`). Images: a gap in 4.4.2/4.4.3. Only 4.4.4 records `degraded` and stops later submits. The Jobs tab shows "Blocked by budget". | Raise the caps (`PATCH /projects/{id}/settings/media` or `PUT /settings/media`, admin; read on every submit), then Regenerate or new run. | `integration/test_media_jobs.py::test_a_reservation_that_breaches_a_cap_blocks_the_job_before_any_http`; `integration/test_s4p11_video_production.py::test_a_budget_that_runs_out_mid_video_is_a_gap_and_stops_the_submits`; `integration/test_media_budget.py` (50 concurrent reservations) | PARTIAL: no degrade ladder for images; nothing renders `degraded`; no Console header step |
| Every image candidate fails lint | One retry with strengthened negatives, then gap `all_candidates_failed_lint` (`nodes/creative/n4_4_2_image_masters.py:215`). | A gap is not on the G8 card, so Regenerate is `409`. New run. | `integration/test_s4p9_image_masters.py::test_every_candidate_failing_twice_is_one_retry_then_a_gap` | PARTIAL: the gap does not name the rule; QA does not show it |
| OCR or logo matcher unavailable | `detector_unavailable` → verdict `indeterminate` (`guardrails/matchers/image.py:70`), never a pass; the candidate is dropped. | Restore `tesseract` on the worker, then new run. | `integration/test_s3p5_image_lint.py::test_with_ocr_unavailable_the_verdict_is_indeterminate_never_pass` | HANDLED (stricter than §18) |
| No supported ratio and crop keeps < 0.85 saliency | A ratio no frame covers is a gap; a crop keeping < 0.85 is a gap stating the % (`nodes/creative/n4_4_3_image_renditions.py:873`). Start shows the ratio-coverage table. | Regenerate with a `model_override` that covers the ratio, or change the default model and start a new run. | `integration/test_s4p10_image_renditions.py::test_pmax_logo_is_composited_fitted_and_a_thin_crop_is_a_gap`; `media/test_capability.py::test_an_unsupported_ratio_no_supported_one_covers_is_a_gap` | HANDLED |
| No references, or not allowed | `reference_guided` only for a reference that passes `refusal()`; else `composited_real` or `none` (`media/references.py:193`). | Register a reference (`POST /projects/{id}/media-references`) and allow references (admin), then new run. | `integration/test_s4p9_concepts.py::test_an_uncleared_third_party_reference_resolves_composited_real` | PARTIAL: nothing actually composites the product photo |
| Landing page unreachable / 4xx / 5xx / bot-blocked | Non-2xx → `unreachable` (`preview/landing.py:163`); bad sitelink URLs rejected; copy still produced; `blocking_for: launch` on the campaign's package dependency. | The site owner fixes the page; new run. | `integration/test_s4p7_landing.py::test_an_unreachable_page_is_recorded_and_asks_no_model` | HANDLED |
| Offer data stale | 4.3.2 `not_required` with reason past 7 days (`nodes/creative/n4_3_2_offer_assets.py:247`). | None in the product: no route or UI writes offer data. | `integration/test_s4p8_extras.py::test_stale_offers_are_not_required_and_ask_no_model` | PARTIAL: CR-E13 warns at 30 days, 4.3.2 drops at 7, so 8–30 days is silent |
| No licensed claim applies to an ad group | 4.2.2 raises `NodeContractError`; the node fails after 3 attempts and its branch goes down. | Sign a claim in Stage 03, then new run. | `creative/test_claim_bound_descriptions.py::test_a_pin_that_licenses_no_claim_fails_the_node` | **NOT IMPLEMENTED**: no per-ad-group `blocked: no_licensed_claim`, no path to H3 |
| Legal owner never acts on H3 | Run parks `awaiting_human_task`. | Withdraw (`web/components/creative/h3-exceptions.tsx` → `POST /creative-runs/{id}/exceptions/withdraw`): assets fall back and the run resumes. | `integration/test_s4p14_exceptions.py::test_withdrawing_every_exception_ends_h3_not_required_and_licenses_nothing` | PARTIAL: no reminder cadence covers H3 (reminders read Approval rows only) |
| Newer ruleset published during a run | No repin: the pin appends only on H3 clearance (`creative/lint_adapter.py:43`). | None needed. | `creative/test_lint_adapter.py::test_the_current_pin_is_the_last_one_appended` | PARTIAL: nothing sets `ruleset_superseded`, so the banner never shows |
| Newer plan frozen before release | Release `409 plan_superseded` (`creative/release.py:196`). | New run (the 409 says so). | `integration/test_s4p16_package.py::test_a_superseded_plan_makes_release_409` | PARTIAL: `reuse_cache` exists but 4.1.1 is never cached, so media is regenerated and billed; no banner |
| A pinned claim expires between assembly and release | Release re-lints at `now`: `409 claim_unlicensed` naming each asset (`creative/release.py:236`). | New run. A reserve swap alone ends in `409 package_stale` because nothing re-runs 4.6/4.7. | `integration/test_s4p16_package.py::test_an_expired_claim_makes_release_409_naming_the_asset` | PARTIAL |
| Two approvers release at once | Advisory lock + `UPDATE … WHERE status = ready_to_release`; the loser gets `409` (`creative/release.py:151`). | None needed. | `integration/test_s4p16_package.py::test_concurrent_releases_yield_exactly_one_200_and_one_409` | HANDLED |
| Volume fills during post-production | Before each video ratio: free space < 2× footprint → gap `storage_insufficient`, no ffmpeg. A disk that fills mid-encode → `assembly_failed`. | Free space (`GET /storage` shows usage), then Regenerate if on the card, else new run. Retry will not re-run a node that succeeded. | `integration/test_s4p12_video_postprod.py::test_too_little_free_space_is_a_gap_before_ffmpeg_runs` | PARTIAL: no `storage_full` code; CR-E11 not checked at Start; the gap's "retry this node" hint is wrong |
| Playwright crashes on a page | Fresh context per URL and device; an error marks only that URL `unreachable` (`preview/landing.py:456`). | Retry. | `preview/test_landing.py::test_a_page_that_never_answers_is_unreached_not_an_exception` | PARTIAL: no retry; a dead browser fails all of 4.5.1 |
| G8 rejects every asset | Rejected assets dropped; check 11 blocks when a campaign type in scope needs media, so the package is `blocked`. | New run. | `creative/test_package_checklist.py::test_check_11_every_campaign_meets_its_minimums_or_names_what_blocks_launch` | PARTIAL: §18 says the text package stays releasable; it does not when media is required |
| `ffmpeg` exits non-zero | One retry with conservative args, stderr tail kept; two failures → `assembly_failed` gap with both tails (`postprod/video.py:976`). | Regenerate if on the card; otherwise new run. | `integration/test_s4p12_video_postprod.py::test_ffmpeg_failing_once_is_retried_conservatively_and_the_tail_is_kept`, `::test_ffmpeg_failing_twice_is_a_gap_with_both_stderr_tails` | HANDLED |
| SERP template stale vs Google | Truncation is a warning at most; only the spec diff blocks (`nodes/creative/n4_6_4_final_lint_and_render.py:583`). | None; a template update is a code change. | `preview/test_serp.py::test_a_template_other_than_the_pinned_version_is_refused` | PARTIAL: `serp_template_version` is not shown on any web preview |
| OpenRouter key missing or revoked mid-run | Key read once at run start; media 401 → `failed provider_rejected` (no `auth` code); a 401 while polling is not caught; text 401 fails the node without retry. | Reconnect (`POST /connections/openrouter/connect`, admin), test (`POST /connections/openrouter/test`), then Retry; failed media jobs need Regenerate or a new run. | `integration/test_creative_entry.py::test_e7_no_credential_blocks` (missing key only) | PARTIAL: no `auth` code; CR-E7 does not test the key; no test for a 401 |
| *(operational)* Worker killed mid-DAG | The reaper (every minute and at worker boot) fails RUNNING runs whose heartbeat is over 5 min old, fails their in-flight nodes and releases the run's own stage lock (fixed in S4-P24). | Retry. Finished nodes do not re-run and make no model request. | `integration/test_s4p24_kill9.py::test_cc12_a_worker_killed_mid_dag_re_executes_no_node_it_finished`, `::test_cc12_a_worker_killed_mid_regeneration_re_asks_no_decided_g8_item` | Manual Retry |
| *(operational)* api killed between a gate decision's commit and its enqueue | Stuck `awaiting_approval` with no pending gate, or `queued` with `started_at` set; **the reaper sweeps neither**. | **API only:** Cancel, then Retry (Cancel marks the rest skipped, and Retry resets them). The UI's Retry button counts failed nodes only, so it stays hidden. | `integration/test_s4p24_kill9.py::test_cc12_an_api_killed_after_a_g8_decision_re_executes_nothing_and_re_asks_nothing` | Manual (API only); ruling owed |

### Crash recovery in one paragraph

A worker `kill -9` loses nothing that was committed. Resume re-polls saved video
jobs and never re-POSTs one. It settles a paid job's reservation that the kill
left open. It re-asks no decided G8 item. It re-runs no finished node
(`tests/integration/test_s4p24_kill9.py`, 17 real-SIGKILL cases). What still
needs a person is the unstick:
- **Worker died:** Retry once the reaper has failed the run.
- **api died between a decision and its enqueue:** Cancel + Retry via the API.
