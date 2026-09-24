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
