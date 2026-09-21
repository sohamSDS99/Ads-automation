"use client";

import { CircleAlert, TriangleAlert } from "lucide-react";
import Link from "next/link";

import type { Blocker } from "@/lib/api/plan";
import { cn } from "@/lib/utils";

/**
 * Why campaign planning cannot start, in the server's own words.
 *
 * Stage 02 PRD §15.1 is explicit about the failure this exists to prevent:
 * "never a grey tab with a tooltip saying unavailable". So the sentence is
 * rendered verbatim — the API writes it, this does not paraphrase it — and
 * every row carries the link to the place it gets fixed.
 *
 * Warnings sit in the same list as blockers because they are the same kind of
 * fact about the same thing. What separates them is the icon, the tone, and
 * whether the Start button is disabled; hiding a warning somewhere else would
 * make "the plan can start, and here is what is old about it" two screens.
 */
export function EligibilityLock({ blockers }: { blockers: Blocker[] }) {
  if (blockers.length === 0) return null;

  return (
    <ul className="flex flex-col gap-2">
      {blockers.map((blocker) => {
        const warning = blocker.severity === "warning";
        const Icon = warning ? TriangleAlert : CircleAlert;
        return (
          <li
            key={`${blocker.code}-${blocker.detail}`}
            className={cn(
              "flex items-start gap-2.5 rounded-[var(--radius)] border px-3 py-2.5 text-sm",
              warning ? "border-status-gate/50" : "border-status-failed/50",
            )}
          >
            <Icon
              className={cn(
                "mt-0.5 size-4 shrink-0",
                warning ? "text-status-gate" : "text-status-failed",
              )}
              aria-hidden
            />
            <div className="min-w-0 space-y-1">
              <p className="text-fg">{blocker.detail}</p>
              <Link href={blocker.fix_url} className="text-xs text-accent hover:underline">
                {FIX_LABEL[blocker.code] ?? "Open"}
              </Link>
            </div>
          </li>
        );
      })}
    </ul>
  );
}

/**
 * What the link at the bottom of each row is called.
 *
 * Named per code rather than a single "Fix this": the destinations are a
 * report, a settings screen, a running plan and a team page, and one label
 * for four places is the kind of vagueness this component exists to avoid.
 */
const FIX_LABEL: Partial<Record<Blocker["code"], string>> = {
  no_accepted_research: "Read the report",
  research_schema_unsupported: "Open the research run",
  research_says_no_go: "Read the verdict",
  plan_in_flight: "Open the running plan",
  plan_already_frozen: "See the frozen plan",
  missing_credential: "Open Connections",
  source_stale: "Open the project",
  missing_permission: "See who has access",
};
