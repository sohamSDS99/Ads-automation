/**
 * The copy half of Stage 04's design laws (PRD §15.2 rule 3), as a CI grep.
 *
 *     node scripts/check_design_laws.ts        # part of `pnpm lint`
 *
 * §15.2 rule 3 bans three things in what a person reads — emoji, the words
 * "magic", "AI-powered", "effortless", "unleash" and "supercharge", and
 * exclamation marks — and the lucide glyphs that stand for "a model did this".
 * ESLint's `design-laws/no-ai-iconography` catches the glyphs at import; this
 * catches them however they are spelled, and it is the only check that reads
 * the copy.
 *
 * It is a grep that knows what a string is. A bare regex over the source would
 * flag every `!==` and every Tailwind `!important`; this parses each file with
 * the TypeScript compiler and looks only at text a person can read: JSX text,
 * and string and template literals that are not a class list, a module path or
 * a property key. For stylesheets and SVG, where there is no copy, it checks
 * the rule 2 half instead — stylelint holds CSS, and nothing else holds SVG.
 */
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

import ts from "typescript";

/** Where the laws apply, relative to `apps/web`. */
export const STAGE_04_ROOTS = ["components/creative", "app/(app)/projects/[id]/creative"];

export type Finding = { file: string; line: number; column: number; rule: string; message: string };

const BANNED_ICON = /^(?:Lucide)?(?:Sparkles?|Wand2?|WandSparkles|Bot|BrainCircuit|Stars)(?:Icon)?$/;
const BANNED_ICON_PATH = /lucide-react\/.*\/(?:sparkles?|wand(?:-2|-sparkles)?|bot|brain-circuit|stars)(?:\.js)?$/;

const BANNED_WORDS =
  /\b(?:magic(?:al|ally)?|ai[\s‐‑-]?powered|effortless(?:ly)?|unleash(?:es|ed|ing)?|supercharg(?:e|es|ed|er|ing))\b/i;

/**
 * An emoji. `Extended_Pictographic` minus the three that are typography and
 * not pictures — ©, ® and ™ belong in a rights statement, and a rights
 * statement is exactly what Stage 04 asks people to write (Law 44).
 */
const EMOJI = /(?![©®™])\p{Extended_Pictographic}/u;

const CLASS_CALLEES = new Set(["cn", "clsx", "cx", "cva", "twMerge", "tv"]);

const SOURCE_EXTENSIONS = new Set([".ts", ".tsx", ".js", ".jsx", ".mjs"]);

/** True when `node` sits inside a class list — `className`, or a `cn()` argument. */
function inClassContext(node: ts.Node): boolean {
  for (let current = node.parent; current; current = current.parent) {
    if (ts.isJsxAttribute(current)) {
      const name = current.name.getText();
      return name === "className" || name === "class";
    }
    if (
      ts.isCallExpression(current) &&
      ts.isIdentifier(current.expression) &&
      CLASS_CALLEES.has(current.expression.text)
    ) {
      return true;
    }
    if (ts.isFunctionLike(current)) return false;
  }
  return false;
}

/** True when a string literal is a path, a key or a type, rather than copy. */
function notCopy(node: ts.Node): boolean {
  const parent = node.parent;
  if (!parent) return false;
  if (ts.isImportDeclaration(parent) || ts.isExportDeclaration(parent)) return true;
  if (ts.isExternalModuleReference(parent)) return true;
  if (ts.isCallExpression(parent) && parent.expression.kind === ts.SyntaxKind.ImportKeyword) return true;
  if (ts.isLiteralTypeNode(parent)) return true;
  if (ts.isPropertyAssignment(parent) && parent.name === node) return true;
  if (ts.isElementAccessExpression(parent) && parent.argumentExpression === node) return true;
  return inClassContext(node);
}

function copyFindings(text: string): { rule: string; message: string }[] {
  const out: { rule: string; message: string }[] = [];
  if (EMOJI.test(text)) {
    out.push({
      rule: "no-emoji",
      message: "Emoji in a UI string. Stage 04 uses words and lucide icons, never emoji (PRD §15.2 rule 3).",
    });
  }
  const word = BANNED_WORDS.exec(text);
  if (word) {
    out.push({
      rule: "no-hype-copy",
      message: `"${word[0]}" is banned copy (PRD §15.2 rule 3). Say what the control does, in numbers where there are numbers.`,
    });
  }
  if (text.includes("!")) {
    out.push({
      rule: "no-exclamation",
      message: "An exclamation mark in a UI string. Stage 04 states results plainly (PRD §15.2 rule 3).",
    });
  }
  return out;
}

/** Every finding in one TypeScript or JavaScript source. */
export function checkSource(file: string, source: string): Finding[] {
  const kind = file.endsWith("x") ? ts.ScriptKind.TSX : ts.ScriptKind.TS;
  const sf = ts.createSourceFile(file, source, ts.ScriptTarget.Latest, true, kind);
  const findings: Finding[] = [];

  const report = (node: ts.Node, rule: string, message: string) => {
    const { line, character } = sf.getLineAndCharacterOfPosition(node.getStart(sf));
    findings.push({ file, line: line + 1, column: character + 1, rule, message });
  };

  const visit = (node: ts.Node) => {
    // Icons, however they arrive: a named import, a deep import, an alias.
    if (ts.isImportDeclaration(node) && ts.isStringLiteral(node.moduleSpecifier)) {
      const from = node.moduleSpecifier.text;
      if (from === "lucide-react" || from.startsWith("lucide-react/")) {
        if (BANNED_ICON_PATH.test(from)) {
          report(node, "no-ai-iconography", `\`${from}\` is AI iconography (PRD §15.2 rule 3).`);
        }
        const bindings = node.importClause?.namedBindings;
        if (bindings && ts.isNamedImports(bindings)) {
          for (const element of bindings.elements) {
            const imported = (element.propertyName ?? element.name).text;
            if (BANNED_ICON.test(imported)) {
              report(
                element,
                "no-ai-iconography",
                `\`${imported}\` is AI iconography (PRD §15.2 rule 3). Label generated content with an \`AI-generated\` chip and the model name instead.`,
              );
            }
          }
        }
      }
    }

    // Copy.
    let text: string | null = null;
    if (ts.isJsxText(node)) {
      text = node.text;
    } else if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      text = notCopy(node) ? null : node.text;
    } else if (ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)) {
      const template = node.parent?.parent;
      text = template && inClassContext(template) ? null : node.text;
    }
    if (text && text.trim()) {
      for (const finding of copyFindings(text)) report(node, finding.rule, finding.message);
    }

    ts.forEachChild(node, visit);
  };

  visit(sf);
  return findings;
}

const STYLE_LAWS: { pattern: RegExp; rule: string; message: string }[] = [
  {
    pattern: /#(?:[0-9a-f]{3,4}|[0-9a-f]{6}|[0-9a-f]{8})\b/i,
    rule: "tokens-only",
    message: "A raw hex colour (PRD §15.2 rule 2). Use a token from styles/tokens.css.",
  },
  {
    pattern: /(?:linear|radial|conic)-?gradient|<(?:linear|radial)Gradient\b/i,
    rule: "no-gradient",
    message: "A gradient (PRD §15.2 rule 2). Use a flat token surface.",
  },
  {
    pattern: /backdrop-filter|feGaussianBlur|\bblur\(|box-shadow|text-shadow|feDropShadow/i,
    rule: "no-blur-or-glow",
    message: "Blur, glass, glow or an in-flow shadow (PRD §15.2 rule 2).",
  },
];

/** Findings in a stylesheet or an SVG, where the only law that can break is rule 2. */
export function checkAsset(file: string, source: string): Finding[] {
  const findings: Finding[] = [];
  source.split("\n").forEach((line, index) => {
    for (const law of STYLE_LAWS) {
      const match = law.pattern.exec(line);
      if (match) {
        findings.push({
          file,
          line: index + 1,
          column: match.index + 1,
          rule: law.rule,
          message: law.message,
        });
      }
    }
  });
  return findings;
}

function walk(dir: string): string[] {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return [];
  }
  return entries.flatMap((entry) => {
    const path = join(dir, entry);
    return statSync(path).isDirectory() ? walk(path) : [path];
  });
}

/** Every finding under the Stage 04 roots of the app at `webRoot`. */
export function checkTree(webRoot: string): { files: number; findings: Finding[] } {
  const files = STAGE_04_ROOTS.flatMap((root) => walk(join(webRoot, root)));
  const findings = files.flatMap((path) => {
    const file = relative(webRoot, path);
    const ext = extname(path);
    const source = readFileSync(path, "utf8");
    if (SOURCE_EXTENSIONS.has(ext)) return checkSource(file, source);
    if (ext === ".css" || ext === ".svg") return checkAsset(file, source);
    return [];
  });
  return { files: files.length, findings };
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const webRoot = dirname(dirname(fileURLToPath(import.meta.url)));
  const { files, findings } = checkTree(webRoot);
  for (const f of findings) {
    console.error(`${f.file}:${f.line}:${f.column}  ${f.rule}  ${f.message}`);
  }
  if (findings.length > 0) {
    console.error(`\ncheck_design_laws: ${findings.length} finding(s) in ${files} Stage 04 file(s).`);
    process.exit(1);
  }
  console.log(`check_design_laws: ${files} Stage 04 file(s), no findings.`);
}
