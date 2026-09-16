"use client";

import { Check, Lock } from "lucide-react";

import { cn } from "@/lib/utils";

export type Step = {
  id: string;
  title: string;
  /** Rendered with a lock and no link when the signed-in role cannot write it. */
  readOnly?: boolean;
  complete?: boolean;
};

/**
 * Where you are in setup, and what is left.
 *
 * Every step is reachable at any time: this is a form split across screens, not
 * a checkout. Forcing a linear path would mean an operator who only came to fix
 * one market has to walk past four screens to reach it.
 */
export function Stepper({
  steps,
  current,
  onSelect,
}: {
  steps: Step[];
  current: number;
  onSelect: (index: number) => void;
}) {
  return (
    <nav aria-label="Setup steps">
      <ol className="flex flex-wrap gap-1">
        {steps.map((step, index) => {
          const active = index === current;
          return (
            <li key={step.id}>
              <button
                type="button"
                onClick={() => onSelect(index)}
                aria-current={active ? "step" : undefined}
                className={cn(
                  "flex items-center gap-2 rounded-[var(--radius)] px-3 py-2 text-sm transition-colors",
                  active
                    ? "bg-accent-soft font-medium text-accent"
                    : "text-fg-muted hover:bg-surface-hover hover:text-fg",
                )}
              >
                <span
                  aria-hidden
                  className={cn(
                    "flex size-5 shrink-0 items-center justify-center rounded-full border text-[11px]",
                    step.complete && !active && "border-status-success text-status-success",
                    active && "border-accent",
                  )}
                >
                  {step.complete ? <Check className="size-3" /> : index + 1}
                </span>
                {step.title}
                {step.readOnly ? (
                  <Lock className="size-3 text-fg-subtle" aria-label="Read-only for your role" />
                ) : null}
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
