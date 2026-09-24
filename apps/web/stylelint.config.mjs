/**
 * Stage 04's design laws, for stylesheets (PRD §15.2 rule 2).
 *
 * The same law `eslint-rules/design-laws.mjs` enforces on class strings,
 * applied to any `.css` file under Stage 04's components or routes: colour
 * comes from `styles/tokens.css` through `var(--…)`, and nothing draws a
 * gradient, a blur, a glow or an in-flow shadow.
 *
 * Deliberately not `stylelint-config-standard`: this config exists to hold a
 * law, and a law buried under two hundred formatting opinions is one nobody
 * reads the output of. `pnpm lint` runs it over Stage 04's paths only; the
 * token file itself is where hex values are *supposed* to live.
 */
const config = {
  rules: {
    "color-no-hex": [
      true,
      {
        message:
          "A raw hex colour. Stage 04 is tokens only (PRD §15.2 rule 2): use var(--accent) or a sibling from styles/tokens.css.",
      },
    ],
    "color-named": [
      "never",
      {
        message:
          "A named colour. Stage 04 is tokens only (PRD §15.2 rule 2): use a token from styles/tokens.css.",
      },
    ],
    "function-disallowed-list": [
      [
        "linear-gradient",
        "radial-gradient",
        "conic-gradient",
        "repeating-linear-gradient",
        "repeating-radial-gradient",
        "repeating-conic-gradient",
        "rgb",
        "rgba",
        "hsl",
        "hsla",
        "hwb",
        "lab",
        "lch",
        "oklab",
        "oklch",
        "color-mix",
        "blur",
        "drop-shadow",
      ],
      {
        message: (name) =>
          `\`${name}()\` is not allowed in Stage 04: no gradients, raw colours, blur or glow (PRD §15.2 rule 2). Use a flat token surface.`,
      },
    ],
    "property-disallowed-list": [
      ["backdrop-filter", "-webkit-backdrop-filter", "filter", "box-shadow", "text-shadow"],
      {
        message: (property) =>
          `\`${property}\` draws glass, blur, glow or an in-flow shadow, which Stage 04 does not (PRD §15.2 rule 2). Overlays get their shadow from the shared dialog and popover primitives.`,
      },
    ],
  },
};

export default config;
