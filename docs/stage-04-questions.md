# Stage 04 — open questions and carried-forward gaps

Things S4-P0 found that the PRD does not settle, or that belong to a later
phase. Nothing here was built.

## S4-P0

1. **`policy_amendment_origin` is really `amendment_origin`.** PRD §7.5 / §23
   name the type after the column. Migration 0019 alters the type that exists
   (`amendment_origin`, created by 0016). Suggest correcting the PRD text.
2. **`apps/web/lib/permissions.ts` does not list `creative_execute` /
   `creative_release` yet.** S4-P0 is backend-only; the TS mirror should gain
   both in S4-P2 when the 04 tab first reads them.
3. **"The project's default scope" (CR-E8/E9/E10) is not defined.** S4-P0 reads
   it as `Project.settings.media_models` (PRD §7.1): a default model for
   `image` / `video` means that modality is on by default. Confirm, or name the
   setting S4-P1 should read.
4. **CR-E10's ZDR condition has no settings key.** Nothing in the codebase
   records zero data retention. S4-P0 reads `Workspace.settings.zdr_enforced`
   (bool, absent = false), spelled once as `routes_creative.ZDR_SETTING`.
   Confirm the key, and who can set it.
5. **CR-E11's footprint is unknown until S4-P1.** A text-only scope writes no
   media (footprint 0, check passes without touching the disk). A media scope's
   footprint comes from the shot plan S4-P1 prices; until then CR-E8 already
   blocks every media scope, so `media_footprint_bytes` returns `None`
   ("not estimable"), never 0. S4-P1 must return a number here **and** add a
   free-space probe to `StorageBackend`: the api service does not mount the
   worker's `/data` volume, and `tests/test_filesystem_boundary.py` forbids
   reading the filesystem outside `storage/`. `routes_creative.storage_blocker`
   is the (tested) rule it will apply to the two numbers.
6. **`offer_max_age_days` (CR-E13) is not in config or constants.** S4-P0 uses
   Stage 03's `content_constants.offers.staleness_warning_days` (30), so the
   two stages agree on when offer data is stale. Move it to
   `creative_constants.yaml` if Stage 04 should differ.
7. **`CreativeInput.lead_definition` and `.naming` are Optional.** §4.3 types
   them as required; the plan contract does not guarantee either
   (`Objectives.qualified_lead`, `AccountStructure.naming_convention` are
   `| None`). An empty default would assert a decision nobody made.
8. **CR-E15 on the read endpoint.** `GET .../eligibility` is READ (all four
   roles get 200); a caller without `creative_execute` sees a
   `missing_permission` blocker row. `POST .../runs` enforces the 403. Same
   posture as Stage 02's E8.
9. **Pre-existing: `test_the_matrix_covers_every_guarded_route_in_the_application`
   fails on main.** Twenty Stage 03 routes (`/guidelines/published/ruleset`,
   `/human-tasks*`, `/policy-amendments*`, `/signoff-matrix*`, …) were never
   added to `GUARDED_ROUTES`. Every Stage 04 route is listed. Not fixed here
   (scope); it wants its own small PR.
10. **`creative_constants_version` is a placeholder** (`"s4p0-unset"`) until
    `creative_constants.yaml` exists — the S3-P0 `content_constants_version`
    precedent.

## S4-P1

Rulings the media gateway needed that the PRD does not settle, and what the
live API said when the fixtures were recorded (2026-09-24,
`apps/api/tests/fixtures/openrouter/README.md`). Nothing here was built
beyond what is described.

1. **OpenRouter never reported `in_progress`.** Three live video jobs on
   three providers (wan-3.0, grok-imagine-video, veo-3.1-lite), polled every
   1–2 s, all went `pending` → `completed`. The poller handles `in_progress`
   (it is in the documented status enum), but its fixture — and the
   `failed` / `cancelled` / `expired` ones — are **derived** from the
   recorded `pending` body at the owner's direction, and labelled so. The
   only way to record a real terminal failure is to provoke one.
2. **The catalogue can under-price a video by 2×.** wan-3.0 billed
   **$0.2125** for a 2.02 s 854×480 clip whose SKU `duration_seconds_480p`
   0.05 prices at $0.10. veo-3.1-lite ($0.12) and grok-imagine-video
   ($0.05) billed exactly their SKU price. A reservation is the estimate, so
   an under-priced job can carry a run past `max_media_cost_usd` by its
   under-estimate before reconcile catches up — against CC2 ("never
   exceeded"). Options: reserve `estimate × observed per-model ratio` once
   §9.3's estimate-accuracy tracking exists, or a flat safety margin in
   constants. Needs a ruling; nothing was invented.
3. **Megapixel images are not billed width × height × price.** flux.2-klein-4b
   at 16:9 with no tier returned 1824×1024 (1.87 MP) and billed $0.015, not
   $0.026. The estimate prices a tier at its area (`1K` = 1024², `2K` =
   2048², …; no tier = `1K`), confidence `medium` — $0.0147 here. OpenRouter
   says only "concrete pixel dimensions are derived per-provider".
4. **Five of 29 video models cannot be estimated** (every `bytedance/seedance*`
   is priced in `video_tokens`; `flux-video-upscale` per megapixel-second).
   Law 43 needs a number to reserve, so choosing one is a CR-E9 blocker
   **`estimate_unavailable`** — a code the PRD does not list. A
   tokens-per-second constant would make them estimable; it would be a
   guess today.
5. **Blocker / problem codes beyond §4.2 and §16:** `estimate_unavailable`
   (above), `capability_unsupported` as a CR-E8 blocker (a stored project
   default the model no longer accepts), `media_model_out_of_scope` (a model
   for a modality the scope has off, or two for one), `catalogue_unavailable`
   (503 when no snapshot ≤ 24 h exists), `job_not_found` (a poll of an id
   OpenRouter does not know — recorded, a real 404).
6. **`budget.reserve` takes more than §23.1's `(run_id, modality, usd)`**:
   the `job_id` (so `reconcile(job_id)` / `release(job_id)` can find the
   reservation, and so a resumed worker's second reserve is a no-op), the
   caps, the run's text spend (`Run.cost_usd` minus committed media) and the
   committed media spend as a floor (so a Redis flush cannot reset the cap).
7. **The text half of the estimate is §17 CC2's "$6 at default routing"**,
   confidence `low`. There are no creative text nodes to measure yet;
   S4-P4+ should replace it with measured `NodeRun` costs.
8. **The video semaphore covers the submit POST only**, not the render: the
   render is waited on by polling, and holding a slot for minutes across
   polls would serialise runs on two slots. The image slot covers the whole
   (synchronous) generation. Slots are leases on the Redis clock, so a
   killed holder's slot expires. Confirm the intent of `media:video (2)`.
9. **An unpinned image choice is the intersection of its endpoints**, priced
   at the dearest endpoint's lines: OpenRouter may route (and fall back)
   between endpoints, so a request is validated against what every one of
   them accepts. A pinned choice is exactly its endpoint.
10. **A video choice cannot pin a provider.** `POST /api/v1/videos`'
    `provider` object takes only passthrough `options` — no `only` /
    `allow_fallbacks` — so an allowlist entry with a video `provider_tag`
    is refused.
11. **The estimate's `PlanCalc` rows are anchored to the frozen plan's run**
    (`plan_run_id` = the run a creative run is sourced from). There is no
    creative run yet when a user asks for an estimate, and
    `PlanCalc.plan_run_id` is NOT NULL.
12. **Workspace default params apply only where the chosen model takes
    them**; a project's own defaults are validated strictly (422
    `capability_unsupported`). A workspace default is written for every model
    at once, so refusing a model for lacking one would refuse most models.
13. **`check()` re-POSTs an image in `unknown_submit_state` only when it has
    no references.** `GenerationJob.request` holds references by sha256
    (Law 44); their bytes are re-read by `media/references.py` (S4-P9).
    `POST /generation-jobs/{id}/check` (§16) is not in S4-P1's route list;
    `MediaJobs.check()` is what it will call.
14. **CR-E11 is still unmeasured for a media scope.** The footprint needs the
    shot plan (S4-P11) and free space needs a probe of the worker's volume,
    which the api does not mount. With CR-E8 no longer blocking every media
    scope, a media run can now start without the storage check.
15. **httpx logs every request line at INFO**, and `configure_logging` sets
    INFO: for `GET /videos/{id}/content?index=0` that line *is* the unsigned
    URL. `media/videos.py` filters exactly that line (a test proves the leak
    without it). Other outbound calls that carry a secret in a query string
    would leak the same way; worth one repo-wide look.
16. **The S4-P1 media constants live in `media/constants.py`** with §9.5's
    values and sources, stamped with `Settings.creative_constants_version`,
    until S4-P4 creates `creative_constants.yaml`.

## S4-P2

1. **The allowlist editor cannot offer §9.2's provider pin.** S4-P1 landed
   while S4-P2 was open and the web now codes against its `schemas_media`.
   `GET /media/catalogue?modality=image` returns OpenRouter's
   `/images/models` summaries — one record per model, **no `provider_tag` and
   no pricing** (both live on `/images/models/{id}/endpoints`, which no route
   exposes). Video cannot pin at all (S4-P1 §10). So the editor shows image
   prices as "Priced per provider", offers no pin, and shows and keeps a pin
   an entry already has. Proposed: `GET /media/catalogue/{model_id}/endpoints`
   (SETTINGS_WRITE) returning the endpoint records, for a per-row picker.
2. **Workspace default params (`media_defaults`) have storage and a route
   now (S4-P1) but no editor.** It needs S4-P3's `CapabilityParams` — one
   control per descriptor — and building a second set here would fork it.
   `PUT /settings/media` sends only `media_allowlist`, so defaults set by API
   survive every allowlist save. Estimate accuracy (§15.3) still has no route.
3. **Three §15.4 A landing fields are not on the wire.**
   `CreativePackageSummary` has no released-by name, no per-campaign launch
   readiness (`ready` / `blocked: youtube_upload`) and no bound-offer end
   date (so neither the landing's "offers ending soon" row nor the rail's
   third amber reason can be drawn). Cost is joined from the package's run.
   Proposed: `released_by_name`, `launch_readiness[]` and
   `earliest_offer_end` on the summary, added with S4-P16's package.
4. **A newest creative run that `failed` reads `Blocked` on the rail.**
   §15.1 rule 2's vocabulary has no word for it; `Blocked` is the closest
   true one. Ruling wanted.
5. **Fixed four S4-P0 `fix_url`s that named pages the web app does not
   have** (`/settings/sources`, `/projects/{id}/settings`, `/tasks`,
   `/projects/{id}/documents` → connections, model settings, approvals,
   business context). `tests/test_creative_fix_urls.py` now derives the real
   route list from `apps/web/app`. S4-P1's CR-E8/E9 rewrite, merged in, sent
   three more to `{home}/settings`; those now go to `/settings/models`, where
   the allowlist and the project-scoped media settings live.
6. **The design laws apply to `components/creative/**` and the creative
   route directory** — the route is a Stage 04 screen too. `components/ui/`
   predates the law (e.g. `rounded-[var(--radius)]`) and is not rewritten.

## S4-P3

1. **The one-click reduction needed a server answer the PRD did not name.**
   §15.4 B offers "the smallest fitting *scope* reduction", but
   `cost_estimate_v1`'s `reduction` walks the whole degrade ladder and leads
   with `candidates` (one candidate per concept) — which no `CreativeScope`
   field can carry, and the start route re-checks CR-E9 against the full
   scope. Offering it would be a button that changes nothing. Added
   `calc.media.scope_reduction` (the ladder's scope rungs only:
   `third_concept` → two concepts, `video` → off, in ladder order, each
   re-priced by `cost_estimate_v1`) and `EstimateResponse.scope_reduction`.
   `cost_estimate_v1` is unchanged, so no formula version moved. Ruling
   wanted on whether `candidates` / `video_square` should become start-time
   scope fields instead (that would change `CreativeInput`, S4-P0's contract).
2. **Three supported parameters are not offered as run defaults:**
   `aspect_ratio` and `size` fix the frame's shape, which the ratio plan
   decides per rendition (a run-wide 16:9 would contradict a 9:16 rendition);
   `seed` is catalogued as a *may-send* boolean for an integer value, so
   §15.4 B's "boolean → switch" cannot represent it, and a run-wide seed
   repeats itself across every candidate. The server still accepts all three
   in `defaults`. A parameter with one possible value (`n` 1–1 on flux) is
   not drawn: it offers no choice.
3. **No estimate-accuracy history in the model rows.** §15.4 B asks for the
   price line "with its estimate-accuracy history"; `MediaModelRow` has no
   such field, no route serves per-model accuracy (§9.3 "shown in
   Settings"), and no media job has run to measure one. Nothing is drawn
   rather than an empty or invented figure. Needs a field once S4-P9+ write
   `GenerationJob` actuals.
4. **A new recorded fixture:** `openai/gpt-image-1`'s endpoints (free
   public GET, 2026-09-24), because none of the four recorded priceable
   image models takes `quality` — the "no quality control without quality"
   check needs a model that has one.
5. **Dark `--accent-fg` changed from white to `#09090b`.** White on the dark
   accent is 3.7:1 and fails AA for every primary button in dark mode; the
   Start dialog is the first Stage 04 screen with one, so axe caught it here.
   Every `accent-fg` use is text on an accent fill.
6. **The Start trigger shows whenever both pins resolve**, for
   `creative_execute` holders. The landing's blockers are for the *default*
   scope (the project's default models); the dialog prices the scope actually
   chosen, and the start's 409 renders any project-level blocker verbatim.
   Choosing which blockers the dialog "can fix" would be eligibility logic in
   TypeScript.
7. **The project's saved default models and params are not prefilled** —
   there is no read for `Project.settings.media_models` (only `PATCH`). The
   dialog starts with no model chosen, which is also what law 36 asks.

## S4-P4

What S4-P4 had to decide that the PRD does not settle. Items 1 and 2 were put
to Soham during the phase and answered; the rest want a ruling.

1. **`CreativeInput` now lives on the run (migration 0021).** §4.3 rule 1
   says it is "passed read-only to every node", but S4-P0 kept only its hash
   and the worker cannot rebuild it (scope and media choices are request
   parameters; offers, references and sign-off are live rows). Ruled: a
   nullable `run.creative_input` jsonb, creative runs only (CHECK), written in
   the run's INSERT and re-hashed against `input_hash` by the executor before
   the first node. Creative runs started before 0021 fail with
   `creative_input_missing` — there is nothing to backfill them from.
2. **`extras.snippet_headers` / `extras.lead_form_question_types`.** §9.5
   writes `value: [...]`. Ruled: Google's own lists, `source: unverified` as
   §9.5 labels them — the 13 headers of Google Ads Help answer 6280012, and the
   116 values of googleapis v25 `LeadFormFieldUserInputTypeEnum` less its
   UNSPECIFIED/UNKNOWN sentinels. Nothing reads them before 4.3.1/4.3.3.
3. **The VISION modality guard fails closed.** §9.6: "Router rejects a model
   whose `input_modalities` lacks `image`". The text router has no catalogue,
   so a `vision` override is refused outright and the seed
   (`google/gemini-2.5-flash`, fallback `anthropic/claude-haiku-4.5`, both
   image-capable) is used. The first VISION caller (4.4.2) should wire the
   check against the live catalogue and then accept overrides.
4. **`MediaPlanSummary.calc_evidence_ids` (plural).** §11 says "estimate
   `calc_evidence_id`". The executor's citation check (Stage 02 §9.1 item 4)
   only reads the plural field, so the singular would have gone unchecked. It
   cites both calculations: `media.cost_estimate_v1` and `media.ratio_plan_v1`.
5. **`AdGroupBrief.top_keywords` is the top 3 by search volume.** §12.1 names
   the field, not the count, and §9.5 has no constant for it. Three keeps a
   dozen ad groups on one page; the rule is `brief.TOP_KEYWORDS`. A constant in
   `creative_constants.yaml` would need a §9.5 amendment.
6. **`CreativeBrief.offer` is always `null` until S4-P8.** An `OfferBinding`
   names an `offer_record_id`, and `CreativeInput.offer_records` are
   guardrails `OfferRecord`s with no id. Binding needs S4-P8's `offers.py`
   and either an id on the snapshot or the Evidence row id carried beside it.
7. **What an approver may edit at G7.** §5.3 says the edit is "revalidated
   against `CreativeBrief` and re-hashed". S4-P4 also refuses, as a 422 naming
   the field: any change to what code wrote (`plan_ref`, `ruleset_ref`,
   `offer`, `non_negotiables`, `visual_constraints`, `media_plan`); a change to
   which ad groups exist or to their landing URL, keywords or KPI; a source
   the brief did not already cite; a proof point the pin does not license at
   decision time. The approver rewrites lines; they cannot introduce facts.
8. **The spend gate reads the decision as well as the hash.** §8.3 says "G7
   `approved` with `approved_hash == brief_hash`". `assert_g7_approved` now
   requires the brief's `approval_id` to be an approved G7 row of that run;
   S4-P1's fixture wrote a hash with no approval behind it, and now writes one.
9. **G7/G8/G8b route to the *pinned* sign-off matrix** (`Run.creative_input.
   signoff_matrix`), not `plan_approvers`. An owner who is inactive or lacks
   the approver role falls back to any approver, as every gate does (Stage 01
   §16) — so a performance owner who is an `operator` does not block G7.
10. **A run's constants must still be the ones it started under.** If a
    project's `creative_overrides` change while a run waits on G7, resuming it
    fails with `creative_constants_changed` rather than silently producing
    assets under thresholds its input does not name.
11. **§8.1 says the one-DAG-per-node test "covers 84 nodes"; it is 85** on
    main today (23 research + 20 plan + 18 guideline + 24 creative). The test
    derives the set rather than counting, so it covers whatever is registered.
12. **Stage 03's nodes never receive their `GuidelineInput`.** Found while
    wiring `ctx.creative`: `nodes/content/stage_3_3.py` and `stage_3_4.py` read
    `getattr(ctx, "guideline", None) or ctx.scratch.get("guideline_input")`,
    and a grep of `src/` finds nothing that sets either — so those helpers
    always see `None` (e.g. a bound plan's slate never scopes 3.4.1's spec
    sheet). Not verified at runtime and not touched here: it is Stage 03's.
13. **Migration number.** S4-P4 takes 0021. S4-P3 (#60) merged while this
    phase was building and added no revision, so 0021 follows 0020 cleanly;
    any other branch holding a 0021 must renumber — a duplicate alembic
    revision is not a git conflict.

## S4-P5

What S4-P5 had to decide that the PRD does not settle. Every item wants a
ruling; items 1 and 2 are the ones that change behaviour outside this phase.

1. **The PRD's ordering gap: 4.2.3 consumes 4.2.2, which S4-P6 builds.** §11's
   edges feed 4.2.2's descriptions into 4.2.3; §21.3 builds 4.2.2 one phase
   later. S4-P5 defines 4.2.2's *output contract* now
   (`schemas/search_ads.ClaimBoundDescriptionsOutput`, with `claim_ids ⊆
   licensed(pin)` as a validator that refuses to run without the pin's ids) and
   leaves the node a stub. The integration test feeds 4.2.3 fixture
   descriptions through that schema. **Consequence: until S4-P6 merges, a live
   creative run fails at 4.2.3** ("4.2.2 claim_bound_descriptions is still a
   stub"). Rejected: judging headline pairs only — the report would look
   complete and would never have checked the headline × description pairs
   Google serves. Ruling owed: accept the window, or merge S4-P6 with or
   before this.
2. **A Stage 03 defect fixed here: spec-sheet rules ignored their asset type.**
   `RuleScope.matches` never read `asset_types`, and `CountMatcher` compared
   its entity (an asset type, `headline`) with a surface (`rsa_headline`). So
   against any ruleset compiled from the spec sheet a Search path's 15
   characters failed every legal headline, and every Search lint reported "0
   of headline". **Every Stage 04 lint against a real published ruleset
   failed**; S4-P4's tests used a ruleset with no asset rules and could not
   see it. Fixed with `schemas/guardrails.SURFACE_ASSET_TYPES`, which narrows a
   rule only where the surface's asset type is known. This changes the verdicts
   of rulesets already published, but only by removing findings that applied a
   rule to the wrong asset type. Rulings owed: accept a Stage 03 change in a
   Stage 04 PR; confirm the mapping (`asset_group_description → description`;
   `display_text`, `youtube_script` and `landing_page_section` left unmapped,
   so asset-typed rules still apply to them).
3. **Still open from the same root: set rules ignore the submission's campaign
   types.** A count rule is evaluated on every lint call for every campaign
   type the ruleset carries, so an assembled Search ad linted against a
   ruleset that also specifies Performance Max reports "0 of headline" for
   PMax. Candidates are unaffected (item 4); the ad-level lint (4.6.1) will
   need to scope set rules to the campaign types it submits. Not built.
4. **Candidates are linted without set rules** (`lint_adapter.lint_candidate`).
   Law 33 lints every candidate at creation; "3 to 15 headlines" is a property
   of the assembled ad, and asking one candidate would fail every candidate on
   "there is 1 of headline". Every per-target rule still applies.
5. **`combinatorics.pair_flags_v1` — the six flags §11 names but does not
   define.** Each uses structured fields, no word lists: `duplicate` (equal
   after `metrics.normalize`); `near_duplicate` (`copy.trigram_v1 ≥
   copy.near_duplicate_trigram`); `offer_conflict` (percentages or same-currency
   amounts *outside* the licensed claim span that differ); `claim_conflict`
   (both carry claims, not the same ones, and a quantity inside their claim
   spans differs — durations, counts keyed by the word counted, percentages);
   `cta_collision` (both ask, sharing no ask; the CTA verbs are the leading
   words of the pool's own `cta` headlines); `keyword_stuffing` (HH only: a
   headline's `keyword_ref` repeated in the other headline). Known limits: a
   count's unit is the next word ("#1 SDS tool" counts `sds`); a DKI headline
   carries only its `keyword_ref`, so the keyword Google inserts at serve time
   is not checked against its neighbours.
6. **The metrics.** `copy.trigram_v1` = Dice over the character trigrams of the
   NFKC-casefolded, alphanumeric-only text, padded by one space each side.
   `copy.distinctness_v1` = 1 − the two-way mean of each asset's best-match
   similarity on the other side (not the union of every trigram, which scores
   two ads alike for sharing common words). `creative/metrics.py` owns both
   definitions rather than borrowing `guardrails.normalize.trigram_similarity`,
   because a version must pin its definition; any change is a `_v2`.
7. **`select.headlines_v1`'s rules.** Candidates are ranked in a canonical
   order first; the first pick is the candidate farthest from the rest of the
   pool; the feasibility guard is a count bound, not an exact solve. A quota
   the pool cannot meet is *reported* (`quota_report.met = false`) and the node
   succeeds so long as the spec's `min_count` headlines were selected; below
   that it fails. Ruling owed: is a reported shortfall acceptable, or should
   4.2.1 re-ask for the short categories (bounded)?
8. **4.2.1's own checks, beyond the PRD's words.** A `keyword` headline must
   contain its `keyword_ref` as whole words (on the default text); a `proof`
   headline must cite ≥ 1 licensed claim; DKI accepts only Google's five
   documented capitalisations (keyword, Keyword, KeyWord, KEYWord, KeyWORD —
   support.google.com/google-ads/answer/2454041), one insertion per headline
   and a non-empty default; any other braces (location insertion, ad
   customizers) are refused. A candidate that fails lint stays `draft`; one
   that passes lint but breaks these is `dropped` with its reason.
9. **Market and language for a candidate's `LintTarget`.** The ad group's
   market, else the campaign's, else `*`; the campaign's language, else `en` —
   the fallback Stage 03's lint route uses. A plan with no language on a
   non-English campaign is linted as English. Ruling owed.
10. **Asset rows.** `keyword_ref`, `dki` and `default_text` live in
    `CreativeAsset.fields` (§12.2 `TextAsset` has no such fields).
    `content_hash` — §7.2 names the column, not its definition — is sha256 over
    the canonical JSON of kind, surface, text, fields, claims and offer
    binding (`nodes/creative/_text_assets.py`), so a status change, a re-lint
    or a swap never changes it. `ad_ref` stays NULL until package assembly.
11. **The repair round.** Swapped out → `reserve` (a person can swap it back);
    swapped in → `linted`, lineage `{origin: reserve_swap, parent_id: <out>,
    node_id: 4.2.3}`. The asset in the most bad pairs goes first, headlines
    before descriptions, and a category keeps its kind while its quota has no
    slack. **What the round cannot end is reported (`PairReport.unresolved`)
    and the run continues** — nothing downstream refuses it yet (4.6/4.7).
    Ruling owed: should an unresolved *deterministic* flag fail 4.2.3 instead?
12. **One labelling pass after the round.** The pairs a swap forms are
    labelled once, so every pair in the report has a label; they are never
    repaired. That is at most ⌈new pairs / 50⌉ more CLASSIFY calls per ad.
13. **Pins.** HH → H1/H2, HD → H1/D1, DD → D1/D2, in the order the pair reads;
    an asset is pinned once; a pair that contradicts an earlier pin is left
    unpinned. Several assets may share a position (Google rotates within it),
    so assets pinned for two different pairs can serve crosswise.
14. **The executor commits a failed attempt's writes.** `finish_node` commits
    the session with the failure record; nothing rolls the attempt back. So
    4.2.1 builds everything before writing and clears its own rows on entry,
    and 4.2.3 writes an absolute state. A rollback in the executor before the
    failure is recorded would make every node safe by default (not built).

## S4-P9

Worker image, media references, 4.4.1 `creative_concepts`, 4.4.2 `image_masters`.

1. **The PRD's `Dockerfile.worker` is the repo-root `Dockerfile`.** Railway on
   this project honours only Root Directory + `Dockerfile` (see that file's
   header), so the ffmpeg / exiftool / fonts layer went there. Verified on
   Railway: deployment `744e354f` built from `Dockerfile` and logged
   `ffmpeg version 4.4.2-0ubuntu0.22.04.1` and `exiftool=12.40` at build and
   in `worker.startup`. That deploy was of this branch (`f6ae646b`): the next
   merge to `main` redeploys `main`'s worker, without ffmpeg, until this PR
   merges.
2. **An `image_right` names its reference in `proposed`.** `creative_exception`
   has no reference column; an H3 `image_right` about a reference is
   `proposed = {basis, flag: "third_party_reference", reference_id}` —
   `media/references.image_right_proposal` builds it, `cleared_image_rights`
   reads it. 4.6.2 must write it through that constructor. A clearance counts
   only with `status='cleared'` AND a `decided_by`, and only in its own run
   (package-scoped, §8.6).
3. **`composited_real` accepts any registered product photo, whatever its
   origin.** §18 says "composited_real (if a real product photo is
   registered)"; Law 44 governs what reaches a *provider*, and compositing is
   local. If a third-party photo must also be cleared before it is composited
   into an ad, that is a ruling — today such a concept resolves
   `composited_real`, not `none`.
4. **A model with neither `n` nor `seed` makes one candidate.** Law 37 keys a
   job by its request, so identical requests are one job. `n` is used when the
   model takes `candidates_per_concept` in one call; otherwise a derived seed.
5. **VISION overrides stay refused.** `llm/router.py` expected "the first
   VISION caller (4.4.2)" to wire the image-input check against the catalogue;
   4.4.2 uses the seed VISION chain (gemini-2.5-flash, claude-haiku-4.5 — both
   image-capable) and the router still refuses a VISION override. Not in the
   phase's exit criteria.
6. **Candidates and masters carry no XMP yet.** `MediaArtifact.disclosure` is
   NULL on them; §13's stamping happens on renditions in `postprod/` (4.4.3,
   S4-P10). Neither leaves the app.
7. **Uploads are written by the worker.** `api` mounts no Volume, so
   `POST /projects/{id}/media-references` hands the bytes to the worker's
   `store_reference` job and writes no row unless it returns. The same issue
   exists, unfixed, in `POST /human-tasks/{id}/attachments`, which writes
   through `get_storage()` from `api` — on Railway that file lands on the api
   container's ephemeral disk.
8. **A retired reference cannot be re-registered.** The same bytes are a 409
   naming the existing (retired) reference; the PRD has no un-retire.
9. **Image lint targets use the Stage 03 image route's defaults** — `market or
   "*"`, `language or "en"` from the plan campaign. Image rules are scoped by
   campaign type, so neither changes an image verdict today.
10. **Contract drift on `main`, not fixed here.** `TaskClassRouting`,
    `ModelListResponse`, `NodeState` and `RunResponse` were not re-exported
    after S4-P4 added `copywrite`/`vision`/`image_gen`/`video_gen`;
    `scripts/export_schemas.py` rewrites them. This PR exports only its own
    changes (`MediaReferenceOut`, the four image surfaces).
11. **Reconciled with S4-P5 (#63) at merge.** 4.4.2 lints each candidate with
    `PinnedLinter.lint_candidate` — per-target rules only — because a lone
    image counted against the spec sheet's set rules is "0 of image_square"
    and "0 of headline", which failed every candidate against a real
    published ruleset (the hand-built fixture had no set rules, so nothing
    saw it). The four image surfaces stay OUT of `SURFACE_ASSET_TYPES` on
    purpose: one image surface is several spec-sheet asset types
    (`image_landscape`/`image_square`/`image_portrait`, by ratio), and an
    unmapped surface keeps every asset-typed rule applying. The 4.4.2 tests
    now pin rules `guidelines/synthesis.py` itself compiles from the spec sheet.

## S4-P18

1. **The console and the brief page had no reads to render.** §16 lists
   `GET /creative-runs/{id}/brief`, `…/assets`, `…/generation-jobs` and
   `POST /generation-jobs/{id}/check`, but no phase in §21.3 names them, and
   S4-P18's screens are their first consumer — S4-P4 wrote the brief and G7
   behind the approvals surface only. Added exactly those four, with §16's
   paths and permissions, in their own `routes_creative_runs.py`. Ruling
   owed: that they belong here rather than to a backend phase.
2. **The two spend meters ride `GET /runs/{id}`.** §16 says "reuse the Stage
   01 run surface unchanged", and no route reads law 43's reservations.
   Added `RunResponse.creative_spend` (null on every other stage): per cap,
   spent + reserved + cap — the numbers `budget.py`'s reserve script
   compares — rather than a new route. Null, and the meters withheld, when
   Redis cannot say what is reserved.
3. **What G7 authorises is computed server-side** (`creative/g7.authorises`):
   2 RSAs per ad group of a campaign whose `type` is `search` (4.2.3 writes A,
   4.2.4 writes B), image and video *jobs* and `media_usd` from the brief's
   hashed media plan. An untyped campaign counts no RSAs. §15.4 D's "up to
   $38.40" is read as the media plan's estimate, not `max_media_cost_usd`.
4. **Admins keep the G7 override** (Soham, 2026-09-25). The prompt said decide
   controls are absent "for everyone except the performance owner"; the
   approvals surface's `can_decide` also admits an admin (Stage 01 §6.1
   Authorization 3), and the UI follows `can_decide` — no role logic in
   TypeScript. Only H3 excludes admins.
5. **G7 is decided on the brief page only.** The console's node panel and the
   approvals inbox now show a G7 item as a hand-off to the brief page
   (`BriefGateSummary`) instead of the generic `ApprovalCard`, which offered
   Approve on a JSON proposal with none of the numbers §15.2 rule 8 asks for.
6. **`Check again` runs in the worker.** Re-polling waits out a video's poll
   window (minutes), so the route queues `check_generation_job`, which builds
   `MediaJobs` from the workspace's OpenRouter connection (`media/runtime.py`)
   — the first production wiring of S4-P1's job layer. S4-P9 will need the
   same factory for 4.4.2; whichever lands second should reuse it. The arq id
   carries the job's state so a later timeout can be checked again (arq keeps
   a finished id for an hour).
7. **`checkable` is narrower than §16's wording.** "Re-poll a timed_out or
   unknown_submit_state job": a video in `unknown_submit_state` is never
   re-POSTed (law 37) and has no provider id to poll, so it is refused (409)
   and not offered; an image is offered only with its run's own pinned choice
   and no references (their bytes are S4-P9's). Also fixed on the way: the
   predicate reads `request.input_references` off the stored dict, because
   the redacted request holds references without bytes and
   `ImageRequest.model_validate` would reject it — `MediaJobs.check()` still
   validates first and has that latent failure for referenced images.
8. **The offer's `live` tag and end date wait on S4-P8.** `CreativeBrief.offer`
   is always null until then (S4-P4 item 6), and nothing serves an
   `OfferRecord`'s state. The brief renders a binding's resolved fields as
   bound; the tag needs S4-P8 to put that state on the brief view.
9. **No character counters on the Assets tab yet.** A counter's limit is the
   pinned spec's per surface, and no route serves it to the browser;
   `POST /creative-runs/{id}/lint-preview` (§16) is the Ad Studio's.
10. **The word count is the server's while editing.** It is the count on
    record, labelled "recounted when you approve"; a client-side count of the
    Jinja-rendered brief would be a second implementation of the validator.
11. **Visual baselines cover this phase's screens**, not §15.5's twelve
    (most of the twelve do not exist yet): the console, its Jobs tab, the
    brief as the decider sees it and once approved, each × light/dark ×
    1280/390, plus the inline edit at light/1280 — 17 PNGs under
    `apps/web/tests/visual/s4p18`, recorded by `make browser-s4p18` when
    absent and compared on every later run. Clocks, ids, hashes, presence
    and toasts are masked. Not in CI — the repo has none (S4-P24 wires it).
12. **Pre-existing: no run console fits the screen.** `app/(app)/projects/[id]
    /layout.tsx` wraps every project page in `flex flex-col gap-6` with no
    height, so a console page's `h-full` resolves to auto: the console grows
    to its rail's full length, the rail's sticky stage headings never stick,
    and the canvas's fitted graph lands below the fold (a 24-node creative
    rail made the canvas 2,636 px tall at 1280×900). S4-P18 sizes only its own
    console, `xl:h-main` (a token for `<main>`'s content box: `100dvh − 6.5rem`),
    and leaves the layout alone because the evidence page also reads
    `h-full` and would start scrolling internally. Proposed: `xl:h-full` on
    the project layout once the evidence page is checked, then drop the token.
13. **The harness's resumed run now stops at 4.2.3.** Since S4-P5 (#63) the
    real 4.2.1 writes headlines after G7 — so the harness seeds Stage 03's
    real asset sheet and rules, as S4-P5's suite does (4.2.1 refuses to guess
    a headline limit), and answers 4.2.1's model calls with S4-P5's own
    scripted pool — and 4.2.3 then refuses the 4.2.2 stub, so the run fails
    there until S4-P6. The console baselines show that state. When a later
    phase moves the run further, re-record them: `UPDATE_BASELINES=1 make
    browser-s4p18`.

## S4-P10

Rulings owed on what S4-P10 decided where §9.4, §11 (4.4.3) and §21.3 are silent.

1. **A new constant, `logo.width_ratio` = 0.2 (`source: internal`).** §9.4
   item 3 gives a composited logo a floor (`min_width_px`), clear space and a
   contrast bar, but no size. The logo is `max(0.2 × rendition width,
   min_width_px)` wide. Listed as beyond §9.5 in `test_creative_constants.py`;
   constants version bumped to `2026.09.2`. Rule on the value or the basis
   (width vs area vs shorter side).
2. **Spectral residual is written in OpenCV primitives.** The repo ships the
   base `opencv-python-headless` wheel, which has no `cv2.saliency` module
   (contrib only). `calc/media.spectral_residual` follows the contrib
   `StaticSaliencySpectralResidual` steps (64×64, 3×3 mean on log amplitude,
   5×5 σ8 blur, square, normalise) with `cv2.dft`/`blur`/`GaussianBlur`.
   The alternative is swapping the wheel for `opencv-contrib-python-headless`
   in both images. A frame with zero variance has no saliency (the method
   would otherwise turn its all-zero spectrum into a spike at the origin),
   so a window keeps saliency in proportion to its area.
3. **"Retained saliency" is mass, and textured frames push it toward area.**
   With real grain the residual is non-zero everywhere, so retained saliency
   tracks area plus the subject's share: a 16:9 → 1:1 crop of a centred
   subject kept 71% in testing — a gap at 0.85. The plan-time area bound
   (`ratio_plan_v1`) and the pixel-time saliency bound therefore agree on
   most frames. If crops should be judged on the subject only, the rule
   needs a threshold on the map (not in §9.4).
4. **A plan-time gap stays a gap.** `ratio_coverage` marks a ratio `gap`
   when no supported frame covers 85% of its AREA; 4.4.3 does not then crop
   on saliency even if the subject would survive — the estimate the user
   approved said "gap", and §9.4's re-decision runs one way (crop → gap).
5. **Crops come only from painted frames.** A crop is cut from the master or
   a relay/native output of this concept, whichever covers the ratio best.
   The ratio plan may promise a crop "from 16:9" that the run never paints
   (16:9 not required, master at 1:1): that crop then usually gaps. No extra
   job is sent to create a crop source — "a crop costs nothing" (§9.3).
6. **A failed relay falls back to a crop, never to a second job.** `relaid`
   or `native` jobs that do not complete (or whose rendition fails lint) try
   a crop next; the estimate counted one job per painted ratio.
7. **The spec sheet carries no `format`.** §11 lists "format" among
   `asset_specs`, but `AssetSpec` has no such field, so every rendition is
   JPEG (the only format with §9.4's quality search). A fitted logo slot is
   PNG (transparent padding); over `max_bytes` it is a gap.
8. **Logo variant = measured luminance, placement = least-salient corner.**
   There is no light/dark tag on a registered logo: each is trimmed to its
   visible pixels and its alpha-weighted WCAG luminance measured. Corners
   are ranked by the saliency the logo's footprint would cover, ties in
   bottom-right, bottom-left, top-right, top-left order; the first corner
   where some variant reaches 3:1 gets the highest-contrast one. No stated
   `clear_space_ratio` ⇒ no logo (it cannot be kept), recorded.
9. **Visible labels apply only to rules that name the image surface.** A
   pinned `DisclosureRule` whose `surfaces` include the image surface (and
   whose `markets` include the market, or are empty) is drawn: `prefix`
   top-left, `suffix` bottom-right, `anywhere` bottom-left, white on a 60%
   box, text `video.caption_height_pct` of the frame tall (the one sizing
   constant for burned-in text). A rule naming no image surface is treated
   as a copy rule. Rule on both mappings.
10. **Fitted logos are assets.** Each registered logo × spec-sheet logo slot
    (`asset_type` containing `logo`) is a `logo` `CreativeAsset` (node 4.4.3,
    `generated_by_ai=False`, `lineage.origin=reused`) with one `rendition`
    artifact, derivation `composited`, disclosure NULL. It is measured and
    linted like any image (`generated_by_ai=False`); failing lint is a gap.
11. **A relay sends the master regardless of `media_references_allowed`.**
    Law 44 governs uploaded references; the master is the provider's own
    output. "Check again" now re-reads it from the run's MASTER artifact
    (re-hashed; changed bytes are refused), where it used to die as
    `unknown_reference`.
12. **Rendition lint lives in the output, not on a row.** Renditions are
    `media_artifact` rows on the concept's asset (4.4.2's), which has one
    `lint` column (the master's). Each rendition's `CandidateLint` is in
    `renditions[].lint`; a failing rendition is never written.
13. **Two unit tests and three more cannot run in the worker container.**
    `test_creative_fix_urls.py` and `test_deployment_config.py` read the
    repo root (`parents[3]`), and `test_export_tokens.py` /
    `test_nfr_stage_02.py` read an unset `FILE_TOKEN_SECRET` and the
    Makefile: the test service sets the one and mounts neither. They pass on
    the host; everything else ran in the worker image (exiftool, tesseract).
