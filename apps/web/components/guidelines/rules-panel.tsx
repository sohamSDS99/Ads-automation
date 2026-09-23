"use client";

import { FlaskConical, Scale } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import {
  type Authority,
  type Matcher,
  type Rule,
  type RuleCategory,
  type RuleScope,
  type Severity,
} from "@/lib/api/guidelines";
import { useGuideline, useGuidelines } from "@/lib/queries";

/**
 * The sixth node tab: the rules this node produced (PRD §15.3 B).
 *
 * **Nothing here evaluates anything** (§15.4 rule 2). This panel describes
 * matchers; it never runs one. A matcher reimplemented in TypeScript is how a
 * writer and the pipeline end up disagreeing about the same headline, and the
 * disagreement would be invisible until somebody's ad was rejected.
 *
 * Where the rules come from is worth stating, because it is not obvious from
 * §15.3 B's wording. `Rule` objects are not what most nodes emit: they are
 * assembled at 3.6.1 by `synthesis.flatten`, which turns each *section* of the
 * rulebook into rules and stamps each one's category from the guardrails
 * registry. So there are two honest sources and this panel reads both:
 *
 * 1. **The node's own output**, when it already carries `Rule` objects — 3.4.3
 *    does, because the image rules are compiled in Python at the node.
 * 2. **The assembled rulebook**, filtered to the categories this node's stage
 *    produces. `category` is stamped from `RULES[rule_id].category` and cannot
 *    be passed by a caller, so it is a stable attribution rather than a guess.
 *
 * Before 3.6.1 has run there is no rulebook yet, and the panel says exactly
 * that rather than rendering an empty list that reads as "this node made no
 * rules".
 */

/**
 * Which rule categories each stage of the DAG is answerable for.
 *
 * Keyed on the stage, not the node, on purpose: the stage grouping is the
 * DAG's own (`NodeSpec.stage`), and a table keyed on node ids would silently
 * stop covering a node the day one is added inside a stage — which is exactly
 * what S3-P9 does to 3.5.
 */
const STAGE_CATEGORIES: Record<string, RuleCategory[]> = {
  "3.1": ["voice", "lexicon"],
  "3.2": ["claim", "offer"],
  "3.3": ["policy", "disclosure"],
  "3.4": ["asset_spec", "image"],
  "3.5": ["governance", "learned"],
  // 3.6 writes the rulebook, so it answers for all of them.
  "3.6": [
    "voice",
    "lexicon",
    "claim",
    "offer",
    "policy",
    "asset_spec",
    "image",
    "disclosure",
    "governance",
    "learned",
  ],
};

export function RulesPanel({
  projectId,
  runId,
  stage,
  output,
}: {
  projectId: string;
  runId: string;
  stage: string;
  /** This node's raw output, which for some nodes already carries `Rule`s. */
  output: unknown;
}) {
  const guidelines = useGuidelines(projectId);
  // The guideline row for *this* run, not the project's newest: a console left
  // open on last week's run must not show this week's rules.
  const guideline = guidelines.data?.versions.find((item) => item.guideline_run_id === runId);
  const detail = useGuideline(guideline?.id ?? null);

  const fromNode = rulesIn(output);
  const categories = STAGE_CATEGORIES[stage] ?? [];
  const fromRulebook = (detail.data?.payload?.rules ?? []).filter((rule) =>
    categories.includes(rule.category),
  );

  // The node's own rules win when it has them: they are what it actually
  // emitted, and they are readable the moment the node finishes rather than
  // twelve nodes later.
  const rules = fromNode.length > 0 ? fromNode : fromRulebook;
  const source = fromNode.length > 0 ? "node" : "rulebook";

  if (detail.isPending && fromNode.length === 0) {
    return <Skeleton className="h-32 w-full" />;
  }

  if (rules.length === 0) {
    return (
      <EmptyState
        icon={Scale}
        title="No rules from this node yet"
        description={
          detail.data?.payload
            ? "This node's output shapes the rulebook but registers no enforceable rule of its own."
            : "Rules are assembled into the rulebook by 3.6.1. Until it runs there is nothing compiled to show here."
        }
      />
    );
  }

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs text-fg-subtle">
        {rules.length} {rules.length === 1 ? "rule" : "rules"}
        {source === "rulebook"
          ? " from the assembled rulebook, filed under this stage"
          : " emitted by this node"}
      </p>
      <ul className="flex flex-col gap-2">
        {rules.map((rule) => (
          <RuleCard key={rule.rule_id} rule={rule} />
        ))}
      </ul>
    </div>
  );
}

function RuleCard({ rule }: { rule: Rule }) {
  return (
    <li className="rounded-[var(--radius)] border p-3">
      {/* The message first, because it is the rule. `rule_id` is a locator and
          sits under it in the same place every other id in this product does. */}
      <p className="text-sm text-fg">{rule.message}</p>
      {rule.fix_hint ? <p className="mt-1 text-sm text-fg-muted">{rule.fix_hint}</p> : null}

      <p className="mt-2 text-sm text-fg-muted">{describeMatcher(rule.matcher)}</p>

      <div className="mt-2 flex flex-wrap items-center gap-2">
        <SeverityChip severity={rule.severity} />
        <Badge>{rule.category.replace(/_/g, " ")}</Badge>
        <AuthorityChip authority={rule.authority} />
        {scopeLabel(rule.scope) ? (
          <span className="text-xs text-fg-subtle">{scopeLabel(rule.scope)}</span>
        ) : null}
      </div>

      <div className="mt-2 flex flex-wrap items-baseline justify-between gap-2">
        <span data-numeric className="font-mono text-xs text-fg-subtle">
          {rule.rule_id}
        </span>
        {/* §15.3 B asks for a "test this rule" button that opens the linter
            playground pre-filled. The playground is S3-P8's, so the control
            is named rather than wired: a button that 404s is worse than one
            that says where it is going, and Next would prefetch the dead
            route for every reader of this panel. S3-P8 makes this a link. */}
        <span className="inline-flex items-center gap-1.5 text-xs text-fg-subtle">
          <FlaskConical className="size-3.5" aria-hidden />
          Testable in the linter playground
        </span>
      </div>
    </li>
  );
}

/**
 * Severity, with the word as well as the colour.
 *
 * `blocking` is not styled as an error state: a blocking rule working
 * correctly is the system doing its job, and painting the whole rulebook red
 * would make the one genuinely broken thing indistinguishable from the 200
 * working ones.
 */
export function SeverityChip({ severity }: { severity: Severity }) {
  return (
    <Badge tone={severity === "blocking" ? "danger" : severity === "warning" ? "warning" : "neutral"}>
      {severity}
    </Badge>
  );
}

const AUTHORITY_LABEL: Record<Authority["source"], string> = {
  brand: "Brand book",
  google_policy: "Google Ads policy",
  legal_signature: "Legal signature",
  internal: "Internal constant",
  learned_disapproval: "Past disapproval",
};

/**
 * Who said so, and when anybody last checked.
 *
 * A link when the reference resolves to somewhere a person can go, plain text
 * otherwise. The `reviewed_at` date is always shown — a rule whose authority
 * nobody has re-read in a year is the thing this chip exists to expose, and
 * hiding the date behind a hover would defeat it.
 */
export function AuthorityChip({ authority }: { authority: Authority }) {
  const label = AUTHORITY_LABEL[authority.source] ?? authority.source.replace(/_/g, " ");
  const url = authority.reference.startsWith("http") ? authority.reference : null;
  const body = (
    <>
      {label}
      <span className="text-fg-subtle">
        {" · reviewed "}
        <time dateTime={authority.reviewed_at}>{authority.reviewed_at}</time>
      </span>
    </>
  );

  if (url) {
    return (
      <a
        href={url}
        target="_blank"
        rel="noreferrer"
        title={authority.reference}
        className="inline-flex items-center rounded-full border px-2 py-0.5 text-xs text-fg-muted underline-offset-2 transition-colors hover:border-border-strong hover:text-fg hover:underline"
      >
        {body}
      </a>
    );
  }
  return (
    <span
      title={authority.reference}
      className="inline-flex items-center rounded-full border px-2 py-0.5 text-xs text-fg-muted"
    >
      {body}
    </span>
  );
}

/** `null` when the rule applies everywhere — an empty scope is not a fact. */
function scopeLabel(scope?: RuleScope): string | null {
  if (!scope) return null;
  const parts = [
    ...(scope.surfaces ?? []),
    ...(scope.campaign_types ?? []),
    ...(scope.markets ?? []),
    ...(scope.languages ?? []),
    ...(scope.asset_types ?? []),
  ];
  return parts.length > 0 ? `only ${parts.join(", ").replace(/_/g, " ")}` : null;
}

/**
 * One matcher, in a sentence.
 *
 * Describing, never evaluating. The unknown branch prints the kind and its
 * JSON rather than nothing: a rule this build cannot render is still a rule
 * the linter enforces, and an empty row would say the opposite.
 */
export function describeMatcher(matcher: Matcher): string {
  switch (matcher.kind) {
    case "term_set": {
      const m = matcher as Extract<Matcher, { kind: "term_set" }>;
      const verb = m.mode === "require" ? "Requires" : "Forbids";
      const how = m.match && m.match !== "exact" ? ` (${m.match} match)` : "";
      const locale = m.locale ? `, ${m.locale}` : "";
      return `${verb} the ${m.terms.length === 1 ? "term" : "terms"} ${m.terms.join(", ")}${how}${locale}`;
    }
    case "regex": {
      const m = matcher as Extract<Matcher, { kind: "regex" }>;
      return `Matches the pattern ${m.pattern}`;
    }
    case "length": {
      const m = matcher as Extract<Matcher, { kind: "length" }>;
      const unit = m.unit ?? "chars";
      if (m.min != null && m.max != null) return `Between ${m.min} and ${m.max} ${unit}`;
      if (m.max != null) return `At most ${m.max} ${unit}`;
      if (m.min != null) return `At least ${m.min} ${unit}`;
      return `Bounded in ${unit}`;
    }
    case "count": {
      const m = matcher as Extract<Matcher, { kind: "count" }>;
      const entity = m.entity.replace(/_/g, " ");
      if (m.min != null && m.max != null) return `Between ${m.min} and ${m.max} ${entity}`;
      if (m.max != null) return `At most ${m.max} ${entity}`;
      if (m.min != null) return `At least ${m.min} ${entity}`;
      return `A bounded number of ${entity}`;
    }
    case "ratio": {
      const m = matcher as Extract<Matcher, { kind: "ratio" }>;
      const metric = m.metric.replace(/_/g, " ");
      if (m.max != null) return `${metric} at or below ${m.max}`;
      if (m.min != null) return `${metric} at or above ${m.min}`;
      return `Bounds ${metric}`;
    }
    case "enum_allow": {
      const m = matcher as Extract<Matcher, { kind: "enum_allow" }>;
      return `${m.field.replace(/_/g, " ")} must be one of ${m.allowed.join(", ")}`;
    }
    case "claim_licence": {
      const m = matcher as Extract<Matcher, { kind: "claim_licence" }>;
      return `Claim-shaped language (${m.detector_ids.join(", ")}) needs an approved, unexpired claim to license it`;
    }
    case "offer_binding": {
      const m = matcher as Extract<Matcher, { kind: "offer_binding" }>;
      return `A ${m.construction.replace(/_/g, " ")} must match ${m.field.replace(/_/g, " ")} in live offer data`;
    }
    case "disclosure": {
      const m = matcher as Extract<Matcher, { kind: "disclosure" }>;
      const where = m.placement && m.placement !== "anywhere" ? ` as a ${m.placement}` : "";
      return `Must carry the disclosure "${m.required_text}"${where}`;
    }
    default:
      return `${matcher.kind}: ${JSON.stringify(matcher)}`;
  }
}

/** The `Rule[]` a node output carries, if it carries one. */
function rulesIn(output: unknown): Rule[] {
  if (!output || typeof output !== "object") return [];
  const candidate = (output as { rules?: unknown }).rules;
  if (!Array.isArray(candidate)) return [];
  return candidate.filter(
    (item): item is Rule =>
      Boolean(item) &&
      typeof item === "object" &&
      typeof (item as Rule).rule_id === "string" &&
      typeof (item as Rule).message === "string" &&
      Boolean((item as Rule).matcher),
  );
}
