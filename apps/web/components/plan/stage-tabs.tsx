"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

/**
 * The pipeline, as a strip under the project header (Stage 02 PRD §15.1).
 *
 * Navigation, not a tab panel — so it is a `<nav>` of links with
 * `aria-current`, and not `role="tablist"`. Tab semantics on things that
 * change the URL break the arrow-key contract they promise: a screen reader
 * announces "tab 2 of 3, press right arrow" and the right arrow does nothing.
 *
 * Stage 03 renders as a disabled placeholder rather than being omitted,
 * because the point of the strip is that the whole pipeline is legible from
 * day one. Stage 02 is always reachable; what it shows when planning cannot
 * start is the named blocker, which is the page's job and not the tab's.
 */
const STAGES = [
  { id: "research", label: "01 Research", href: "" },
  { id: "plan", label: "02 Campaign planning", href: "/plan" },
  { id: "creative", label: "03 Creative", href: null, hint: "Coming later" },
] as const;

export function StageTabs({ projectId }: { projectId: string }) {
  const pathname = usePathname();
  const base = `/projects/${projectId}`;
  // Anything under `/plan` is stage 02; everything else under the project —
  // the overview, a run console, a report, evidence — is stage 01.
  const active = pathname.startsWith(`${base}/plan`) ? "plan" : "research";

  return (
    <nav
      aria-label="Pipeline stage"
      className="flex items-center gap-1 overflow-x-auto [scrollbar-width:none]"
    >
      {STAGES.map((stage) => {
        if (stage.href === null) {
          return (
            <span
              key={stage.id}
              aria-disabled
              className="-mb-px inline-flex shrink-0 items-center gap-1.5 border-b-2 border-transparent px-3 py-2 text-sm text-fg-subtle"
            >
              {stage.label}
              <span className="text-xs">· {stage.hint}</span>
            </span>
          );
        }
        const current = stage.id === active;
        return (
          <Link
            key={stage.id}
            href={`${base}${stage.href}`}
            aria-current={current ? "page" : undefined}
            className={cn(
              "-mb-px inline-flex shrink-0 items-center border-b-2 px-3 py-2 text-sm transition-colors",
              current
                ? "border-accent font-medium text-fg"
                : "border-transparent text-fg-muted hover:text-fg",
            )}
          >
            {stage.label}
          </Link>
        );
      })}
    </nav>
  );
}
