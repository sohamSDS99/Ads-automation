"use client";

import { Info } from "lucide-react";

import type { AvailableBindings, GuidelineBindings } from "@/lib/api/guidelines";
import { relativeTime, shortDate } from "@/lib/format";

/**
 * The optional bindings, offered at start (Stage 03 PRD §15.3 A).
 *
 * Two independent checkboxes, not a radio group and not a required choice.
 * Each says what binding it *adds* rather than what it is — a person deciding
 * here is choosing how narrow the rulebook should be, not browsing artifacts.
 *
 * When neither exists the component renders a note and nothing else. That note
 * is the visible form of C-E6: it describes a consequence ("scope will be
 * widened"), never an obstacle, and the Start button beside it stays enabled.
 * If this ever renders as a warning banner or greys anything out, the
 * handshake has come back through the UI.
 */
export function BindingPicker({
  available,
  value,
  onChange,
  disabled,
}: {
  available: AvailableBindings;
  value: GuidelineBindings;
  onChange: (next: GuidelineBindings) => void;
  disabled?: boolean;
}) {
  const { research, plan } = available;

  if (!research && !plan) {
    return (
      <p className="flex items-start gap-2 rounded-[var(--radius)] bg-surface-sunken p-3 text-sm text-fg-muted">
        <Info className="mt-0.5 size-4 shrink-0" aria-hidden />
        <span>
          Running without research or plan — scope will be widened. The rulebook will cover every
          Google campaign type and every market on this project, and will say on its face what it
          was built without.
        </span>
      </p>
    );
  }

  return (
    <fieldset className="flex flex-col gap-3" disabled={disabled}>
      <legend className="text-sm font-medium">Optional bindings</legend>

      {research ? (
        <label className="flex cursor-pointer items-start gap-3 rounded-[var(--radius)] border border-border p-3 text-sm hover:bg-surface-hover">
          <input
            type="checkbox"
            className="mt-0.5 size-3.5 shrink-0 accent-[var(--accent)]"
            checked={Boolean(value.research_run_id)}
            onChange={(event) =>
              onChange({
                ...value,
                research_run_id: event.target.checked ? research.research_run_id : null,
                acceptance_id: event.target.checked ? research.acceptance_id : null,
              })
            }
          />
          <span className="min-w-0">
            <span className="font-medium">Accepted research</span>{" "}
            <span className="text-fg-subtle">
              · accepted {relativeTime(research.accepted_at)}
            </span>
            <span className="mt-1 block text-fg-muted">{research.adds}</span>
          </span>
        </label>
      ) : null}

      {plan ? (
        <label className="flex cursor-pointer items-start gap-3 rounded-[var(--radius)] border border-border p-3 text-sm hover:bg-surface-hover">
          <input
            type="checkbox"
            className="mt-0.5 size-3.5 shrink-0 accent-[var(--accent)]"
            checked={Boolean(value.plan_id)}
            onChange={(event) =>
              onChange({
                ...value,
                plan_id: event.target.checked ? plan.plan_id : null,
                plan_version: event.target.checked ? plan.plan_version : null,
              })
            }
          />
          <span className="min-w-0">
            <span className="font-medium">Frozen plan v{plan.plan_version}</span>{" "}
            <span className="text-fg-subtle">
              {plan.frozen_at ? `· frozen ${shortDate(plan.frozen_at)}` : null}
            </span>
            <span className="mt-1 block text-fg-muted">{plan.adds}</span>
          </span>
        </label>
      ) : null}

      {/* Both boxes may be left clear. Saying so removes the reading that an
          offered binding is a required one. */}
      <p className="text-xs text-fg-subtle">
        Leave both clear to build an unscoped rulebook. Binding either one narrows what the run
        covers; neither is required.
      </p>
    </fieldset>
  );
}
