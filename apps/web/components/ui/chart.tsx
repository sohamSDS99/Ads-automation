/**
 * The chrome every figure in this app wears.
 *
 * Extracted from `components/report/charts.tsx` when Stage 02 added figures of
 * its own: two copies of a tooltip style drift, and the first thing anyone
 * notices about a dashboard is that two of its cards have different borders.
 * Nothing here draws data — it is the frame, the axis colours and the tooltip
 * surface, so a chart file is only ever about its own geometry.
 */
import { cn } from "@/lib/utils";

/** Axis text. One step quieter than body copy, and still over 4.5:1 on surface. */
export const AXIS = { stroke: "var(--fg-subtle)", fontSize: 11 } as const;

/** Gridlines and axis rules: one step off the surface, never a data colour. */
export const GRID = "var(--border)";

export const TOOLTIP_STYLE = {
  contentStyle: {
    background: "var(--surface-raised)",
    border: "1px solid var(--border)",
    borderRadius: "var(--radius)",
    fontSize: "0.75rem",
    color: "var(--fg)",
  },
  labelStyle: { color: "var(--fg-muted)" },
} as const;

export function ChartFrame({
  title,
  note,
  actions,
  children,
  className,
}: {
  title: string;
  note?: string;
  /** Sits on the title row: a unit toggle, a link out. Never a filter. */
  actions?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <figure className={cn("rounded-[var(--radius)] border bg-surface-raised p-4", className)}>
      <figcaption className="mb-3 flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h4 className="text-sm font-medium text-fg">{title}</h4>
          {note ? <p className="mt-0.5 text-xs text-fg-subtle">{note}</p> : null}
        </div>
        {actions ? <div className="shrink-0">{actions}</div> : null}
      </figcaption>
      {children}
    </figure>
  );
}

/**
 * A figure the screen promised and this run could not draw.
 *
 * Rendering nothing leaves a silent hole where a reader expects a chart, and a
 * reader who does not know the chart exists cannot tell a missing source from a
 * finding of "there is nothing here". Saying which source was missing turns an
 * absence into information.
 */
export function ChartMissing({ title, reason }: { title: string; reason: string }) {
  return (
    <figure className="rounded-[var(--radius)] border border-dashed bg-surface p-4">
      <figcaption>
        <h4 className="text-sm font-medium text-fg-muted">{title}</h4>
        <p className="mt-0.5 text-xs text-fg-subtle">{reason}</p>
      </figcaption>
      <div
        aria-hidden
        className="mt-3 flex h-24 items-center justify-center rounded-[4px] border border-dashed text-xs text-fg-subtle"
      >
        no data to plot
      </div>
    </figure>
  );
}
