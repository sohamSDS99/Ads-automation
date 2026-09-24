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
