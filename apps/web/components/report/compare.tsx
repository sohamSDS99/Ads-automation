"use client";

/**
 * "Compare with previous run" (PRD §13.4 C).
 *
 * The question this answers is never "what does the report say" — the report is
 * right there — it is "what moved since last time". So the panel leads with the
 * handful of single values that decide something (the launch verdict above all)
 * and only then lists the records that changed, section by section.
 *
 * Sections with nothing to say are not rendered at all. The API omits them, and
 * a column of twenty-six "no change" rows would bury the three that matter.
 *
 * Where the API says it capped a list, the panel says so too. A cap that reads
 * as "and nothing else changed" is worse than no comparison.
 */

import { ArrowRight, Minus, PencilLine, Plus } from "lucide-react";

import { Alert } from "@/components/ui/alert";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import type { FieldChange, ItemDiff, RunDiff, SectionDiff } from "@/lib/api/diff";
import { absoluteTime, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const STATUS_STYLE = {
  added: { icon: Plus, label: "Added", tone: "text-status-success" },
  removed: { icon: Minus, label: "Removed", tone: "text-status-failed" },
  changed: { icon: PencilLine, label: "Changed", tone: "text-status-running" },
} as const;

export function ComparePanel({
  diff,
  pending,
  error,
}: {
  diff: RunDiff | undefined;
  pending: boolean;
  error: string | null;
}) {
  if (error) {
    return (
      <Alert tone="warning" title="Nothing to compare">
        {error}
      </Alert>
    );
  }
  if (pending || !diff) return <Skeleton className="h-40 w-full" />;

  return (
    <Card>
      <CardHeader
        title="Changes since the previous run"
        description={
          <>
            Comparing this report, generated{" "}
            <time dateTime={diff.generated_at} title={absoluteTime(diff.generated_at)}>
              {relativeTime(diff.generated_at)}
            </time>
            , with the run from{" "}
            <time
              dateTime={diff.against_generated_at}
              title={absoluteTime(diff.against_generated_at)}
            >
              {relativeTime(diff.against_generated_at)}
            </time>
            {diff.against_is_parent ? "" : " that you selected"}.
          </>
        }
      />
      <CardBody className="space-y-5">
        {diff.unchanged ? (
          <p className="text-sm text-fg-muted">
            Nothing the report tracks differs between these two runs. Citations are not compared —
            evidence is re-gathered every run, so every one of them is new by definition.
          </p>
        ) : null}

        {diff.scalars.length ? <ScalarChanges changes={diff.scalars} /> : null}

        {diff.sections.map((section) => (
          <SectionBlock key={section.path} section={section} />
        ))}
      </CardBody>
    </Card>
  );
}

/**
 * The single values. Given their own block above the sections because the
 * launch verdict moving from `go_with_fixes` to `no_go` is the one change
 * nobody should have to scroll to find.
 */
function ScalarChanges({ changes }: { changes: FieldChange[] }) {
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
          </dd>
        </div>
      ))}
    </dl>
  );
}

function SectionBlock({ section }: { section: SectionDiff }) {
  return (
    <section className="space-y-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="text-sm font-medium text-fg">{section.title}</h3>
        <p className="flex gap-3 text-xs tabular-nums text-fg-muted">
          {section.added ? <span className="text-status-success">+{section.added}</span> : null}
          {section.removed ? <span className="text-status-failed">−{section.removed}</span> : null}
          {section.changed ? (
            <span className="text-status-running">~{section.changed}</span>
          ) : null}
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
function Value({ value, className }: { value: unknown; className?: string }) {
  return (
    <span className={cn("break-words tabular-nums", className)}>{stringify(value)}</span>
  );
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
