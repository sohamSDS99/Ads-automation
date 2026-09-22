"use client";

import { Map as MapIcon, Palette, Telescope } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Skeleton } from "@/components/ui/skeleton";
import { useProject } from "@/lib/queries";
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
 * a tooltip saying "unavailable" is the one thing §15.1 rules out. Stage 03 is
 * a placeholder rather than an omission, because the point of the group is
 * that the whole pipeline is legible from day one.
 */
const STAGES = [
  { id: "research", label: "01 Research", href: "", icon: Telescope },
  { id: "plan", label: "02 Campaign planning", href: "/plan", icon: MapIcon },
  { id: "creative", label: "03 Creative", href: null, icon: Palette, hint: "Coming later" },
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
  const active = pathname.startsWith(`${base}/plan`) ? "plan" : "research";

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
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
