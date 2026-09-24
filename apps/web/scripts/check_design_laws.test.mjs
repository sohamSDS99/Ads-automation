/**
 * `check_design_laws.ts`, held to the copy it exists to reject and the code it
 * must leave alone. Run with `pnpm test:design-laws`.
 */
import assert from "node:assert/strict";
import { test } from "node:test";

import { checkAsset, checkSource } from "./check_design_laws.ts";

const rules = (source, file = "x.tsx") => checkSource(file, source).map((f) => f.rule);

test("a planted Sparkles import fails, however it is spelled", () => {
  assert.deepEqual(rules(`import { Sparkles } from "lucide-react";`), ["no-ai-iconography"]);
  assert.deepEqual(rules(`import { Bot as Helper } from "lucide-react";`), ["no-ai-iconography"]);
  assert.deepEqual(rules(`import { LucideWand2 } from "lucide-react";`), ["no-ai-iconography"]);
  assert.deepEqual(rules(`import S from "lucide-react/dist/esm/icons/sparkles";`), ["no-ai-iconography"]);
});

test("banned words, emoji and exclamation marks in copy fail", () => {
  assert.deepEqual(rules(`const a = <p>Generate magic headlines</p>;`), ["no-hype-copy"]);
  assert.deepEqual(rules(`const a = <Button aria-label="AI-powered images" />;`), ["no-hype-copy"]);
  assert.deepEqual(rules("const t = `Supercharge ${n} ads`;"), ["no-hype-copy"]);
  assert.deepEqual(rules(`const a = <p>Done 🚀</p>;`), ["no-emoji"]);
  assert.deepEqual(rules(`const a = <p>Saved!</p>;`), ["no-exclamation"]);
  assert.deepEqual(rules(`toast("Allowlist saved!");`), ["no-exclamation"]);
});

test("code that merely contains the characters passes", () => {
  assert.deepEqual(rules(`const ok = a !== b && !c;`), []);
  assert.deepEqual(rules(`const a = <div className="!mt-0 md:!p-2" />;`), []);
  assert.deepEqual(rules(`const a = <div className={cn("!mt-0", on && "mt-2!")} />;`), []);
  assert.deepEqual(rules(`import { Bot } from "./bot-detection";`), []);
  assert.deepEqual(rules(`type Kind = "magic_link";`), []);
  assert.deepEqual(rules(`const map = { "unleash": 1 };`), []);
  assert.deepEqual(rules(`const a = <p>© 2026 Acme™ · 27/30 · ≈ $2.40 — ends 12 Oct</p>;`), []);
  assert.deepEqual(rules(`const a = <p>Image magnification</p>;`), []);
});

test("stylesheets and SVG fail on rule 2", () => {
  const found = (source, file) => checkAsset(file, source).map((f) => f.rule);
  assert.deepEqual(found(".a { color: #2563eb; }", "a.css"), ["tokens-only"]);
  assert.deepEqual(found(".a { background: linear-gradient(red, blue); }", "a.css"), ["no-gradient"]);
  assert.deepEqual(found(".a { backdrop-filter: blur(4px); }", "a.css"), ["no-blur-or-glow"]);
  assert.deepEqual(found('<linearGradient id="g" />', "a.svg"), ["no-gradient"]);
  assert.deepEqual(found(".a { color: var(--accent); }", "a.css"), []);
});
