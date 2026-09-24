/**
 * The design-law rules, held to the cases they exist for.
 *
 * Run with `pnpm test:design-laws`. Every `invalid` case below was written
 * before the rule that rejects it, and the three PRD §21.3 names — a gradient,
 * a raw hex, a `Sparkles` import — are the first three.
 */
import { RuleTester } from "eslint";

import plugin from "./design-laws.mjs";

const tester = new RuleTester({
  languageOptions: {
    ecmaVersion: 2022,
    sourceType: "module",
    parserOptions: { ecmaFeatures: { jsx: true } },
  },
});

const tokens = plugin.rules["tokens-only"];
const icons = plugin.rules["no-ai-iconography"];

tester.run("tokens-only", tokens, {
  valid: [
    `const a = <div className="bg-surface text-fg-muted rounded-token border p-4" />;`,
    `const a = <div className="md:hover:bg-surface-hover focus-visible:outline-accent" />;`,
    `const a = <div className={cn("text-status-failed", ok && "bg-accent-soft")} />;`,
    `const a = <div className="shadow-overlay" />;`,
    `const a = <div className="shadow-none" />;`,
    // Words that only look like utilities outside a class list.
    `const copy = "Allow to-do models from the catalogue";`,
    // A fragment is a path, not a colour.
    `const a = <a href="/settings/models#media">Media</a>;`,
    // Import paths are never colours.
    `import x from "./#abc";`,
    // Code inside a callback in cn() is code, not classes: this string is
    // not a shadow utility, it is a label.
    `const a = cn("p-2", items.map(() => "shadow-lg"));`,
    `const a = <div style={{ width: "100%" }} />;`,
  ],
  invalid: [
    // §21.3: a planted gradient.
    { code: `const a = <div className="bg-gradient-to-r from-accent to-surface" />;`, errors: 3 },
    { code: `const a = <div className="bg-linear-to-r" />;`, errors: 1 },
    { code: `const a = <div style={{ background: "linear-gradient(90deg, red, blue)" }} />;`, errors: 1 },
    { code: `const a = <div style={{ backgroundImage: "url(x.png)" }} />;`, errors: 1 },
    // §21.3: a planted raw hex.
    { code: `const a = <div className="text-[#2563eb]" />;`, errors: 1 },
    { code: `const a = <svg><path fill="#2563eb" /></svg>;`, errors: 1 },
    { code: `const a = <div style={{ color: "#fff" }} />;`, errors: 1 },
    { code: `const colour = "rgb(37 99 235)";`, errors: 1 },
    // Arbitrary values of every kind.
    { code: `const a = <div className="w-[13px]" />;`, errors: 1 },
    { code: `const a = <div className="rounded-[var(--radius)]" />;`, errors: 1 },
    { code: `const a = <div className="bg-(--accent)" />;`, errors: 1 },
    { code: `const a = <div className="[&>p]:mt-2" />;`, errors: 1 },
    { code: "const a = <div className={`p-2 ${x} text-[13px]`} />;", errors: 1 },
    { code: `const v = cva("p-2", { variants: { tone: { hot: "bg-rose-500" } } });`, errors: 1 },
    // Glass, blur, glow and shadow.
    { code: `const a = <div className="backdrop-blur-md" />;`, errors: 1 },
    { code: `const a = <div className="blur-sm" />;`, errors: 1 },
    { code: `const a = <div className="drop-shadow-lg" />;`, errors: 1 },
    { code: `const a = <div className="shadow-lg" />;`, errors: 1 },
    { code: `const a = <div className="md:shadow" />;`, errors: 1 },
    { code: `const a = <div style={{ boxShadow: "0 0 8px var(--accent)" }} />;`, errors: 1 },
    { code: `const a = <div style={{ backdropFilter: "none" }} />;`, errors: 1 },
    // Raw palette colours: not tokens.
    { code: `const a = <div className="text-blue-600 bg-white" />;`, errors: 2 },
    { code: `const a = <div className={clsx({ "border-zinc-200": on })} />;`, errors: 1 },
  ],
});

tester.run("no-ai-iconography", icons, {
  valid: [
    `import { Lock, Palette, TriangleAlert } from "lucide-react";`,
    // Only lucide's glyphs are banned by name; a local component called Bot is
    // somebody else's problem, and not a glyph.
    `import { Bot } from "./bot-detection";`,
    `import { Botanical } from "lucide-react";`,
  ],
  invalid: [
    // §21.3: a planted Sparkles import.
    { code: `import { Sparkles } from "lucide-react";`, errors: 1 },
    { code: `import { SparklesIcon, LucideWand2 } from "lucide-react";`, errors: 2 },
    { code: `import { Bot as Helper } from "lucide-react";`, errors: 1 },
    { code: `import { BrainCircuit, Stars, WandSparkles, Sparkle } from "lucide-react";`, errors: 4 },
    { code: `import Sparkles from "lucide-react/dist/esm/icons/sparkles";`, errors: 1 },
    { code: `import * as Icons from "lucide-react"; const a = Icons.Wand2;`, errors: 1 },
  ],
});

console.log("design-laws: all RuleTester cases pass");
