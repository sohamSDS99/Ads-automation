/**
 * Stage 04's design laws, as ESLint rules (Stage 04 PRD §15.2 rules 2–3).
 *
 * §15.2 is written to be binary — "a screen that breaks one does not merge" —
 * and a law nobody can run is a law that is kept only while somebody
 * remembers it. These two rules are the part of it a parser can see:
 *
 * - `tokens-only` (rule 2): no raw colour, no arbitrary Tailwind value, no
 *   gradient, no blur or glass, no glow or drop shadow. Checked on the class
 *   strings (`className`, and the arguments of `cn`/`clsx`/`cva`/`twMerge`),
 *   on `style` objects, and on every other string for the three things that
 *   are never anything but colour: a hex literal, a colour function, a CSS
 *   gradient.
 * - `no-ai-iconography` (rule 3, icons): the lucide glyphs that say "a model
 *   did this" instead of saying what was done.
 *
 * The copy half of rule 3 — emoji, the banned words, exclamation marks — is
 * `scripts/check_design_laws.ts`, because it is a question about the text a
 * person reads, and that script reads the text.
 *
 * Scoped to Stage 04 by `eslint.config.mjs`. The shared primitives in
 * `components/ui/` predate the law and are not rewritten by it.
 */

/** Lucide's glyphs for "AI", by their base name (§15.2 rule 3, §22 UI laws). */
export const BANNED_ICONS = new Set([
  "Sparkle",
  "Sparkles",
  "Wand",
  "Wand2",
  "WandSparkles",
  "Bot",
  "BrainCircuit",
  "Stars",
]);

/** The same glyphs by the file name lucide ships each one under. */
const BANNED_ICON_FILES = new Set([
  "sparkle",
  "sparkles",
  "wand",
  "wand-2",
  "wand-sparkles",
  "bot",
  "brain-circuit",
  "stars",
]);

/** Lucide exports each icon three ways: `Sparkles`, `SparklesIcon`, `LucideSparkles`. */
export function iconBaseName(name) {
  return name.replace(/^Lucide/, "").replace(/Icon$/, "");
}

function isLucide(source) {
  return source === "lucide-react" || source.startsWith("lucide-react/");
}

/** The functions whose string arguments are class lists. */
const CLASS_CALLEES = new Set(["cn", "clsx", "cx", "cva", "twMerge", "tv"]);

const PALETTE =
  "slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|black|white";

const COLOUR_UTILITY =
  "bg|text|border(?:-[xytrblse])?|ring|ring-offset|outline|fill|stroke|divide|placeholder|caret|accent|decoration|shadow|inset-shadow|from|via|to";

const RAW_PALETTE = new RegExp(`^(?:${COLOUR_UTILITY})-(?:${PALETTE})(?:-\\d{2,3})?(?:/\\d+)?$`);

/**
 * The utility with its variants and modifiers removed: `md:hover:!-mt-2` is
 * `mt-2`. Variants are split on the last `:` that is not inside brackets, so an
 * arbitrary variant like `[&>p]:mt-2` still reaches the bracket check below.
 */
export function utilityOf(token) {
  let depth = 0;
  let cut = -1;
  for (let i = 0; i < token.length; i += 1) {
    const ch = token[i];
    if (ch === "[" || ch === "(") depth += 1;
    else if (ch === "]" || ch === ")") depth -= 1;
    else if (ch === ":" && depth === 0) cut = i;
  }
  return token
    .slice(cut + 1)
    .replace(/^!/, "")
    .replace(/!$/, "")
    .replace(/^-/, "");
}

/**
 * Why one class token breaks rule 2, or `null` when it does not.
 *
 * Each message names the cause and the fix (§15.2 rule 9 applies to the
 * people reading lint output as much as to anyone else).
 */
export function classViolation(token) {
  if (/[[\]]/.test(token) || /-\(--/.test(token)) {
    return `\`${token}\` is an arbitrary value. Stage 04 is tokens only (PRD §15.2 rule 2): use a token utility such as \`text-fg-muted\`, \`bg-surface\` or \`rounded-token\`, or add the token to styles/tokens.css first.`;
  }
  const utility = utilityOf(token);
  if (/^(?:bg-(?:gradient|linear|radial|conic)(?:-|$)|from-|via-|to-)/.test(utility)) {
    return `\`${token}\` draws a gradient. Stage 04 has no gradients (PRD §15.2 rule 2): use a flat token surface such as \`bg-surface\`.`;
  }
  if (/^(?:backdrop-|blur(?:-|$)|drop-shadow(?:-|$))/.test(utility)) {
    return `\`${token}\` is blur or glass. Stage 04 has no blur, glassmorphism or glow (PRD §15.2 rule 2): use an opaque token surface.`;
  }
  if (/^(?:inset-shadow|text-shadow)(?:-|$)/.test(utility)) {
    return `\`${token}\` is a shadow on in-flow content. Stage 04 draws no glow and no in-flow shadow (PRD §15.2 rule 2).`;
  }
  if (/^shadow(?:-|$)/.test(utility) && utility !== "shadow-overlay" && utility !== "shadow-none") {
    return `\`${token}\` is a drop shadow. Shadow is for overlays only (PRD §15.2 rule 2): use \`shadow-overlay\` on a dialog, popover or drawer, and a border elsewhere.`;
  }
  if (RAW_PALETTE.test(utility)) {
    return `\`${token}\` is a raw palette colour. Stage 04 is tokens only (PRD §15.2 rule 2): use a token such as \`text-fg\`, \`bg-accent-soft\` or \`text-status-failed\`.`;
  }
  return null;
}

const HEX = /(?:^|[\s:,(=])#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})(?![0-9a-z_-])/i;
const COLOUR_FUNCTION = /\b(?:rgba?|hsla?|hwb|lab|lch|oklab|oklch|color-mix)\(/i;
const CSS_GRADIENT = /\b(?:repeating-)?(?:linear|radial|conic)-gradient\(/i;
const CSS_BLUR = /\bblur\(/i;

/** Why a string that is not a class list breaks rule 2, or `null`. */
export function valueViolation(text) {
  if (HEX.test(text)) {
    return "A raw hex colour. Stage 04 is tokens only (PRD §15.2 rule 2): use a token utility, or `var(--accent)` and its siblings from styles/tokens.css.";
  }
  if (CSS_GRADIENT.test(text)) {
    return "A CSS gradient. Stage 04 has no gradients (PRD §15.2 rule 2): use a flat token surface.";
  }
  if (COLOUR_FUNCTION.test(text)) {
    return "A raw colour function. Stage 04 is tokens only (PRD §15.2 rule 2): use a token from styles/tokens.css.";
  }
  if (CSS_BLUR.test(text)) {
    return "A blur filter. Stage 04 has no blur, glassmorphism or glow (PRD §15.2 rule 2).";
  }
  return null;
}

/** `style` properties that exist only to do what rule 2 forbids. */
const BANNED_STYLE_PROPERTIES = new Map([
  ["backdropFilter", "glass (backdrop blur)"],
  ["WebkitBackdropFilter", "glass (backdrop blur)"],
  ["filter", "a filter (blur or glow)"],
  ["boxShadow", "a drop shadow or glow"],
  ["textShadow", "a text shadow or glow"],
  ["backgroundImage", "a background image (the way gradients are drawn)"],
]);

function isClassAttribute(node) {
  return (
    node?.type === "JSXAttribute" &&
    node.name?.type === "JSXIdentifier" &&
    (node.name.name === "className" || node.name.name === "class")
  );
}

function isClassCall(node) {
  return (
    node?.type === "CallExpression" &&
    node.callee?.type === "Identifier" &&
    CLASS_CALLEES.has(node.callee.name)
  );
}

/** True when this string literal is (part of) a class list. */
function inClassContext(node) {
  for (let current = node.parent; current; current = current.parent) {
    if (isClassAttribute(current) || isClassCall(current)) return true;
    // A class list never crosses a function boundary: a callback inside `cn()`
    // is code, and the strings in it are not classes.
    if (/Function/.test(current.type)) return false;
  }
  return false;
}

function stringValue(node) {
  if (node.type === "Literal" && typeof node.value === "string") return node.value;
  if (node.type === "TemplateElement") return node.value.cooked ?? node.value.raw;
  return null;
}

const tokensOnly = {
  meta: {
    type: "problem",
    docs: { description: "Stage 04 is tokens only: no raw colour, arbitrary value, gradient, blur or glow" },
    schema: [],
  },
  create(context) {
    function check(node) {
      const text = stringValue(node);
      if (text === null || text.length === 0) return;
      // A module specifier is a path, never a colour.
      if (node.parent?.type === "ImportDeclaration" || node.parent?.type === "ExportNamedDeclaration") {
        return;
      }
      if (inClassContext(node)) {
        for (const token of text.split(/\s+/).filter(Boolean)) {
          const message = classViolation(token);
          if (message) context.report({ node, message });
        }
        return;
      }
      const message = valueViolation(text);
      if (message) context.report({ node, message });
    }

    return {
      Literal: check,
      TemplateElement: check,
      "JSXAttribute[name.name='style'] Property"(node) {
        const key =
          node.key?.type === "Identifier"
            ? node.key.name
            : node.key?.type === "Literal"
              ? String(node.key.value)
              : null;
        const what = key ? BANNED_STYLE_PROPERTIES.get(key) : undefined;
        if (what) {
          context.report({
            node,
            message: `\`style.${key}\` is ${what}. Stage 04 has no gradients, blur, glow or in-flow shadow (PRD §15.2 rule 2).`,
          });
        }
      },
    };
  },
};

const noAiIconography = {
  meta: {
    type: "problem",
    docs: { description: "No Sparkles, Wand, Bot, BrainCircuit or Stars icons in Stage 04" },
    schema: [],
  },
  create(context) {
    const namespaces = new Set();
    const message = (name) =>
      `\`${name}\` is AI iconography. Stage 04 labels generated content plainly (PRD §15.2 rule 3): show an \`AI-generated\` chip with the model name, or an icon for what the control does.`;

    return {
      ImportDeclaration(node) {
        const source = String(node.source.value);
        if (!isLucide(source)) return;
        const file = source.split("/").pop() ?? "";
        if (source !== "lucide-react" && BANNED_ICON_FILES.has(file.replace(/\.js$/, ""))) {
          context.report({ node, message: message(file) });
        }
        for (const specifier of node.specifiers) {
          if (specifier.type === "ImportNamespaceSpecifier") {
            namespaces.add(specifier.local.name);
          } else if (specifier.type === "ImportSpecifier") {
            const imported =
              specifier.imported.type === "Identifier"
                ? specifier.imported.name
                : String(specifier.imported.value);
            if (BANNED_ICONS.has(iconBaseName(imported))) {
              context.report({ node: specifier, message: message(imported) });
            }
          }
        }
      },
      MemberExpression(node) {
        if (
          node.object.type === "Identifier" &&
          namespaces.has(node.object.name) &&
          node.property.type === "Identifier" &&
          BANNED_ICONS.has(iconBaseName(node.property.name))
        ) {
          context.report({ node, message: message(node.property.name) });
        }
      },
    };
  },
};

const plugin = {
  meta: { name: "design-laws" },
  rules: {
    "tokens-only": tokensOnly,
    "no-ai-iconography": noAiIconography,
  },
};

export default plugin;
