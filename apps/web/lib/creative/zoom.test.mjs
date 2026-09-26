/**
 * The shared zoom behind ReferenceCompare (Stage 04 PRD §15.4 H). Run with `pnpm test:unit`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { clampZoom, FIT, pan, percent, transformOf, zoomIn, zoomOut } from "./zoom.ts";

test("fit is the identity and never pans", () => {
  assert.equal(transformOf(FIT), "translate(0.000%, 0.000%) scale(1)");
  assert.deepEqual(pan(FIT, 0.5, -0.5), FIT);
});

test("steps walk up and down and stop at the ends", () => {
  let zoom = FIT;
  const seen = [];
  for (let i = 0; i < 8; i += 1) {
    zoom = zoomIn(zoom);
    seen.push(percent(zoom));
  }
  assert.deepEqual(seen, ["150%", "200%", "300%", "400%", "600%", "600%", "600%", "600%"]);
  assert.equal(percent(zoomOut(zoomOut(zoom))), "300%");
  assert.equal(zoomOut(FIT).scale, 1);
});

test("the centre is clamped so a zoomed view never shows past the edge", () => {
  const zoom = clampZoom({ scale: 2, cx: 0, cy: 1 });
  assert.deepEqual(zoom, { scale: 2, cx: 0.25, cy: 0.75 });
  // At 2x the top-left quarter is on screen: translate(0, 0) scale(2).
  assert.equal(transformOf({ scale: 2, cx: 0.25, cy: 0.25 }), "translate(0.000%, 0.000%) scale(2)");
  // Centred at 2x: the middle of the image sits at the middle of the box.
  assert.equal(transformOf({ scale: 2, cx: 0.5, cy: 0.5 }), "translate(-50.000%, -50.000%) scale(2)");
});

test("pan moves by a fraction of what is on screen", () => {
  const zoom = pan({ scale: 2, cx: 0.5, cy: 0.5 }, 0.1, 0);
  assert.equal(zoom.cx, 0.55);
  assert.equal(pan({ scale: 4, cx: 0.5, cy: 0.5 }, 0.1, 0).cx, 0.525);
});
