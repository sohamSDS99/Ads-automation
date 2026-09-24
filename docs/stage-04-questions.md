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

## S4-P2

1. **Media API envelopes assumed by the web (S4-P1 to confirm or correct).**
   S4-P1 builds the routes in parallel, so S4-P2 codes against PRD §16's
   routes and the shapes §7 and the S4-P1 brief name, and fixes the two
   envelopes they leave implicit (`apps/web/lib/api/media.ts`):
   - `GET /media/catalogue?modality=` →
     `{modality, models: CapabilityRecord[], catalogue_hash, warning?}`,
     one record per model *per provider endpoint* (a model with three
     `provider_tag`s is three records; the editor groups them and offers
     the tags as the provider pin). `warning` carries §9.1 rule 1's
     "last-good snapshot" sentence. A missing OpenRouter key is read as
     `409`, as `GET /models` does today.
   - `GET /settings/media` → `{media_allowlist: {image: [...], video: [...]}}`;
     `PUT /settings/media` takes the same body and returns the saved value.
   - `CapabilityRecord.pricing[].unit` for video is read as `second`.
2. **Workspace default params and estimate accuracy are not built.**
   §15.3 lists both under Media generation and §9.2 names "workspace default
   params", but `Workspace.settings` (§7) stores only `media_allowlist`, and
   no §16 route returns estimate accuracy. The params editor would also be
   S4-P3's `CapabilityParams`. Needs a storage key and a read before a UI.
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
   route list from `apps/web/app`. S4-P1's CR-E8/E9 rewrite should keep
   `media_*` blockers on `/settings/models`, which is where they are fixed.
6. **The design laws apply to `components/creative/**` and the creative
   route directory** — the route is a Stage 04 screen too. `components/ui/`
   predates the law (e.g. `rounded-[var(--radius)]`) and is not rewritten.
