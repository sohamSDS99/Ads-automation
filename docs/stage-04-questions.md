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
   ("not estimable"), never 0. S4-P1 must return a number here.
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
