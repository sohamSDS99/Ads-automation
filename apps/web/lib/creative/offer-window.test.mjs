/**
 * How `OfferBindingField` reads an offer's window (Stage 04 PRD §15.4 F):
 * `20% off · ends 12 Oct 2026 · 18 days`. Run with `pnpm test:unit`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { boundFigure, daysUntil, formatDate, offerEnd, remaining } from "./offer-window.ts";

const binding = (resolved) => ({ offer_record_id: "x", sku_or_set: "SDS-PRO", fields: {}, resolved });
const at = (y, m, d, h = 12) => new Date(y, m - 1, d, h);

test("the figure is the binding's own string, never reformatted", () => {
  assert.equal(boundFigure(binding({ percent_off: "20", currency: "USD" })), "20% off");
  assert.equal(boundFigure(binding({ money_off: "10.00", currency: "USD" })), "USD 10.00 off");
  assert.equal(boundFigure(binding({ price: "99.00", currency: "EUR" })), "EUR 99.00");
  assert.equal(boundFigure(binding({ currency: "USD" })), null);
});

test("the window ends at ends_at, else effective_to, else the bound end", () => {
  const offer = { ends_at: null, effective_to: "2026-10-12T00:00:00+00:00" };
  assert.equal(offerEnd(offer, binding({})), "2026-10-12T00:00:00+00:00");
  assert.equal(offerEnd({ ...offer, ends_at: "2026-10-01T00:00:00+00:00" }, binding({})), "2026-10-01T00:00:00+00:00");
  assert.equal(offerEnd(null, binding({ end: "2026-11-01T00:00:00+00:00" })), "2026-11-01T00:00:00+00:00");
  assert.equal(offerEnd({ ends_at: null, effective_to: null }, binding({})), null);
});

test("12 Oct 2026 is 18 calendar days after 24 Sep 2026, whatever the hour", () => {
  const end = at(2026, 10, 12, 1).toISOString();
  assert.equal(formatDate(at(2026, 10, 12).toISOString()), "12 Oct 2026");
  assert.equal(daysUntil(end, at(2026, 9, 24, 23)), 18);
  assert.equal(remaining(end, at(2026, 9, 24, 0)), "18 days");
});

test("the last days and a past end say so in words", () => {
  const end = at(2026, 10, 12).toISOString();
  assert.equal(remaining(end, at(2026, 10, 11)), "1 day");
  assert.equal(remaining(end, at(2026, 10, 12, 20)), "ends today");
  assert.equal(remaining(end, at(2026, 10, 13)), "ended yesterday");
  assert.equal(remaining(end, at(2026, 10, 15)), "ended 3 days ago");
});
