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
