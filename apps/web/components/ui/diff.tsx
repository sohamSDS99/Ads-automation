"use client";

/**
 * How this app renders a comparison, whatever is being compared.
 *
 * Extracted from `components/report/compare.tsx` when Stage 02 added a plan
 * diff (§15.3 F): the engine behind both is the same one, the wire shape is the
 * same three projections, and two renderers would have meant a reader learning
 * added/removed/changed twice. Same move `components/ui/chart.tsx` made when
 * Stage 02 grew figures of its own.
 *
 * What stays with the callers is the header — a run diff is dated, a plan diff
 * is versioned, and those are different sentences.
 */

import { ArrowRight, Minus, PencilLine, Plus } from "lucide-react";

import type { FieldChange, ItemDiff, SectionDiff } from "@/lib/api/diff";
import { cn } from "@/lib/utils";

const STATUS_STYLE = {
  added: { icon: Plus, label: "Added", tone: "text-status-success" },
  removed: { icon: Minus, label: "Removed", tone: "text-status-failed" },
  changed: { icon: PencilLine, label: "Changed", tone: "text-status-running" },
} as const;

/**
 * The scalars, then the sections.
 *
 * Sections with nothing to say never arrive — the API omits them — and the
 * scalars lead because the one value that decides something (a launch verdict,
 * an envelope) is the change nobody should have to scroll to find.
 */
export function DiffBody({
  scalars,
  sections,
  unchanged,
  unchangedNote,
  className,
}: {
  scalars: FieldChange[];
  sections: SectionDiff[];
  unchanged: boolean;
  unchangedNote: string;
  className?: string;
}) {
  return (
    <div className={cn("space-y-5", className)}>
      {unchanged ? <p className="text-sm text-fg-muted">{unchangedNote}</p> : null}
      {scalars.length ? <ScalarChanges changes={scalars} /> : null}
      {sections.map((section) => (
        <SectionBlock key={section.path} section={section} />
      ))}
    </div>
  );
}

export function ScalarChanges({ changes }: { changes: FieldChange[] }) {
  return (
    <dl className="space-y-2">
      {changes.map((change) => (
        <div
          key={change.field}
          className="flex flex-wrap items-baseline gap-x-3 gap-y-1 border-b pb-2 last:border-0"
        >
          <dt className="min-w-40 text-sm font-medium text-fg">{change.field}</dt>
          <dd className="flex min-w-0 flex-1 flex-wrap items-baseline gap-2 text-sm">
            <Value value={change.before} className="text-fg-muted line-through decoration-1" />
            <ArrowRight aria-hidden className="size-3.5 shrink-0 text-fg-subtle" />
            <Value value={change.after} className="text-fg" />
            <Delta before={change.before} after={change.after} />
          </dd>
        </div>
      ))}
    </dl>
  );
}

/**
 * The difference, where both sides are numbers.
 *
 * §15.3 F asks for budget and target changes as before -> after *with the
 * delta*. The server sends the pair and not the difference, because only the
 * reader knows whether the useful form is absolute or proportional — so both
 * are shown, and neither is computed anywhere else. Subtracting two numbers is
 * not the forecast arithmetic §15.4 rule 2 forbids; it is the arithmetic of
 * reading two numbers next to each other.
 */
function Delta({ before, after }: { before: unknown; after: unknown }) {
  if (typeof before !== "number" || typeof after !== "number") return null;
  const change = after - before;
  if (change === 0) return null;
  const pct = before === 0 ? null : (change / Math.abs(before)) * 100;
  const sign = change > 0 ? "+" : "−";
  return (
    <span
      data-numeric
      className={cn(
        "shrink-0 rounded-full border px-1.5 py-px text-xs",
        // Not a status colour. Up is not good and down is not bad — a falling
        // envelope and a falling CPA mean opposite things, and the reader is
        // the only one who knows which this is.
        "border-border text-fg-muted",
      )}
    >
      {sign}
      {formatDelta(Math.abs(change))}
      {pct === null ? "" : ` · ${sign}${Math.abs(pct).toFixed(pct >= 10 ? 0 : 1)}%`}
    </span>
  );
}

function formatDelta(magnitude: number): string {
  if (Number.isInteger(magnitude)) return String(magnitude);
  return magnitude.toFixed(2);
}

export function SectionBlock({ section }: { section: SectionDiff }) {
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="text-sm font-medium text-fg">{section.title}</h3>
        <p className="flex gap-3 text-xs tabular-nums text-fg-muted">
          {section.added ? <span className="text-status-success">+{section.added}</span> : null}
          {section.removed ? <span className="text-status-failed">−{section.removed}</span> : null}
          {section.changed ? <span className="text-status-running">~{section.changed}</span> : null}
        </p>
      </div>
      <ul className="space-y-1.5">
        {section.items.map((item) => (
          <ItemRow key={`${item.status}-${item.key}`} item={item} />
        ))}
      </ul>
      {section.truncated ? (
        <p className="text-xs text-fg-subtle">
          Showing {section.items.length} of {section.total}. The counts above are complete.
        </p>
      ) : null}
    </section>
  );
}

function ItemRow({ item }: { item: ItemDiff }) {
  const { icon: Icon, label, tone } = STATUS_STYLE[item.status];
  return (
    <li className="flex gap-2.5 text-sm">
      <Icon aria-hidden className={cn("mt-1 size-3.5 shrink-0", tone)} />
      <div className="min-w-0 flex-1">
        <span className="sr-only">{label}: </span>
        <span className="break-words text-fg">{item.label}</span>
        {item.changes.length ? (
          <ul className="mt-0.5 space-y-0.5">
            {item.changes.map((change) => (
              <li
                key={change.field}
                className="flex flex-wrap items-baseline gap-x-2 text-xs text-fg-muted"
              >
                <span className="font-mono text-fg-subtle">{change.field}</span>
                <Value value={change.before} className="line-through decoration-1" />
                <ArrowRight aria-hidden className="size-3 shrink-0 text-fg-subtle" />
                <Value value={change.after} className="text-fg" />
              </li>
            ))}
          </ul>
        ) : null}
      </div>
    </li>
  );
}

/** Values arrive as arbitrary JSON. Render them without pretending to know the shape. */
export function Value({ value, className }: { value: unknown; className?: string }) {
  return <span className={cn("break-words tabular-nums", className)}>{stringify(value)}</span>;
}

const MAX_VALUE_CHARS = 180;

function stringify(value: unknown): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "string") return truncate(value);
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) return truncate(value.map(stringify).join(", ")) || "—";
  return truncate(JSON.stringify(value));
}

function truncate(text: string): string {
  return text.length > MAX_VALUE_CHARS ? `${text.slice(0, MAX_VALUE_CHARS)}…` : text;
}
