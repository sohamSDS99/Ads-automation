import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { FlatCompat } from "@eslint/eslintrc";

import designLaws from "./eslint-rules/design-laws.mjs";

const compat = new FlatCompat({ baseDirectory: dirname(fileURLToPath(import.meta.url)) });

/**
 * Where Stage 04's design laws apply (PRD §15.2). The components, and the
 * routes that compose them — a page file is a screen as much as a component
 * is. `[id]` is escaped because to a glob it is a character class.
 */
export const STAGE_04_FILES = [
  "components/creative/**/*.{ts,tsx,js,jsx,mjs}",
  "app/\\(app\\)/projects/\\[id\\]/creative/**/*.{ts,tsx}",
];

const config = [
  ...compat.extends("next/core-web-vitals", "next/typescript"),
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts"] },
  {
    files: STAGE_04_FILES,
    plugins: { "design-laws": designLaws },
    rules: {
      "design-laws/tokens-only": "error",
      "design-laws/no-ai-iconography": "error",
    },
  },
];

export default config;
