"use client";

import { Check, Minus } from "lucide-react";

import type { ReviewChecklist as Checklist } from "@/lib/api/review";
import { CHECKS, ticked, type CheckKey } from "@/lib/creative/review";
import { cn } from "@/lib/utils";

/**
 * `ReviewChecklist` (Stage 04 PRD §15.4 H): the four checks an approval is
 * made of — `label correct`, `product matches`, `subjects allowed`, `rights
 * clear`. Each starts unticked and is ticked explicitly, one at a time (keys
 * 1–4); there is no "tick all". Approve stays disabled until all four are.
 *
 * `onToggle` absent renders the record read-only: a decided gate, or someone
 * who cannot decide it — for whom the control itself is absent (§15.5 item 4).
 */
export function ReviewChecklist({
  checklist,
  onToggle,
}: {
  checklist: Checklist;
  onToggle?: (key: CheckKey) => void;
}) {
  const count = ticked(checklist);
  return (
    <fieldset className="flex flex-col gap-1.5">
      <legend className="mb-1.5 flex w-full items-baseline justify-between text-sm font-medium text-fg">
        <span>Checklist</span>
        <span className="text-xs font-normal tabular-nums text-fg-muted">
          {count} of {CHECKS.length} ticked
        </span>
      </legend>
      {CHECKS.map(({ key, label, hint }, index) =>
        onToggle ? (
          <label
            key={key}
            className={cn(
              "flex cursor-pointer items-start gap-2.5 rounded-token border px-2.5 py-2 transition-colors duration-150 hover:bg-surface-hover",
              checklist[key] && "border-status-success",
            )}
          >
            <input
              type="checkbox"
              className="mt-0.5 size-4 shrink-0 accent-accent"
              checked={checklist[key]}
              onChange={() => onToggle(key)}
              aria-keyshortcuts={String(index + 1)}
              aria-describedby={`check-hint-${key}`}
            />
            <span className="min-w-0 flex-1">
              <span className="block text-sm text-fg">{label}</span>
              <span id={`check-hint-${key}`} className="block text-xs text-fg-muted">
                {hint}
              </span>
            </span>
            <kbd className="shrink-0 rounded border px-1.5 font-mono text-xs text-fg-muted">{index + 1}</kbd>
          </label>
        ) : (
          <p key={key} className="flex items-center gap-2 px-0.5 text-sm">
            {checklist[key] ? (
              <Check aria-hidden className="size-4 shrink-0 text-status-success" />
            ) : (
              <Minus aria-hidden className="size-4 shrink-0 text-fg-subtle" />
            )}
            <span className={checklist[key] ? "text-fg" : "text-fg-muted"}>{label}</span>
            <span className="sr-only">{checklist[key] ? "ticked" : "not ticked"}</span>
          </p>
        ),
      )}
    </fieldset>
  );
}
