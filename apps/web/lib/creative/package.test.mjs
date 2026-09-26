/**
 * The QA and Package screens' display helpers (Stage 04 PRD §15.4 K, L). Run with `pnpm test:unit`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  adRefParts,
  confirmsVersion,
  costDelta,
  elementLabel,
  formatBytes,
  groupPreviews,
  manifestTree,
  markIn,
  middle,
  rolesLabel,
  truncationText,
  truncations,
} from "./package.ts";

const preview = (overrides) => ({
  id: overrides.id,
  creative_run_id: "run",
  ad_ref: overrides.ad_ref ?? "c-sds-us/sds software/A",
  device: overrides.device,
  combination: { roles: overrides.roles ?? ["likely_1"], likelihood: overrides.likelihood ?? 0.2 },
  has_screenshot: true,
  dom_metrics: {},
  spec_diff: {},
  visual_diff: null,
  template_version: "serp-1",
  verdict: "pass",
  created_at: "2026-09-26T00:00:00Z",
});

test("element keys read as words", () => {
  assert.equal(elementLabel("headline_2"), "Headline 2");
  assert.equal(elementLabel("description_1"), "Description 1");
  assert.equal(elementLabel("path_2"), "Path 2");
  assert.equal(elementLabel("display_url"), "Display URL");
  assert.equal(elementLabel("business_name"), "business name");
});

test("roles keep 4.6.4's order and say both when one render has two", () => {
  assert.equal(rolesLabel(["likely_1"]), "Likely 1");
  assert.equal(rolesLabel(["likely_2", "longest"]), "Likely 2 · longest");
  assert.equal(rolesLabel(["longest"]), "Longest");
  assert.equal(rolesLabel([]), "Combination");
});

test("an ad ref splits into campaign, ad group and variant, the group keeping its slashes", () => {
  assert.deepEqual(adRefParts("c-sds-us/sds software/B"), { campaign: "c-sds-us", adGroup: "sds software", variant: "B" });
  assert.deepEqual(adRefParts("c/a/b/A"), { campaign: "c", adGroup: "a/b", variant: "A" });
  assert.deepEqual(adRefParts("odd"), { campaign: "odd", adGroup: "", variant: "" });
});

test("previews group by RSA then combination, pairing the two devices in the server's order", () => {
  const items = [
    preview({ id: "1", device: "desktop", roles: ["likely_1"] }),
    preview({ id: "2", device: "mobile", roles: ["likely_1"] }),
    preview({ id: "3", device: "mobile", roles: ["likely_2", "longest"], likelihood: 0.1 }),
    preview({ id: "4", ad_ref: "c-sds-us/sds software/B", device: "mobile" }),
  ];
  const groups = groupPreviews(items);
  assert.deepEqual(groups.map((g) => g.adRef), ["c-sds-us/sds software/A", "c-sds-us/sds software/B"]);
  const [first, second] = groups[0].pairs;
  assert.equal(first.mobile.id, "2");
  assert.equal(first.desktop.id, "1");
  assert.deepEqual(second.roles, ["likely_2", "longest"]);
  assert.equal(second.desktop, null);
  assert.equal(second.likelihood, 0.1);
});

test("truncations are the elements 4.6.4 measured as overflowing or clipped, with their boxes", () => {
  const box = { x: 10, y: 20, width: 100, height: 18 };
  const found = truncations({
    elements: [
      { key: "headline_1", asset_id: "a", scroll_width: 250, client_width: 236, overflow_px: 14, clipped: false, box },
      { key: "headline_2", asset_id: "b", scroll_width: 100, client_width: 100, overflow_px: 0, clipped: false, box },
      { key: "headline_3", asset_id: "c", scroll_width: 90, client_width: 90, overflow_px: 0, clipped: true },
    ],
  });
  assert.deepEqual(
    found.map((t) => [t.key, t.overflowPx, t.clipped, t.box]),
    [["headline_1", 14, false, box], ["headline_3", 0, true, null]],
  );
  assert.equal(truncationText(found[0]), "14 px past its slot");
  assert.equal(truncationText(found[1]), "clipped by the layout");
  assert.equal(truncationText({ overflowPx: 3, clipped: true }), "3 px past its slot and clipped");
});

test("a preview measured before boxes still names its truncations from truncated[]", () => {
  const found = truncations({
    truncated: ["headline_1", "description_2"],
    overflow_px: [{ element: "headline_1", asset_id: "a", px: 9 }],
  });
  assert.deepEqual(found.map((t) => [t.key, t.overflowPx, t.clipped, t.box]), [
    ["headline_1", 9, false, null],
    ["description_2", 0, true, null],
  ]);
});

test("a mark moves into the cropped frame, and one outside it is dropped", () => {
  const frame = { x: 180, y: 40, width: 600, height: 160 };
  assert.deepEqual(markIn({ x: 200, y: 60, width: 100, height: 20 }, frame), { x: 20, y: 20, width: 100, height: 20 });
  assert.equal(markIn({ x: 0, y: 0, width: 100, height: 20 }, frame), null);
  assert.deepEqual(markIn({ x: 5, y: 5, width: 1, height: 1 }, null), { x: 5, y: 5, width: 1, height: 1 });
});

test("hashes are cut in the middle; sizes are binary units", () => {
  assert.equal(middle("a41f0000000000009c2e"), "a41f…9c2e");
  assert.equal(middle("short"), "short");
  assert.equal(formatBytes(812), "812 B");
  assert.equal(formatBytes(12_697), "12.4 KiB");
  assert.equal(formatBytes(3_250_586), "3.1 MiB");
  assert.equal(formatBytes(157_286_400), "150 MiB");
});

test("the manifest is a tree: folders first, each carrying its files' bytes and count", () => {
  const entry = (path, bytes) => ({ path, bytes, sha256: "0".repeat(64), media_type: "application/json" });
  const tree = manifestTree([
    entry("landing/b/patch.json", 30),
    entry("package.json", 5),
    entry("landing/a/patch.html", 20),
    entry("landing/a/patch.json", 10),
  ]);
  assert.equal(tree.bytes, 65);
  assert.equal(tree.files, 4);
  assert.deepEqual(tree.children.map((c) => c.name), ["landing", "package.json"]);
  const landing = tree.children[0];
  assert.equal(landing.kind, "dir");
  assert.equal(landing.bytes, 60);
  assert.equal(landing.files, 3);
  assert.deepEqual(landing.children.map((c) => [c.name, c.path]), [["a", "landing/a"], ["b", "landing/b"]]);
  assert.deepEqual(landing.children[0].children.map((c) => c.name), ["patch.html", "patch.json"]);
});

test("the release confirmation is the version, exactly", () => {
  assert.equal(confirmsVersion("v3", 3), true);
  assert.equal(confirmsVersion(" v3 ", 3), true);
  assert.equal(confirmsVersion("V3", 3), false);
  assert.equal(confirmsVersion("3", 3), false);
  assert.equal(confirmsVersion("v2", 3), false);
  assert.equal(confirmsVersion("v33", 3), false);
});

test("cost delta is actual against estimate, with no percentage on a zero estimate", () => {
  const delta = costDelta("2.00", "2.40");
  assert.ok(Math.abs(delta.usd - 0.4) < 1e-9);
  assert.ok(Math.abs(delta.pct - 20) < 1e-9);
  assert.deepEqual(costDelta("0", "0"), { usd: 0, pct: null });
});
