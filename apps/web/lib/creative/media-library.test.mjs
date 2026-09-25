/**
 * The Media Library's view model (Stage 04 PRD §15.4 G): the letterbox never
 * crops, every rendition, logo and gap lands in its surface group, the byte
 * label says what the stored output says, and the timeline's windows come from
 * the file's own numbers. Run with `pnpm test:unit`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  angleSource,
  atPercent,
  bytesLabel,
  bytesOfLimit,
  captionMarkers,
  columnsFor,
  endCardRegion,
  gridRows,
  letterbox,
  parsePx,
  ratioValue,
  surfaceGroups,
  tokenColour,
} from "./media-library.ts";

const lint = { verdict: "pass", unchecked: false, ruleset_version: "1.0", rule_ids: [] };

function rendition(concept, ratio, px, extra = {}) {
  return {
    concept_id: concept,
    campaign_ref: "c-sds",
    asset_id: `asset-${concept}`,
    media_id: `m-${concept}-${ratio}`,
    job_id: "j1",
    surface: "pmax_image",
    ratio,
    px,
    derivation: "native",
    scale: { sx: 1, sy: 1 },
    retained_saliency: 1,
    logo_composited: false,
    logo_note: null,
    bytes: 200_000,
    max_bytes: 5_242_880,
    lint,
    disclosure: {},
    ...extra,
  };
}

const CONCEPTS = {
  product_depiction: "none",
  reference_refusals: {},
  campaigns: [
    {
      campaign_ref: "c-sds",
      campaign_type: "performance_max",
      concepts: [
        { id: "c-sds:c1", name: "Clipboard", campaign_ref: "c-sds" },
        { id: "c-sds:c2", name: "Warehouse", campaign_ref: "c-sds" },
      ],
    },
  ],
};

test("a letterbox keeps the true ratio inside the frame and never overflows it", () => {
  for (const ratio of [1.91, 16 / 9, 1, 4 / 5, 9 / 16, 0.02, 50]) {
    const box = letterbox(ratio, 1);
    assert.ok(box.width <= 100 && box.height <= 100);
    assert.ok(box.width === 100 || box.height === 100, "one side touches the frame");
    assert.ok(Math.abs(box.width / box.height - ratio) < 1e-9, "never cropped or stretched");
  }
});

test("sizes and ratios parse, and anything else is refused, not guessed", () => {
  assert.deepEqual(parsePx("1200x628"), { width: 1200, height: 628 });
  assert.equal(parsePx("1200×628"), null);
  assert.equal(parsePx("0x628"), null);
  assert.equal(ratioValue("1.91:1"), 1.91);
  assert.equal(ratioValue("9:16"), 0.5625);
  assert.equal(ratioValue("wide"), null);
});

test("bytes read in the spec sheet's binary units, and an unrecorded limit says so", () => {
  assert.equal(bytesLabel(512), "512 B");
  assert.equal(bytesLabel(188_416), "184 KB");
  assert.equal(bytesLabel(5_242_880), "5 MB");
  assert.equal(bytesLabel(1_572_864), "1.5 MB");
  assert.equal(bytesOfLimit(188_416, 5_242_880), "184 KB / 5 MB");
  assert.equal(bytesOfLimit(188_416, null), "184 KB · no limit");
  assert.equal(bytesOfLimit(188_416, undefined), "184 KB · limit not recorded");
});

test("renditions, fitted logos and gaps group by surface; a gap sits among its concept's files", () => {
  const groups = surfaceGroups(
    {
      renditions: [
        rendition("c-sds:c2", "1:1", "1200x1200"),
        rendition("c-sds:c1", "1:1", "1200x1200"),
        rendition("c-sds:c1", "1.91:1", "1200x628", { derivation: "crop" }),
        rendition("c-sds:c1", "9:16", "not-a-size"),
      ],
      logos: [
        {
          campaign_ref: "c-sds",
          asset_type: "logo",
          ratio: "1:1",
          px: "128x128",
          registered_logo_id: "l1",
          asset_id: "logo-asset",
          media_id: "m-logo",
          scale: { sx: 1, sy: 1 },
          bytes: 9000,
          surface: "pmax_image",
          max_bytes: 5_242_880,
          lint,
        },
      ],
      gaps: [
        { campaign_ref: "c-sds", concept_id: "c-sds:c1", surface: "pmax_image", ratio: "4:5", why: "keeps 80%, under the 85% floor" },
        { campaign_ref: "c-sds", concept_id: null, surface: "search_image", ratio: "1:1", why: "no registered logo" },
      ],
    },
    CONCEPTS,
  );

  assert.deepEqual(groups.map((g) => [g.surface, g.label, g.files, g.gaps]), [
    ["search_image", "Search", 0, 1],
    ["pmax_image", "Performance Max", 4, 1],
  ]);
  const pmax = groups[1].tiles.map((t) => [t.kind, t.conceptName, t.ratio]);
  assert.deepEqual(pmax, [
    ["file", "Clipboard", "1.91:1"],
    ["file", "Clipboard", "1:1"],
    ["gap", "Clipboard", "4:5"],
    ["file", "Warehouse", "1:1"],
    ["file", null, "1:1"],
  ]);
  const logo = groups[1].tiles.at(-1);
  assert.equal(logo.derivation, "composited");
  assert.equal(logo.generated, false);
  assert.equal(groups[0].tiles[0].why, "no registered logo");
});

test("virtual rows are a header then rows of the column count", () => {
  const groups = [
    { surface: "a", label: "A", files: 5, gaps: 0, tiles: [1, 2, 3, 4, 5].map((i) => ({ key: `a${i}` })) },
    { surface: "b", label: "B", files: 1, gaps: 0, tiles: [{ key: "b1" }] },
  ];
  const rows = gridRows(groups, 2);
  assert.deepEqual(rows.map((r) => (r.kind === "header" ? `H${r.group.surface}` : r.tiles.length)), [
    "Ha", 2, 2, 1, "Hb", 1,
  ]);
  assert.equal(columnsFor(1280, 176, 12), 6);
  assert.equal(columnsFor(358, 150, 12), 2);
  assert.equal(columnsFor(100, 176, 12), 1);
});

test("an angle key names the brief line it came from", () => {
  assert.deepEqual(angleSource("angle"), { adGroupRef: null, line: "angle" });
  assert.deepEqual(angleSource("ag-sds:primary"), { adGroupRef: "ag-sds", line: "primary_message" });
  assert.deepEqual(angleSource("ag:x:angle_b"), { adGroupRef: "ag:x", line: "angle_b" });
});

test("a palette token takes the brand's colour, a hex is its own, and an unknown one none", () => {
  const palette = [{ name: "Signal teal", hex: "#0F766E" }, { name: "No colour" }];
  assert.equal(tokenColour("Signal teal", palette), "#0F766E");
  assert.equal(tokenColour("#1d4ed8", palette), "#1d4ed8");
  assert.equal(tokenColour("No colour", palette), null);
  assert.equal(tokenColour("Missing", palette), null);
});

test("the timeline places captions with their OCR sample and the end card at the end", () => {
  const markers = captionMarkers(
    [
      { t0: 0.5, t1: 2.25, text: "Every sheet" },
      { t0: 3, t1: 5, text: "On every phone" },
    ],
    [{ index: 1, t_ms: 4000, expected: "On every phone", read: "On every phone", similarity: 0.97, passed: true }],
  );
  assert.deepEqual(markers.map((m) => [m.startMs, m.endMs, m.ocr?.similarity ?? null]), [
    [500, 2250, null],
    [3000, 5000, 0.97],
  ]);
  assert.deepEqual(endCardRegion(14_000, 2000), { startMs: 12_000, endMs: 14_000 });
  assert.equal(endCardRegion(14_000, undefined), null);
  assert.equal(atPercent(5000, 14_000).toFixed(4), "35.7143");
  assert.equal(atPercent(-10, 14_000), 0);
  assert.equal(atPercent(20_000, 14_000), 100);
});
