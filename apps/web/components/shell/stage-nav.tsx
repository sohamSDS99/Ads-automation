"use client";

import { BookCheck, Map as MapIcon, Palette, Telescope } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Skeleton } from "@/components/ui/skeleton";
import { stageChip } from "@/lib/api/guidelines";
import { useGuidelines, useProject } from "@/lib/queries";
import { cn } from "@/lib/utils";

/**
 * The pipeline, down the side panel (Stage 02 PRD §15.1, relocated).
 *
 * §15.1 drew this as a strip across the top of every project route. This is
 * the same navigation with the same contract — one row per stage, stage 02
 * always reachable, stage 03 a visible placeholder — turned on its side,
 * because the panel is where this product's navigation lives and two
 * navigation systems on one screen is one too many.
 *
 * Navigation, not a tab panel — so it is a `<nav>` of links with
 * `aria-current`, and not `role="tablist"`. Tab semantics on things that
 * change the URL break the arrow-key contract they promise: a screen reader
 * announces "tab 2 of 3, press right arrow" and the right arrow does nothing.
 *
 * Stage 02 is not gated here. What it shows when planning cannot start is the
 * named blocker, which is the page's job and not this row's — a grey row with
 * a tooltip saying "unavailable" is the one thing §15.1 rules out.
 *
 * **Stage 03 has no lock state at all**, which is a stronger statement than
 * "is not gated here" (Stage 03 PRD §15.1 rule 1). Stage 02 could in principle
 * compute a disabled row from `/plan/eligibility` and deliberately does not;
 * stage 03 has no upstream precondition to compute one from. Any lock on that
 * row is a bug, so there is no code path here that could produce one.
 *
 * §15.1 rule 4 also renumbers the placeholder: the board has seven stages and
 * the old `03 Creative` was one short of where copy and creative actually sit.
 */
const STAGES = [
  { id: "research", label: "01 Research", href: "", icon: Telescope },
  { id: "plan", label: "02 Campaign planning", href: "/plan", icon: MapIcon },
  { id: "guidelines", label: "03 Content guidelines", href: "/guidelines", icon: BookCheck },
  { id: "creative", label: "04 Copy & creative", href: null, icon: Palette, hint: "Coming later" },
] as const;

/** One geometry for all three rows, so they line up whatever they are made of. */
const ROW =
  "flex items-center gap-3 rounded-[var(--radius)] py-2 text-sm justify-center px-0 md:justify-start md:px-3";

/**
 * The label: the row's accessible name on the rail, its visible text above it.
 *
 * `sr-only` rather than `hidden`, because on the narrow rail the label is the
 * link's accessible name and an icon with no name is an unlabelled link.
 * `flex-1` rather than an auto margin on whatever follows — `not-sr-only`
 * resets `margin`, so an `ml-auto` sibling that is also screen-reader-only
 * would depend on which of the two rules the stylesheet happens to emit last.
 */
const LABEL = "sr-only md:not-sr-only md:min-w-0 md:flex-1 md:truncate";

export function StageNav({ projectId }: { projectId: string }) {
  const pathname = usePathname();
  const project = useProject(projectId);
  const base = `/projects/${projectId}`;
  // Anything under `/plan` is stage 02; everything else under the project —
  // the overview, a run console, a report, evidence — is stage 01.
  const guidelines = useGuidelines(projectId);
  // Longest-prefix first would matter if one route were a prefix of another;
  // these three are disjoint, so the order is only about reading order.
  const active = pathname.startsWith(`${base}/guidelines`)
    ? "guidelines"
    : pathname.startsWith(`${base}/plan`)
      ? "plan"
      : "research";
  // Rendered only once the list has loaded, and only when there is something
  // to say — see `stageChip`. A chip shown while the answer is still in flight
  // is a chip a person would act on before it is true.
  const chip = guidelines.data ? stageChip(guidelines.data.versions) : null;

  return (
    <nav aria-label="Pipeline stage" className="shrink-0 p-2">
      {/* Whose pipeline this is. The rows below say 01, 02 and 03 and nothing
          about which project they belong to, and on the deeper routes — a run
          console, a report, a plan viewer — the heading names the run rather
          than the project. Left in the name's own case: a project name is
          something somebody typed, and shouting it back is not a label. */}
      <div className="hidden px-3 pb-2 md:block">
        {project.data ? (
          <p className="truncate text-xs font-medium text-fg-subtle" title={project.data.name}>
            {project.data.name}
          </p>
        ) : (
          <Skeleton className="h-3.5 w-32" />
        )}
      </div>

      <ul className="flex flex-col gap-1">
        {STAGES.map((stage) => {
          const Icon = stage.icon;

          if (stage.href === null) {
            return (
              <li key={stage.id}>
                <span
                  aria-disabled
                  title={`${stage.label} · ${stage.hint}`}
                  className={cn(ROW, "text-fg-subtle")}
                >
                  <Icon className="size-4 shrink-0" aria-hidden />
                  <span className={LABEL}>{stage.label}</span>
                  {/* Sits at the far edge rather than trailing the label: it
                      is a note about the row, not part of its name. */}
                  <span className="sr-only text-xs md:not-sr-only md:shrink-0">{stage.hint}</span>
                </span>
              </li>
            );
          }

          const current = stage.id === active;
          return (
            <li key={stage.id}>
              <Link
                href={`${base}${stage.href}`}
                aria-current={current ? "page" : undefined}
                title={stage.label}
                className={cn(
                  ROW,
                  "transition-colors",
                  current
                    ? "bg-accent-soft font-medium text-accent"
                    : "text-fg-muted hover:bg-surface-hover hover:text-fg",
                )}
              >
                <Icon className="size-4 shrink-0" aria-hidden />
                <span className={LABEL}>{stage.label}</span>
                {/* Stage 03 carries its own status, independent of the other
                    two rows. Hidden on the narrow rail, where the row is an
                    icon and there is nowhere to put it. */}
                {stage.id === "guidelines" && chip ? (
                  <span className="hidden shrink-0 text-xs text-fg-subtle md:inline">{chip}</span>
                ) : null}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
