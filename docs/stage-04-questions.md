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

## S4-P6

What S4-P6 had to decide that the PRD does not settle. Items 7, 11 and 12 are
the ones that decide whether a live run gets past Stage 4.2.

1. **S4-P5 item 1 is closed.** 4.2.3 now reads the real 4.2.2. Its "4.2.2 is
   still a stub" guard and the test for it are gone, and S4-P5's integration
   test scripts 4.2.2's COPYWRITE pool instead of registering a fixture node.
2. **"Never an asset" means no row at all, not even a draft.** In 4.2.2 and
   4.2.5 (and B, through 4.2.2's path), a candidate carrying an unlicensed
   claim-shaped span is withheld whole. Its spans become
   `exception_candidates[]{span, occurrences}`, and no `CreativeAsset` is
   written for it. **4.2.1 (S4-P5) does not do this:** a headline with an
   unlicensed span fails lint and is stored as a `draft` row, and nothing is
   collected. Ruling owed: should 4.2.1 withhold and collect the same way?
3. **Exception candidates are also collected by 4.2.5.** §11 lists
   `exception_candidates` for 4.2.2 only, but law 34 covers every span, so
   4.2.5's output carries them too. `exceptions.max_exceptions_per_run` is not
   applied when candidates are collected; it belongs to the node that turns
   them into `CreativeException` rows.
4. **4.2.2 trusts the model's claim binding.** `claim_ids` must be licensed at
   the pin (the enum plus the schema validator), and `claim_text` must be a
   verbatim, case-insensitive quote from the description. Nothing checks that
   the quoted words actually state the cited claim; checking would mean a
   second claim matcher (law 33). So a description can cite a licensed claim
   it does not state. Accept, or give Stage 03 a rule for it.
5. **Description selection is not a named method.** 4.2.2 selects in written
   order and skips any near-duplicate of one already picked (`copy.trigram_v1`
   at or above `copy.near_duplicate_trigram`, the pair 4.2.3 would block). The
   rest are reserves. §11 names `select.headlines_v1` for 4.2.1 and nothing for
   4.2.2, so no versioned id was invented. Accept, or define
   `select.descriptions_v1`.
6. **Paths are asset rows** (`kind='path'`, `rsa_path`), linted like
   everything else. A path that fails lint leaves `None`, and a passing path2
   moves up to path1, because Google takes path2 only after path1.
7. **Distinctness is hard to reach when both ads must use the same keywords.**
   B has to meet the same keyword quota with the same three keywords, each
   used once (otherwise `keyword_stuffing`). A long keyword leaves about four
   characters to vary in 30, so B's keyword headlines score 0.88–0.93
   `copy.trigram_v1` against A's. On the test copy, B cleared
   `variant_min_distance` 0.65 only after its proof and CTA lines were
   reworded (0.635 → 0.683). Expect live runs to fail 4.2.4 often, especially
   when an ad group has long keywords and one licensed claim that every
   description must state. Options: leave keyword headlines out of the metric
   (a `copy.distinctness_v2`), lower the threshold, or accept.
8. **Who states the hypothesis and the metric.** Code does: the hypothesis is
   written from the brief's approved lines ("Variant B, led by “angle_b”,
   beats variant A, led by “primary_message”, on {kpi}."), and
   `primary_metric` is the ad group's KPI (the brief's `kpi`: the plan's
   campaign `primary_kpi`, or the north-star metric). The PRD does not say who
   writes either. The model writes neither, so neither can assert anything
   unsourced.
9. **A B that paraphrases A fails the run.** 4.2.4 raises, writes nothing of
   B, and the executor's bounded retry is the only re-ask. 4.2.4 is on the
   path, so the run fails. Accept, or make a failed B non-fatal and package A
   alone.
10. **`ResponsiveSearchAd` (§12.2) is defined here and used first for
    `ad_b`.** A's RSA is not assembled in this phase. `ad_ref` is
    `{campaign_ref}/{ad_group_ref}/{variant}`, a convention invented here;
    `CreativeAsset.ad_ref` stays unset, as it does on S4-P5's rows.
11. **4.2.5 cannot write anything with today's constants.** Content constants
    2026.09.2 have no `performance_max.business_name` spec and no `demand_gen`
    or `display` specs at all. So every non-Search slate fails 4.2.5 with
    `spec_missing`, which names every gap at once and never guesses a limit.
    The performance owner has to add these specs, with sources, as a Stage 03
    constants MINOR.
12. **The short-description rule "from specs" is not built.** The sheet has
    no `short_description` asset type, and no Stage 03 surface maps to one.
    Enforcing a 60-character limit in Stage 04 code would reimplement a
    character limit (law 33). Stage 03 needs to add both first; then 4.2.5
    writes it. Warning: adding a `short_description` asset type to the sheet
    today would push its length rule onto every surface that has no asset
    type (`display_text`, `youtube_script`, `landing_page_section`), under
    S4-P5's rule that an unknown mapping keeps applying.
13. **Demand Gen and Display have no Stage 03 text surfaces.** 4.2.5 lints
    them as `pmax_headline`, `long_headline`, `asset_group_description` and
    `business_name`. Those are the surfaces whose asset type matches the spec
    each line is written against, so the pin's limits for that campaign type
    apply. Performance Max descriptions use `pmax_description`. `display_text`
    has no asset type and cannot be used. Ruling owed on the mapping.
14. **4.2.5's business name is written by the model.** `CreativeInput`
    carries no advertiser name. It belongs in Stage 03's `CreativeContext`.
15. **4.2.5 writes exactly each spec's `max_count`** (15 / 5 / 5 for
    Performance Max) and keeps every line that passes lint; §11 names no pool
    or selection for it. Fewer passing than a spec's `min_count` fails the
    node.
16. **What `not_required` covers in 4.2.5.** It is returned when the slate
    has none of the three types (§11), and also, with the reason stated, when
    the slate has one but the run's brief covers no asset group in it (a
    scoped run, or a plan with none). Only campaign types on the slate are
    written for: a Performance Max campaign in the account structure but not
    on the slate gets no text.

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

## S4-P7

What S4-P7 had to decide that the PRD does not settle. Item 1 changed Stage 03
code; items 4, 5 and 14 are the ones a ruling most changes.

1. **`landing_page_section` is now its own asset type (Stage 03 change).**
   Unmapped in `SURFACE_ASSET_TYPES`, it fell into the unknown-surface
   fallback, so every asset-typed spec rule applied to it:
   `asset_spec.length.v1` failed a 22-character H1 against the 15-character
   path limit, and **no proposed H1 could ever pass lint.** It maps to
   `landing_page_section`, which no spec-sheet entry names; brand, claim and
   policy rules (not asset-typed) still apply. S4-P5's fail-closed test used
   this surface as its example; it now uses `display_text`, the principle
   unchanged. Stage 03's own landing-page-section lints lose the ad length
   limits too, which I believe is right. Ruling owed.
2. **`match.token_trigram_v1` is defined as token-aligned trigrams.** Per
   headline: each distinct word is matched to its closest H1 word by
   `copy.trigram_v1`'s trigram Dice and the headline scores the mean (the
   share of the ad's words the page repeats, so a longer H1 loses nothing).
   An ad group scores its best-echoed final A headline (Google may serve any
   of 15 deliberately different ones); the page scores its weakest (ad group,
   device). A device that did not render is left out; neither ⇒ `unavailable`.
3. **The proposed H1 is one COPYWRITE call for 3 candidates.** Each is linted
   with `lint_candidate` as `landing_page_section` in every (campaign type,
   market, language) of the ad groups on the page, and the worst verdict
   stands. Only a candidate that passes lint **and** reaches the threshold is
   proposed; ties go to model order. None ⇒ `proposed_h1: null` with a note,
   and no second call.
4. **The offer phrase is `OfferBinding.resolved`'s first value — and it is
   dormant today.** 4.1.1 writes `offer: null` into every brief until S4-P8's
   `offers.py`, so live runs never check an offer; the node test supplies the
   phrase. S4-P8 should confirm the first resolved value is the one an offer
   leads with, or name the field.
5. **"In a text node" is taken literally.** `<strong>20%</strong> off` is two
   text nodes, so the phrase is not found and the page is
   `blocking_for_launch`. Precise, but a false block on markup like that.
   Ruling owed: literal text nodes, or the text of the nearest block element?
6. **The audited form** is the desktop render's form with the most visible,
   fillable fields (a footer newsletter loses); a tie goes to the first.
   Hidden inputs, buttons and CSS-hidden honeypots are not fields, so they are
   never proposed for removal. Radios and checkboxes sharing a name are one
   field.
7. **The routing contact field.** 2.1.4's `RoutingRule{segment, owner}` names
   no channel, so the contact a lead is routed by is chosen in code: a field
   the site already requires first, then email before phone, then document
   order. CLASSIFY gives each field one label, so if a required signal is
   itself a contact ("company email", as in the seed plan) the email is kept
   for that signal and a phone field can still become the routing contact.
8. **`missing_signals`** (required signals no field carries) is recorded on
   the form and does not change the verdict. Ruling owed: should a form that
   cannot capture a required signal be `needs_change`?
9. **`obscured_by_overlay`** counts `position: fixed` elements only (§10.2
   says "fixed"; a sticky header is not counted), per element rather than
   their union, and it is recorded without changing the verdict.
10. **Verdicts.** `unreachable` when either device did not answer 2xx; per Q10
    that is blocking for launch, and it is its own verdict value. A missing
    offer above the fold on any device ⇒ `blocking_for_launch`; a failed match
    or fields beyond the minimal set ⇒ `needs_change`. 4.5.1 writes a
    preliminary verdict on the row and 4.5.2 replaces it.
11. **`networkidle` within 20 s** is one budget: `load` first, then
    `networkidle` in what is left. A page that never idles (long-poll, chat
    widget) is still measured and recorded `settled: false`, not unreachable.
12. **A non-GET navigation is answered `204`, not aborted.** An aborted
    navigation makes Chromium commit an error page over the document being
    audited; `204` means "stay here". Fetches, XHRs and beacons are aborted.
    Nothing non-GET reaches the network either way (asserted in the browser
    and in the fixture server's log).
13. **Screenshots are storage keys with no route.** §16 lists no landing
    screenshot route, and `GET /media/{id}/content` belongs to media rows.
    S4-P20's `FoldOverlay` needs one; not built here.
14. **No private-address guard.** The renderer loads whatever `landing_url`
    the frozen plan names, including a private or metadata address, as
    `web_crawler` already does. The plan is operator input, but this is an
    SSRF surface from the worker. Ruling owed: add an allowlist or
    public-address check to both?
15. **Concurrency 1 is per worker process** (a lock per event loop). Across
    worker replicas it is not enforced; Railway runs one worker.
16. **Tests need a browser.** The integration suite gets an autouse renderer
    that returns unreached pages, so other phases' runs (pointed at
    example.com, no Chromium in the default test image) record `unreachable`
    and call no model. `test_s4p7_landing.py` renders for real and needs the
    worker image; `tests/preview` and `tests/creative/test_landing_audit.py`
    need `playwright install chromium-headless-shell` on the host.
17. **`GET /landing-audits/{id}/patch?format=html`** is served inline as
    `text/html` with `Content-Security-Policy: default-src 'none'; sandbox`:
    every value is escaped when the patch is built, and a browser that opens
    it still runs nothing. `404` when the audit proposes no change.

## S4-P11

Rulings owed on what S4-P11 decided where §8.4, §9.4 video 1–2, §11 (4.4.4)
and §21.3 are silent.

1. **`AssetSpec` gained a duration window (Stage 03, additive).** §9.5 says a
   surface lacking "video durations" is `spec_missing` until a Stage 03
   amendment adds the spec — but `AssetSpec` forbade the keys, so no amendment
   could. `min_duration_s` / `max_duration_s` are optional and omitted from the
   dump when unset, so every existing spec sheet and ruleset hash is byte for
   byte unchanged (a test pins it). **No Google number was added**: the shipped
   `content_constants.yaml` has no video asset type at all, so on every real
   ruleset today 4.4.4 is `not_required` ("no video surface"). The
   performance owner adds video specs (ratio + window) to make video possible.
2. **How the length is chosen inside the window.** With a minimum: the
   shortest length ≥ it (and ≤ any maximum) the model's durations sum to —
   the least footage and spend the spec allows. With only a maximum: the
   longest ≤ it (a bumper is made at its cap). A campaign's video types must
   share one window (their intersection); disjoint windows are
   `conflicting_duration_specs`. Rule on "shortest", and on whether a length
   should instead be a creative choice the brief records.
3. **The shot plan's tie-breaks.** Fewest clips (fewest seams and per-job
   minimums), then the most even split (20 s from 4/6/8 is 8+6+6, not 8+8+4),
   then longest first. A length the durations cannot sum to exactly is a
   `CalcError`, never a trimmed clip.
4. **The estimate G7 approved can under-count video.** `cost_estimate_v1`
   (S4-P1) prices one job per video ratio at the chosen `duration` (or the
   longest supported). The shot plan makes N clips summing to the spec's
   length, so a 10 s video from 4/6/8 s clips is two jobs, not one. The caps
   still hold (a refused reservation is `blocked_by_budget`), but G7's
   "authorises the estimated spend" does not. Proposed: price the shot plan in
   the estimate — it needs the spec window at estimate time, which
   `estimate_inputs` can read from the pin. Not built (S4-P1's formula).
5. **One script and one concept per campaign.** The estimate counts one video
   per ratio per campaign, so 4.4.4 makes one: from the campaign's first 4.4.1
   concept, one script serving every ratio. Rule if a video per concept is
   wanted (it multiplies video spend by `concepts_per_campaign`).
6. **The model never times anything.** It writes one shot per clip (visual,
   voiceover, on-screen text) and the CTA; `t0`/`t1` are the shot plan's, and
   captions are the voiceover — one per voiced beat — so every voiceover
   interval is captioned by construction. `VideoScript` still validates it
   (and a draft that never shows the CTA is refused before it is timed).
7. **The script is committed before any clip is submitted, and reused on
   resume.** A resumed or retried 4.4.4 reads back its `video_script` asset
   instead of asking `COPYWRITE` again: a new script is new prompts, new
   idempotency keys and a second bill for every clip. The kill test asserts
   the script is asked for once (the POST count alone cannot prove it — the
   test's schema-filled answers are identical every time; a mutation proved
   the script-count assertion catches it).
8. **`youtube_script` → `video_script` in `SURFACE_ASSET_TYPES` (Stage 03).**
   Unmapped, every `headline`-scoped rule reached every voiceover line: against
   a real Performance Max ruleset every script failed "36 chars; the limit is
   30". Mapped, headline limits stay on headlines; unscoped rules (never terms,
   claims, policy) still reach the script, and a future `video_script` spec
   would. Each line is linted alone with `lint_candidate` (set rules are the
   assembled ad's).
9. **Every clip prompt forbids the product, even for `reference_guided`.**
   §11 gives 4.4.4 no `references` input and 4.4.4 depends on 4.4.1 only (not
   4.4.2), so no reference or master reaches the video model and nothing
   composites a product into a clip — a depicted product could only be an
   invented one (Law 38). Also forbidden: text and logos (captions, logo and
   end card are code's). A `relaid` ratio is painted from the prompt at that
   ratio (text-to-video), as the estimate priced it.
10. **A crop ratio is P12's; without a painted source it is a gap.** A required
    video ratio the model does not paint but can crop from a ratio this video
    *is* made at is recorded `{"plan": "crop", "from": …}`; otherwise
    `no_source_ratio` — the estimate priced no clip to crop it from.
11. **`timed_out` fails the node; failed / expired / cancelled /
    `unknown_submit_state` are gaps.** A timed-out job is still alive, so it is
    not a gap: the node fails naming each job and "Check again". Check again
    (S4-P18's route and worker, unchanged) finishes and downloads it; the
    operator then retries 4.4.4, which finds the clip done with no POST. Check
    again does not retry the node by itself — rule if it should.
    `unknown_submit_state` for a video stays a person's decision (409 on
    Check again); there is no "Submit again (may double-bill)" route yet
    (§18 names one, §16 lists none).
12. **Budget exhaustion mid-video leaves paid clips of an incomplete ratio.**
    Reservations are per job, so when the cap refuses clip k of a ratio, clips
    before it are spent and the ratio is a `blocked_by_budget` gap; no later
    clip of any campaign is submitted, and the rung taken is recorded
    (`video`, or `video_square` for 1:1 — square is made last so the ladder's
    first video rung is what runs out). Reserving a whole ratio's clips at once
    needs a group reservation in `budget.py`.
13. **The video asset's surface is `video_frame`** (§7.1's delta name); it is
    not yet in the `Surface` literal because nothing lints a frame before
    S4-P12. The asset stays `draft` until P12 renders and lints it.
14. **`generate_audio`** is sent as the run's choice, else
    `video.generate_audio_default` (false, Q9) whenever the model takes the
    field; `size` is never sent (it would contradict the ratio being made).
15. **`duration_s` is the finished video's length.** Whether P12's end card
    overlays the last `end_card_ms` of the final clip or is appended (which
    would push the total past a maximum) is P12's to decide against the spec.
16. **Clips are recorded as `MediaArtifact(role=clip)` from ffprobe's
    reading** (`postprod.probe_video`, packets counted so a cut-short download
    fails), `aspect_ratio` = the ratio requested. P12 checks the pixels match
    it; the recorded clip OpenRouter returned is 1280×720 whatever was asked.

## S4-P19

1. **The Ad Studio's three writes had no phase.** §16 lists
   `PATCH /creative-assets/{id}`, `POST /creative-assets/{id}/swap` and
   `POST /creative-runs/{id}/lint-preview`; no §21.3 phase names them and this
   is their first consumer. Added exactly those, with §16's paths and
   permissions, in `routes_creative_runs.py` beside S4-P18's reads; the checks
   live in `creative/edits.py`. `drop` and `regenerate` were not added (§15.4 E
   has no drop control; regenerate is S4-P13's). Ruling owed: that they belong
   here.
2. **The lint preview lints each target as a candidate is linted at creation**
   (`PinnedLinter.lint_candidates`: every per-target rule, none of the set
   rules, which count the assembled ad) and measures an `rsa_headline` on its
   keyword-insertion default, as 4.2.1 does. §16 says only "LintResult at run
   pin"; a lone headline linted against the full program fails on "1 of 15
   headlines" every time.
3. **A failing edit is refused, not stored as a draft.** §16 says PATCH
   "re-lints"; law 33 says only a pass leaves `draft`. Storing a failing edit
   would demote the asset to `draft`, out of the ad, and lose whether it was
   carried or a reserve. PATCH answers `422 lint_failed` with the `LintResult`
   and writes nothing; the chip had already said `fail`. Rule on it.
4. **An edit is held to its node's own checks**, imported from the node:
   4.2.1's `invalid_reason` (one well-formed insertion, a keyword headline keeps
   its keyword, a proof headline stands on a claim licensed *now*) and, for a
   description, 4.2.2's `claim_span` on the words 4.2.2 found it stating its
   claim in (`422 claim_removed` names them). A person cannot rebind a
   description to another claim here. Only `headline` and `description` are
   editable (`422 kind_not_editable`), only while `linted` or `reserve`
   (`409 asset_not_editable`), never once frozen (`409 asset_frozen`).
5. **An edit keeps no revision.** §7.2's `lineage` is one record —
   `{origin: human_edit, parent_id (kept), by_user, node_id}` — and the text is
   overwritten in place, so the asset id every node output names stays valid.
   What it said before survives only in the api log (`content_hash_before`).
   Should an edit write an audit row, or the asset keep its revisions?
6. **A swap goes in unpinned and unchecked against its new pairs.** A pin
   belongs to the order-dependent pair 4.2.3 judged; the reserve was never in
   it, so the pin is cleared, not moved. The swap re-lints the reserve at the
   current pin and re-runs its node's checks, but not `pair_flags_v1`, and not
   the quotas: the heatmap shows its pairs as *Not checked — swapped in*, the
   quota bar says *1 short*, and 4.6.4 is the record. Should swap and edit
   re-run the deterministic pair flags (and refuse a quota break) instead?
7. **Pairs after an edit are shown as checked before the edit**, with 4.2.3's
   old finding in words — never as though the old check still held.
8. **CJK headlines are under-counted by the linter, and the counter agrees
   with it.** `measure(text, "chars")` is Python's `len()`: one per code point.
   Google counts a double-width character as two, so a 16-character Japanese
   headline (32 of Google's 30) passes. The counter mirrors the server, as
   §15.5 requires; the defect is Stage 03's `asset_spec.length.v1` unit, not
   fixed here (law 33: Stage 04 never reimplements a limit).
9. **The SERP preview's frame is display chrome**: 600 px desktop, 328 px
   phone (360 less 16 px gutters), Arial at 20/26 and 18/24 title, 14/22 text,
   one headline line on desktop and two on a phone, two and three description
   lines. S4-P15's versioned `preview/serp.py` templates are the record; when
   they land, should the live preview read their widths rather than tokens?
10. **Loading a pair the pins keep apart.** With H1 and H2 pinned, two unpinned
    headlines are never served together. The preview shows them first anyway,
    so the pair can be read, and says Google never serves them side by side.
11. **The heatmap shows H×H and H×D, as §15.4 E says;** 4.2.3's D×D pairs are
    reported but not drawn.
12. **A Stage 03 file changed for Stage 04's latency.** The first lint in a
    process loads simplemma's English data (119 ms), which put the first chip
    at 392 ms of the 400 ms budget. `lexicon.warm()` runs at api startup (a
    failure only logs); the first chip now lands at ~286 ms.
13. **The shared `Dialog` blurs the page behind it** (`backdrop-blur-[2px]`),
    which §15.2 rule 2 bans on Stage 04 screens; the shortcut list on `?` uses
    `Popover` instead. The primitive is unchanged — rule on fixing it app-wide.
14. **`PublishedRuleSet.compiled.asset_specs` was typed without its `specs`
    wrapper** in the web client (the Python model is `AssetSpecSheet{specs}`);
    nothing read it before, and the type is corrected.
15. **Nothing shows who edited a headline or when.** `CreativeAssetItem` has
    `lineage.by_user` but no `updated_at`, and the matrix does not render
    either. Wanted?

## S4-P12

Rulings owed on what S4-P12 decided where §9.4 video 3–5, §18 and §21.3 are
silent, and the defects it found in shipped code.

1. **Verification measures the logo at the size the video shows it.** §9.4
   video 4 says "sample frames … → Stage 03 logo metrics". The pinned
   templates are built by 3.4.3 at `ocr_working_width_px` (1280). ORB matches
   within about an octave of scale, so a logo at `logo.width_ratio` (20%) of a
   1280 px frame — 256 px — scores as noise against them whether or not it is
   there (measured: ~0.10–0.18 on frames with and without it, against the
   0.62 floor). `verify.py` re-registers the SAME registered asset, fitted to
   the placed box exactly as it was composited, through Stage 03's own
   `template_from_bytes`, measures only the placed box (with its clear space)
   through Stage 03's `measure`, and judges at the pinned template's own
   `min_score`. A logo under 256 px is measured with template and crop
   upscaled by one factor (ORB finds too few keypoints below that). Measured
   on the fixtures: 0.83 (16:9) / 0.68 (9:16) while the logo is shown, 0 at
   t = 0, < 0.12 after 4.5 s and for the variant not placed. **The same
   defect makes Stage 03's `image.logo_match` unpassable for any 20%-width
   logo on a still** — rule on whether 3.4.3 should also register templates
   at the scale renditions use.
2. **Stage 03 defect: a transparent logo registers as an empty template.**
   `precheck._load` converts RGBA to RGB without compositing, so the
   transparent field becomes black: a dark-ink logo on a transparent PNG is
   dark-on-black — almost no ORB keypoints, and a polarity no frame shows.
   Such a brand's videos can never pass verification (a blocking gap, never a
   bad file). The fixtures use a logo on its own opaque plate. Not fixed here
   (Stage 03 code, and it would change new registrations' hashes).
3. **Single-pass `loudnorm` does not normalise behind a silent shot.** Its
   dynamic mode sets gain from a 3 s window: a -52 LUFS tone after a silent
   6 s shot came out at -50.9 LUFS. Assembly measures first (`print_format=
   json`) and applies `loudnorm=I=-16:TP=-1.5` with the measurements and
   `linear=true` — the same target, two passes. A clip set whose audio is all
   below the gate is left silent.
4. **The caption face is "Inter Semi Bold".** fontconfig's family name for
   `fonts-inter`'s SemiBold face has a space. `Inter SemiBold` (as §9.4 spells
   it) matches nothing, and libass then draws in DejaVu Sans with no warning.
   The face libass selected is read from its `fontselect` log line; any other
   face fails the assembly.
5. **The safe zone is two new constants.** §9.4 puts the caption box "inside
   the surface's safe zone"; no spec sheet or constant gives one.
   `video.safe_zone_bottom_pct` = 0.20 (the bottom share kept clear for a
   player's controls and a vertical feed's overlay; the captions' MarginV) and
   `video.safe_zone_edge_pct` = 0.05 (title-safe at the other edges), both
   `internal`; constants version 2026.09.3. Rule on the values, and on whether
   they should differ by orientation.
6. **The shot's `on_screen_text` is burned too.** §9.4 lists captions only,
   but the script's `on_screen_text` is "text shown over the shot", and the
   CTA is required to appear there so the ad "works with sound off" — unshown,
   that check is empty. It is drawn top centre in the caption style, clear of
   the logo's corner (margins = logo box + clear space + edge).
7. **The logo sits in a top corner.** The bottom belongs to the captions, so
   `place_logo` gained a `corners=` parameter; the corner and variant are
   chosen on the mean of frames sampled across the logo window (0.5–4.5 s).
   `logo.permitted_surfaces` names video as `video`, not 4.4.4's lint surface
   `video_frame`; the logo check uses `video`.
8. **The end card is appended, and a spec maximum keeps room for it.** The
   file is script length + `end_card_ms`. With a spec maximum, 4.4.4's
   `gather` now plans the script at ≤ max − ⌈end card⌉ (a 6 s bumper cap →
   a 4 s script + 2 s card), since the finished file is what the spec
   measures; a minimum is unchanged (a 10 s floor → 10 s + 2 s). Rule on
   whether the end card should instead replace the last 2 s of footage.
9. **The end card's colour.** "Brand colour token" = the first colour token
   whose role says `primary`, else the first with a readable hex. With no
   token the card is near-black (#111111), recorded as `colour_note`. The CTA
   is white or near-black, whichever contrasts more, and must reach 4.5:1 or
   the card is refused.
10. **What "blocking" does.** A failed verification is a
    `verification_failed` gap carrying the full verification (every logo and
    caption frame, every failed fact); no file is stored. Masters that pass
    are `role=rendition` (the detail drawer's `variant=master`), with a
    `preview` (480p: short side 480, H.264 CRF 28) and a `poster` (the frame
    at `brand_first_at_ms`), both `derived_from` it.
11. **Stamp.** Every assembled video is `compositeWithTrainedAlgorithmicMedia`
    — captions and the end card are code's marks on a model's footage even
    when no logo could be placed. The MP4 `comment` reads "AI-generated video
    (<model>); IPTC DigitalSourceType <uri>". The proxy and poster are stamped
    too. The XMP write keeps `moov` before `mdat` (checked on every file).
12. **Conservative encoder arguments** (§18 names none): one thread, preset
    `veryfast`, bicubic scaling, a 4096-packet muxing queue. Codec, profile,
    pixel format, CRF, frame rate and faststart never change — they are the
    spec. The stderr tail kept is the last 4000 characters.
13. **Free-space rule before each assembly.** CR-E11's factor: free ≥ 2 ×
    the estimated footprint, where footprint = the clips' bytes × the frame's
    pixel growth (≥ 1). Too little is a `storage_insufficient` gap naming both
    numbers; ffmpeg does not run. A backend with no fixed size (`free_bytes()`
    None) is not checked. `StorageBackend` gained `free_bytes()` (statvfs
    `f_bavail`), which walks nothing, unlike `usage()`.
14. **Where the stderr tail is stored.** §18 says "on the artifact". A run
    that failed and was retried successfully stores it on the rendition's
    `MediaArtifact.transform.ffmpeg_failures` (and `failed_attempts` in the
    output); a rendition that failed twice has no artifact — `MediaArtifact`
    has no status column and needs a file — so both tails ride on the
    `assembly_failed` gap in the node output.
15. **Rendition ids are deterministic** — `uuid5(asset_id, "4.4.4:<ratio>:<role>")`
    — so a node that died after writing a file leaves it at the key the retry
    overwrites (§7.4 keys files by media id), never an orphan. The retry
    deletes the earlier attempt's rendition/preview/poster rows and files and
    remakes them from the same clips; nothing is POSTed.
16. **No disclosure label on video yet.** Law 38 composites disclosure labels
    in postprod, but Stage 03's `DisclosureRule.surfaces` has no video surface,
    so no pinned rule can require one; nothing is drawn. When Stage 03 adds it,
    4.4.4 must burn the label.
17. **`frame_check` / `media_probe` derived Evidence** (§7.3) are not written:
    the verification lives in the node output and the probe on the artifact.
    Rule on whether each rendition should cite a `frame_check` row.
18. **Caption OCR reads the caption band twice.** The frame at each caption's
    midpoint is cropped to the band libass draws captions in (from 3 lines
    above the safe-zone margin to the box's bottom) and read inverted and
    binarised on the captions' white; the better similarity is kept. The whole
    bottom half let fractal footage garble a legible caption to 0.77. OCR runs
    in English (`eng`); a non-English market's captions would need its
    tesseract language — rule on the mapping.
19. **Video frames are not linted as images yet.** §7.1 names `video_frame`
    as the surface a video frame is linted as; nothing in §9.4 video says
    which frames. Not built.
