"use client";

/**
 * "This run could not fully read one of its sources" (PRD §16, §15 NF4).
 *
 * NF4 is the requirement this exists for: a connector that partly failed must
 * never be silent. The run still completes and the report is still usable — it
 * is just thinner than it looks, and the only way a reader can know that is if
 * something says so on the way past.
 *
 * Two severities, drawn differently on purpose:
 *
 * * **degraded** — a connector broke mid-run. Red, and it carries the
 *   connector's own words, which for the Transparency Center is the name of the
 *   selector that stopped matching. That string is the difference between "the
 *   competitor section is short" and a fix.
 * * **unavailable** — nothing of that kind is connected at all. That is a setup
 *   state, not a fault, and drawing it in red would teach people to ignore red.
 *
 * Rendered from `GET /runs/{id}`, never from the event stream, so it survives a
 * reload. A banner that disappears on refresh teaches people the problem went
 * away.
 */

import { TriangleAlert, Unplug } from "lucide-react";

import type { DegradedSource } from "@/lib/api/runs";
import { cn } from "@/lib/utils";

export function DegradedBanner({
  sources,
  className,
}: {
  sources: DegradedSource[];
  className?: string;
}) {
  const degraded = sources.filter((source) => source.severity === "degraded");
  const unavailable = sources.filter((source) => source.severity === "unavailable");
  if (!degraded.length && !unavailable.length) return null;

  return (
    <div className={cn("space-y-2", className)}>
      {degraded.length ? <DegradedGroup sources={degraded} /> : null}
      {unavailable.length ? <UnavailableGroup sources={unavailable} /> : null}
    </div>
  );
}

function DegradedGroup({ sources }: { sources: DegradedSource[] }) {
  return (
    <div
      role="alert"
      className="rounded-[var(--radius)] border border-status-failed bg-surface px-3.5 py-3 text-sm"
    >
      <div className="flex gap-3">
        <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0 text-status-failed" />
        <div className="min-w-0 space-y-2">
          <p className="font-medium text-fg">
            {sources.length === 1
              ? "One source did not fully answer"
              : `${sources.length} sources did not fully answer`}
          </p>
          <ul className="space-y-1.5">
            {sources.map((source) => (
              <li key={`${source.kind}-${source.severity}`} className="text-fg-muted">
                <span className="text-fg">{humanKind(source.kind)}</span>
                {source.detail ? (
                  <>
                    {" — "}
                    {/* Verbatim, in mono: it is a machine's own words about a
                        machine, and paraphrasing it would lose the selector
                        name that makes it actionable. */}
                    <span className="break-words font-mono text-xs text-fg-muted">
                      {source.detail}
                    </span>
                  </>
                ) : null}
                {source.nodes.length ? (
                  <span className="text-fg-subtle"> · {source.nodes.join(", ")}</span>
                ) : null}
              </li>
            ))}
          </ul>
          <p className="text-xs text-fg-subtle">
            The run continued. Sections drawn from these sources are thinner than usual, and the
            report lists them.
          </p>
        </div>
      </div>
    </div>
  );
}

function UnavailableGroup({ sources }: { sources: DegradedSource[] }) {
  return (
    <div
      role="status"
      className="flex gap-3 rounded-[var(--radius)] border bg-surface px-3.5 py-3 text-sm"
    >
      <Unplug aria-hidden className="mt-0.5 size-4 shrink-0 text-fg-subtle" />
      <div className="min-w-0">
        <p className="text-fg">
          Not connected:{" "}
          <span className="text-fg-muted">
            {sources.map((source) => humanKind(source.kind)).join(", ")}
          </span>
        </p>
        <p className="mt-0.5 text-xs text-fg-subtle">
          Nothing was collected for these. The report marks the sections that depend on them rather
          than filling them in.
        </p>
      </div>
    </div>
  );
}

/**
 * `search_term_pnl` → `search term pnl`.
 *
 * Underscores only. Title-casing would turn `cpc` into `Cpc`, and these are the
 * evidence kinds an operator already reads in the Evidence Explorer — they
 * should look the same in both places.
 */
function humanKind(kind: string): string {
  return kind.replace(/_/g, " ");
}
