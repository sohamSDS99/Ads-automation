/**
 * `wordDiff` — the Landing audit's ad-headline-vs-H1 reading aid (Stage 04 PRD
 * §15.4 J). Run with `pnpm test:unit`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { wordDiff } from "./word-diff.ts";

const kinds = (tokens) => tokens.map((t) => `${t.text}:${t.kind}`);

test("identical lines share every word", () => {
  const { a, b } = wordDiff("SDS software for EHS teams", "SDS software for EHS teams");
  assert.ok(a.every((t) => t.kind === "shared") && b.every((t) => t.kind === "shared"));
});

test("case and punctuation do not make a word different", () => {
  const { a, b } = wordDiff("SDS Software, built for teams", "sds software built for Teams!");
  assert.deepEqual(kinds(a), ["SDS:shared", "Software,:shared", "built:shared", "for:shared", "teams:shared"]);
  assert.ok(b.every((t) => t.kind === "shared"));
});

test("only the words one side has are marked, in order", () => {
  const { a, b } = wordDiff("SDS Management Software", "Welcome to our SDS company");
  assert.deepEqual(kinds(a), ["SDS:shared", "Management:only", "Software:only"]);
  assert.deepEqual(kinds(b), ["Welcome:only", "to:only", "our:only", "SDS:shared", "company:only"]);
});

test("a repeated word is shared once per match, never twice", () => {
  const { a, b } = wordDiff("safety data safety", "safety sheets");
  assert.deepEqual(kinds(a), ["safety:shared", "data:only", "safety:only"]);
  assert.deepEqual(kinds(b), ["safety:shared", "sheets:only"]);
});

test("a token with no letter or digit takes no side", () => {
  const { a } = wordDiff("SDS | Software — now", "SDS now");
  assert.deepEqual(kinds(a), ["SDS:shared", "|:neutral", "Software:only", "—:neutral", "now:shared"]);
});

test("an empty H1 leaves every ad word unshared", () => {
  const { a, b } = wordDiff("Keep every SDS current", "");
  assert.ok(a.every((t) => t.kind === "only"));
  assert.deepEqual(b, []);
});

test("non-Latin scripts compare by letters too", () => {
  const { a } = wordDiff("Sicherheitsdatenblätter verwalten", "Sicherheitsdatenblätter heute verwalten.");
  assert.ok(a.every((t) => t.kind === "shared"));
});
