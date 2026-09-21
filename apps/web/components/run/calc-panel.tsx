"use client";

import { ChevronDown, ChevronRight, Sigma } from "lucide-react";
import Link from "next/link";

import { EmptyState } from "@/components/ui/empty-state";
import { JsonTree } from "@/components/ui/json-tree";
import { Skeleton } from "@/components/ui/skeleton";
import type { PlanCalcRow } from "@/lib/api/plan";
import { usePlanCalcs } from "@/lib/queries";

/**
 * The arithmetic behind one node's numbers (Stage 02 PRD §15.3 B, *Calc* tab).
 *
 * This tab exists because of law 14: the model never does arithmetic, so every
 * figure has a registered `@formula` and a `plan_calc` row behind it. The
 * question it answers is "where did this number come from", asked by an
 * approver who is mid-decision on a gate — so the formula and its result are
 * on the closed row, and the inputs are one keystroke away rather than one
 * screen away.
 *
 * Collapsed by default: a node like 2.1.2 computes a ceiling per segment, and
 * ten expanded input trees would bury the ten results that are the reason
 * anyone opened the tab.
 */
export function CalcPanel({
  planRunId,
  nodeId,
  projectId,
}: {
  planRunId: string;
  nodeId: string;
  projectId: string;
}) {
  const calcs = usePlanCalcs(planRunId, nodeId);

  if (calcs.isPending) {
    return (
      <div className="space-y-2">
        <Skeleton className="h-14 w-full" />
        <Skeleton className="h-14 w-full" />
      </div>
    );
  }

  const items = calcs.data?.items ?? [];

  if (items.length === 0) {
    return (
      <EmptyState
        icon={Sigma}
        title="No numbers from this node"
        description="Nothing here computed a figure. Nodes that do leave one row per calculation, with the formula, its inputs and the constants version behind it."
      />
    );
  }

  return (
    <div className="space-y-2">
      {items.map((calc) => (
        <CalcRow key={calc.id} calc={calc} projectId={projectId} />
      ))}
    </div>
  );
}

function CalcRow({ calc, projectId }: { calc: PlanCalcRow; projectId: string }) {
  const headline = headlineOf(calc.result);
  const inputCount = Object.keys(calc.inputs).length;

  return (
    <details className="group rounded-[var(--radius)] border bg-surface">
      <summary className="flex cursor-pointer list-none items-baseline gap-2 px-3 py-2.5 [&::-webkit-details-marker]:hidden">
        {/* Two icons rather than one rotated: a 90deg transform on a 14px
            chevron rendered as an axis-aligned corner instead of a caret. The
            explicit pair is also what every other disclosure in this app
            does. */}
        <ChevronRight
          aria-hidden
          className="mt-0.5 size-3.5 shrink-0 self-start text-fg-subtle group-open:hidden"
        />
        <ChevronDown
          aria-hidden
          className="mt-0.5 hidden size-3.5 shrink-0 self-start text-fg-subtle group-open:block"
        />
        <div className="min-w-0 flex-1">
          <span className="block truncate font-mono text-xs text-fg" title={calc.formula_id}>
            {calc.formula_id}
          </span>
          <span className="mt-0.5 block text-xs text-fg-subtle">
            {inputCount} {inputCount === 1 ? "input" : "inputs"} · constants {calc.calc_version}
          </span>
        </div>
        {headline ? (
          <span data-numeric className="shrink-0 text-right font-mono text-xs text-fg">
            {/* The key is what gives the number its unit. Sighted readers get
                it from the formula id beside it and the Result tree inside;
                a screen reader would otherwise announce a bare "5760". */}
            <span className="sr-only">{headline.label}: </span>
            <span title={headline.label}>{headline.value}</span>
          </span>
        ) : null}
      </summary>

      <div className="space-y-3 border-t px-3 py-2.5">
        <Field label="Result">
          <JsonTree value={calc.result} />
        </Field>
        <Field label="Inputs">
          {inputCount === 0 ? (
            <p className="text-xs text-fg-subtle">
              None — this formula reads only constants.
            </p>
          ) : (
            <JsonTree value={calc.inputs} />
          )}
        </Field>
        {calc.evidence_id ? (
          <Link
            href={`/projects/${projectId}/evidence?ids=${calc.evidence_id}`}
            className="inline-block text-xs text-accent hover:underline"
          >
            Open the derived evidence row
          </Link>
        ) : (
          // Nullable by design: evidence can be pruned and the calculation has
          // to outlive it. Saying so beats a link that 404s.
          <p className="text-xs text-fg-subtle">
            Its evidence row is no longer stored. The calculation still reproduces.
          </p>
        )}
      </div>
    </details>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <p className="mb-1 text-xs font-medium tracking-wide text-fg-subtle">{label}</p>
      {children}
    </div>
  );
}

/**
 * The one value worth showing on a closed row.
 *
 * Only when the result is a single primitive: a formula that returns six
 * fields has no headline, and picking one arbitrarily would assert a
 * prominence the data does not have. The key carries the unit —
 * `max_cpa_won_usd` says more than any formatter this component could guess
 * at — so the number is rendered plainly and the key is the tooltip.
 *
 * Deliberately *not* `toLocaleString()`. This row and the `Result` tree three
 * lines below it show the same figure, and an auditor comparing them has to
 * see the same characters; `5,760` above `5760` invites the question of
 * whether they are two numbers. Worse, the separator is the reader's locale,
 * so an unlabelled `5.760` would read as five-point-seven-six in half of
 * Europe.
 */
function headlineOf(result: Record<string, unknown>): { label: string; value: string } | null {
  const entries = Object.entries(result);
  if (entries.length !== 1) return null;
  const [key, value] = entries[0]!;
  if (value === null || typeof value === "object") return null;
  return { label: key, value: String(value) };
}
