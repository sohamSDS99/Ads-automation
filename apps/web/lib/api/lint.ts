/**
 * The linter playground's client.
 *
 * **There is no rule evaluation in this file, and there must never be.** Every
 * verdict, span and severity here came from `POST /guidelines/{id}/lint`. A
 * matcher reimplemented in TypeScript is how a writer and the pipeline end up
 * disagreeing about the same copy (PRD §15.4 rule 2).
 */
import { apiFetch } from "@/lib/api";

/**
 * Mirrors `agent.schemas.guardrails.Surface` exactly.
 *
 * These are validated server-side as a `Literal`, so a near-miss is a 422 and
 * not a soft failure — the names are Google's asset slots, not generic ones:
 * there is no bare `headline`, because a Search headline and a Performance Max
 * headline have different limits and different rules.
 */
export type Surface =
  | "rsa_headline"
  | "rsa_description"
  | "rsa_path"
  | "long_headline"
  | "pmax_headline"
  | "pmax_description"
  | "asset_group_description"
  | "display_text"
  | "youtube_script"
  | "sitelink"
  | "callout"
  | "structured_snippet"
  | "business_name"
  | "landing_page_section";

export type Severity = "blocking" | "warning" | "advisory";

export type LintTarget = {
  ref: string;
  surface: Surface | string;
  campaign_type: string;
  market: string;
  language: string;
  text?: string | null;
  image_ref?: string | null;
  generated_by_ai?: boolean;
  image_metrics?: Record<string, number> | null;
};

export type LintFinding = {
  target_ref: string;
  rule_id: string;
  severity: Severity;
  /** Character offsets into `LintTarget.text`. The only source of highlighting. */
  span: [number, number] | null;
  message: string;
  fix_hint: string | null;
  authority_ref: string;
  claim_id: string | null;
  /** A blocking check whose input was unavailable. Never styled as a pass. */
  indeterminate: boolean;
};

export type LintResult = {
  ruleset_version: string;
  verdict: "pass" | "pass_with_warnings" | "fail";
  findings: LintFinding[];
  targets_checked: number;
  rules_evaluated: number;
  elapsed_ms: number;
  evaluated_at: string;
};

export type LintResponse = { guideline_id: string; result: LintResult };

export type ImageLintResult = {
  guideline_id: string;
  ruleset_version: string;
  verdict: "pass" | "pass_with_warnings" | "fail" | "indeterminate";
  reason: string | null;
  findings: {
    rule_id: string;
    severity: Severity;
    message: string;
    fix_hint: string | null;
    authority_ref: string;
    indeterminate: boolean;
  }[];
  metrics: Record<string, unknown>;
  rules_evaluated: number;
  evidence_id: string | null;
  evaluated_at: string;
};

/**
 * Lint copy. Side-effect free, so it is safe on a debounce.
 *
 * `signal` is required by convention rather than by the type: the playground
 * fires on every keystroke and an un-aborted in-flight request will resolve
 * after a newer one and overwrite fresher findings with staler ones.
 */
export function lintText(guidelineId: string, targets: LintTarget[], signal?: AbortSignal) {
  return apiFetch<LintResponse>(`/guidelines/${guidelineId}/lint`, {
    method: "POST",
    body: JSON.stringify({ targets }),
    signal,
  });
}

export function lintImage(
  guidelineId: string,
  file: File,
  scope: { surface: string; campaign_type: string; market: string; language: string },
) {
  const form = new FormData();
  form.append("file", file);
  form.append("surface", scope.surface);
  form.append("campaign_type", scope.campaign_type);
  form.append("market", scope.market);
  form.append("language", scope.language);
  return apiFetch<ImageLintResult>(`/guidelines/${guidelineId}/lint/image`, {
    method: "POST",
    body: form,
  });
}

/** Grouped the way the picker reads: which campaign the slot belongs to. */
export const SURFACES: { id: Surface; label: string; group: string }[] = [
  { id: "rsa_headline", label: "Headline", group: "Search" },
  { id: "rsa_description", label: "Description", group: "Search" },
  { id: "rsa_path", label: "Path", group: "Search" },
  { id: "pmax_headline", label: "Headline", group: "Performance Max" },
  { id: "long_headline", label: "Long headline", group: "Performance Max" },
  { id: "pmax_description", label: "Description", group: "Performance Max" },
  { id: "asset_group_description", label: "Asset group description", group: "Performance Max" },
  { id: "display_text", label: "Display text", group: "Display" },
  { id: "youtube_script", label: "Script", group: "YouTube" },
  { id: "sitelink", label: "Sitelink", group: "Extensions" },
  { id: "callout", label: "Callout", group: "Extensions" },
  { id: "structured_snippet", label: "Structured snippet", group: "Extensions" },
  { id: "business_name", label: "Business name", group: "Extensions" },
  { id: "landing_page_section", label: "Landing page section", group: "Landing page" },
];

/**
 * Campaign types, spelled as `content_constants.yaml` spells them.
 *
 * `campaign_type` is a free string on `LintTarget` rather than a literal, so a
 * typo here would not 422 — it would silently scope every campaign-scoped rule
 * out and return a clean pass. That failure is quieter than a rejection, which
 * is why these are copied from the constants file rather than invented.
 */
export const CAMPAIGN_TYPES = [
  "search",
  "performance_max",
  "demand_gen",
  "display",
  "video",
  "shopping",
] as const;

/**
 * Slice `text` into highlighted and plain runs from the server's spans.
 *
 * Overlaps are resolved by taking the most severe span at each character, so
 * two rules objecting to the same words render once rather than as nested
 * marks that cannot be read.
 */
export function highlightRuns(
  text: string,
  findings: LintFinding[],
): { text: string; severity: Severity | null }[] {
  const rank: Record<Severity, number> = { advisory: 1, warning: 2, blocking: 3 };
  const at: (Severity | null)[] = new Array(text.length).fill(null);
  for (const finding of findings) {
    if (!finding.span) continue;
    const [start, end] = finding.span;
    for (let i = Math.max(0, start); i < Math.min(text.length, end); i += 1) {
      const current = at[i] ?? null;
      if (current === null || rank[finding.severity] > rank[current]) {
        at[i] = finding.severity;
      }
    }
  }
  const runs: { text: string; severity: Severity | null }[] = [];
  let index = 0;
  while (index < text.length) {
    const severity = at[index] ?? null;
    let end = index;
    while (end < text.length && (at[end] ?? null) === severity) end += 1;
    runs.push({ text: text.slice(index, end), severity });
    index = end;
  }
  return runs;
}

export const VERDICT_COPY: Record<string, { label: string; tone: string }> = {
  pass: { label: "Passes", tone: "success" },
  pass_with_warnings: { label: "Passes with warnings", tone: "gate" },
  fail: { label: "Blocked", tone: "failed" },
  indeterminate: { label: "Could not be checked", tone: "skipped" },
};
