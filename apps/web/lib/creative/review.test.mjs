/**
 * The G8 / G8b review state (Stage 04 PRD §15.4 H). Run with `pnpm test:unit`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  canApprove,
  decide,
  emptyChecklist,
  initialCursor,
  initialState,
  nextUndecided,
  recordedState,
  sumUsd,
  tally,
  tallyLine,
  toDraft,
  toggleCheck,
  toSubmission,
} from "./review.ts";

const item = (id, extra = {}) => ({
  asset_id: id,
  kind: "image",
  campaign_ref: "PMax - SDS",
  concept_id: "c1",
  regenerated_from: null,
  renditions: [],
  disclosure: {},
  product_refs: [],
  vision_advisory: { notes: [], flags: [] },
  decision: null,
  ...extra,
});
const items = ["a", "b", "c"].map((id) => item(id));
const order = items.map((it) => it.asset_id);
const all = { label_ok: true, product_match_ok: true, subjects_ok: true, rights_ok: true };

test("approve is refused until all four checks are ticked, one explicit tick at a time", () => {
  let state = { decision: null, checklist: emptyChecklist(), note: null };
  for (const key of ["label_ok", "product_match_ok", "subjects_ok"]) {
    state = toggleCheck(state, key).next;
    assert.equal(canApprove(state), false);
    assert.deepEqual(decide(state, "approve", "G8"), { refused: "needs_ticks" });
  }
  state = toggleCheck(state, "rights_ok").next;
  assert.equal(canApprove(state), true);
  assert.equal(decide(state, "approve", "G8").next.decision, "approve");
});

test("unticking an approved asset withdraws the approval and says so", () => {
  const approved = { decision: "approve", checklist: { ...all }, note: null };
  const { next, withdrew } = toggleCheck(approved, "rights_ok");
  assert.equal(withdrew, true);
  assert.equal(next.decision, null);
  const rejected = { decision: "reject", checklist: { ...all }, note: null };
  assert.equal(toggleCheck(rejected, "rights_ok").withdrew, false);
  assert.equal(toggleCheck(rejected, "rights_ok").next.decision, "reject");
});

test("regenerate needs a note and is not offered at G8b", () => {
  const state = { decision: null, checklist: emptyChecklist(), note: null };
  assert.deepEqual(decide(state, "regenerate", "G8", "   "), { refused: "needs_note" });
  assert.deepEqual(decide(state, "regenerate", "G8b", "warmer light"), { refused: "not_offered" });
  const made = decide(state, "regenerate", "G8", "  warmer light ");
  assert.equal(made.next.decision, "regenerate");
  assert.equal(made.next.note, "warmer light");
  // Reject needs no tick and no note.
  assert.equal(decide(state, "reject", "G8b").next.decision, "reject");
});

test("a draft restores where the reviewer left off, but never a decision Approve could not make", () => {
  const draft = {
    cursor: "b",
    items: [
      { asset_id: "a", decision: "approve", checklist: { ...all }, note: null },
      { asset_id: "b", decision: "approve", checklist: { ...all, rights_ok: false }, note: null },
      { asset_id: "c", decision: "regenerate", checklist: { label_ok: 1 }, note: "brighter" },
      { asset_id: "gone", decision: "reject", checklist: {}, note: null },
    ],
  };
  const g8 = initialState(items, draft, "G8");
  assert.equal(g8.a.decision, "approve");
  assert.equal(g8.b.decision, null, "an approve without four ticks comes back undecided");
  assert.equal(g8.b.checklist.label_ok, true);
  assert.equal(g8.c.decision, "regenerate");
  assert.equal(g8.c.checklist.label_ok, false, "1 is not a tick — only JSON true is");
  assert.equal("gone" in g8, false);
  assert.equal(initialState(items, draft, "G8b").c.decision, null, "no regeneration at G8b");
  assert.equal(initialCursor(items, draft), 1);
  assert.equal(initialCursor(items, { cursor: "gone" }), 0);
  assert.equal(initialState(items, null, "G8").a.decision, null);
});

test("tally, next undecided with wrap, and the submission", () => {
  const state = initialState(items, null, "G8");
  const decided = {
    ...state,
    a: { decision: "approve", checklist: { ...all }, note: null },
    c: { decision: "regenerate", checklist: emptyChecklist(), note: "less clutter" },
  };
  assert.deepEqual(tally(decided, order), { approve: 1, reject: 0, regenerate: 1, undecided: 1 });
  assert.equal(nextUndecided(decided, order, 2), 1, "wraps past the end");
  assert.deepEqual(toSubmission(decided, order), { undecided: ["b"] });
  const done = { ...decided, b: { decision: "reject", checklist: emptyChecklist(), note: null } };
  assert.equal(nextUndecided(done, order, 0), null);
  const sent = toSubmission(done, order).items;
  assert.deepEqual(
    sent.map((it) => [it.asset_id, it.decision, it.note]),
    [
      ["a", "approve", undefined],
      ["b", "reject", undefined],
      ["c", "regenerate", "less clutter"],
    ],
  );
  assert.deepEqual(sent[0].checklist, all);
});

test("the autosave sends only what somebody touched, with the cursor", () => {
  const state = initialState(items, null, "G8");
  const touched = { ...state, b: toggleCheck(state.b, "label_ok").next };
  assert.deepEqual(toDraft(touched, order, "b"), {
    items: [{ asset_id: "b", decision: null, checklist: { ...emptyChecklist(), label_ok: true }, note: null }],
    cursor: "b",
  });
});

test("the submit line reads exactly as the PRD writes it", () => {
  const counts = { approve: 14, reject: 2, regenerate: 3, undecided: 0 };
  assert.equal(tallyLine(counts, "G8", 1.8), "Approve 14 · Reject 2 · Regenerate 3 (≈ $1.80)");
  assert.equal(tallyLine(counts, "G8", null), "Approve 14 · Reject 2 · Regenerate 3 (pricing…)");
  assert.equal(tallyLine({ ...counts, regenerate: 0 }, "G8", 0), "Approve 14 · Reject 2 · Regenerate 0");
  assert.equal(tallyLine(counts, "G8b", 1.8), "Approve 14 · Reject 2");
  assert.equal(tallyLine(counts, "G8"), "Approve 14 · Reject 2 · Regenerate 3", "a recorded card carries no price");
  assert.equal(sumUsd(["0.6000", "0.6000", "0.6000"]), 1.8);
  assert.equal(sumUsd(["0.1", "0.2"]), 0.3);
});

test("a recorded card reads back its decisions", () => {
  const recorded = recordedState([
    item("a", { decision: { decision: "reject", checklist: { ...all }, note: null } }),
    item("b"),
  ]);
  assert.equal(recorded.a.decision, "reject");
  assert.equal(recorded.b.decision, null);
});
