"use client";

/**
 * A figure in the plan, and the calculation behind it (Stage 02 PRD §15.3 D).
 *
 * "Every `Number` renders with a superscript chip; hovering shows `formula_id`,
 * inputs and result; clicking opens the Evidence Explorer filtered to that
 * `derived` row." That sentence is the read side of law 14, and it is the whole
 * reason this app can be used to sign off a budget: a figure with no
 * calculation behind it is a figure somebody has to take on trust.
 *
 * Three states, and the difference between them matters:
 *
 * - a `Number` whose `calc_evidence_id` resolves — chip, popover, evidence link;
 * - a `Number` whose id does not resolve, because the evidence row was pruned —
 *   chip, and the popover says so. The calculation outlived its citation
 *   (`PlanCalc.evidence_id` is nullable by design) and pretending otherwise
 *   would be a link that 404s;
 * - a plain number, which §12 still uses for the rows inside a table — no chip.
 *   Drawing an unopenable chip on it would teach the reader that chips mean
 *   nothing.
 */

import { Sigma } from "lucide-react";
import Link from "next/link";

import { JsonTree } from "@/components/ui/json-tree";
import { Popover } from "@/components/ui/popover";
import {
  asPlanNumber,
  figureValue,
  type PlanFigure,
  type PlanNumber,
} from "@/lib/api/plan";
import { compactNumber, usd } from "@/lib/format";
import type { usePlanCalcIndex } from "@/lib/queries";
import { cn } from "@/lib/utils";

export type CalcIndex = ReturnType<typeof usePlanCalcIndex>;

/**
 * Format a figure by its declared unit.
 *
 * `usd()` carries no thousands separator on purpose — the separator is the
 * reader's locale and this plan has a DE market — so anything that reaches
 * seven digits is shown compact instead. `$2346000` is not a number anybody
 * reads, and a pipeline figure is a magnitude rather than something reconciled
 * against the split.
 */
export function formatFigure(value: PlanFigure, unit?: PlanNumber["unit"]): string {
  const scalar = figureValue(value);
  if (scalar === null) return typeof value === "string" && value ? value : "—";
  const resolved = unit ?? asPlanNumber(value)?.unit ?? "count";
  switch (resolved) {
    case "usd":
      return Math.abs(scalar) >= 1_000_000 ? `$${compactNumber(scalar)}` : usd(scalar, 0);
    case "pct":
      return `${scalar.toFixed(scalar % 1 === 0 ? 0 : 1)}%`;
    case "days":
      return `${Math.round(scalar)} ${Math.round(scalar) === 1 ? "day" : "days"}`;
    case "months":
      return `${Math.round(scalar)} ${Math.round(scalar) === 1 ? "month" : "months"}`;
    case "ratio":
      return scalar.toFixed(2);
    case "count":
    default:
      return Math.abs(scalar) >= 10_000
        ? compactNumber(scalar)
        : String(Number.isInteger(scalar) ? scalar : scalar.toFixed(1));
  }
}

export function Figure({
  value,
  unit,
  calcs,
  projectId,
  label,
  className,
}: {
  value: PlanFigure;
  unit?: PlanNumber["unit"];
  calcs: CalcIndex;
  projectId: string;
  /** What the figure is, for the popover heading and for screen readers. */
  label: string;
  className?: string;
}) {
  const traced = asPlanNumber(value);
  const text = formatFigure(value, unit);

  if (!traced?.calc_evidence_id) {
    return (
      <span data-numeric className={cn("text-fg", className)}>
        {text}
      </span>
    );
  }

  return (
    <span className={cn("inline-flex items-start gap-0.5", className)}>
      <span data-numeric className="text-fg">
        {text}
      </span>
      <CalcChip
        evidenceId={traced.calc_evidence_id}
        confidence={traced.confidence}
        label={label}
        calcs={calcs}
        projectId={projectId}
      />
    </span>
  );
}

function CalcChip({
  evidenceId,
  confidence,
  label,
  calcs,
  projectId,
}: {
  evidenceId: string;
  confidence: PlanNumber["confidence"];
  label: string;
  calcs: CalcIndex;
  projectId: string;
}) {
  const calc = calcs.byEvidenceId.get(evidenceId);
  const inputCount = calc ? Object.keys(calc.inputs).length : 0;

  return (
    <Popover
      side="top"
      align="start"
      trigger={
        <button
          type="button"
          className={cn(
            "mt-px inline-flex items-center rounded-[4px] px-1 py-px align-super transition-colors",
            "bg-accent-soft text-accent hover:bg-accent hover:text-accent-fg",
          )}
        >
          <Sigma aria-hidden className="size-2.5" />
          <span className="sr-only">Show the calculation behind {label}</span>
          {/* A figure the plan itself calls low-confidence says so on the chip
              rather than only inside it. A reader scanning the media plan for
              soft ground should not have to open twelve popovers to find it. */}
          {confidence === "low" ? (
            <span aria-hidden className="ml-0.5 text-[0.5rem] leading-none">
              ?
            </span>
          ) : null}
        </button>
      }
    >
      <div className="space-y-2">
        <p className="flex items-baseline justify-between gap-2">
          <span className="text-xs font-medium text-fg">{label}</span>
          <span className="text-xs text-fg-subtle">{confidence} confidence</span>
        </p>

        {calc ? (
          <>
            <p className="font-mono text-[0.6875rem] break-all text-fg-muted">
              {calc.formula_id}
            </p>
            <div className="space-y-1">
              <p className="text-[0.6875rem] font-medium tracking-wide text-fg-subtle uppercase">
                Result
              </p>
              <JsonTree value={calc.result} />
            </div>
            <div className="space-y-1">
              <p className="text-[0.6875rem] font-medium tracking-wide text-fg-subtle uppercase">
                Inputs
              </p>
              {inputCount === 0 ? (
                <p className="text-xs text-fg-subtle">None — this formula reads only constants.</p>
              ) : (
                <JsonTree value={calc.inputs} />
              )}
            </div>
            <p className="flex flex-wrap items-center gap-3">
              <span className="text-xs text-fg-subtle">constants {calc.calc_version}</span>
              <Link
                href={`/projects/${projectId}/evidence?ids=${evidenceId}`}
                className="text-xs text-accent hover:underline"
              >
                Open in evidence
              </Link>
            </p>
          </>
        ) : calcs.loading ? (
          <p className="text-sm text-fg-muted">Loading the calculation…</p>
        ) : (
          // The row is gone, not missing: `PlanCalc.evidence_id` is nullable so
          // a calculation outlives the evidence it produced. The figure is
          // still the figure that was computed.
          <p className="text-sm text-fg-muted">
            The calculation behind this figure is no longer stored. It was computed when the plan
            ran, and the figure is what it produced.
          </p>
        )}
      </div>
    </Popover>
  );
}
