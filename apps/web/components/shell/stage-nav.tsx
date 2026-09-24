"use client";

import { BookCheck, CircleDashed, Lock, Map as MapIcon, Palette, Telescope } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { Skeleton } from "@/components/ui/skeleton";
import { stageBadges, stageChip, type StageBadge } from "@/lib/api/guidelines";
import { useCreativeStatus, useGuidelineAttention, useGuidelines, useProject } from "@/lib/queries";
import { useSession } from "@/lib/session";
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
 *
 * **Stage 04 is the first row that locks** (Stage 04 PRD §15.1 rule 1), and it
 * locks on exactly one thing: `GET /creative/eligibility` answering
 * `eligible: false`. The lock never makes the row unreachable — a locked entry
 * is still a link, because the page it opens is the one that names the
 * blocker in words and links to the stage that fixes it. The row carries the
 * server's own first sentence as its accessible name and tooltip, so a lock is
 * never only a picture of a padlock. Stages 05–07 stay one placeholder row.
 */
const STAGES = [
  { id: "research", label: "01 Research", href: "", icon: Telescope },
  { id: "plan", label: "02 Campaign planning", href: "/plan", icon: MapIcon },
  { id: "guidelines", label: "03 Content guidelines", href: "/guidelines", icon: BookCheck },
  { id: "creative", label: "04 Copy & creative", href: "/creative", icon: Palette },
  { id: "later", label: "05–07", href: null, icon: CircleDashed, hint: "Coming later" },
] as const;

/** One geometry for all three rows, so they line up whatever they are made of. */
const ROW =
  "flex items-start gap-3 rounded-[var(--radius)] py-2 text-sm justify-center px-0 md:justify-start md:px-3";

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

/**
 * The two dots of §15.1 rule 3, stacked on the stage icon.
 *
 * Each dot's sentence is `sr-only` text inside the link, so the link's
 * accessible name carries it — "03 Content guidelines, 1 task is waiting for
 * your signature" — rather than a `title` a screen reader may or may not
 * announce. The ring is the row background, so a dot on the active row reads
 * against the accent tint as cleanly as on a plain one.
 */
function StageBadges({ badges }: { badges: StageBadge[] }) {
  return (
    <span className="absolute -top-0.5 -right-1 flex items-center gap-0.5">
      {badges.map((badge) => (
        <span key={badge.tone} className="flex items-center">
          <span
            aria-hidden
            className={cn(
              "size-1.5 rounded-full ring-2 ring-[var(--bg)]",
              badge.tone === "danger"
                ? "bg-[var(--status-failed)]"
                : "bg-[var(--status-gate)]",
            )}
          />
          <span className="sr-only">{badge.label}</span>
        </span>
      ))}
    </span>
  );
}

export function StageNav({ projectId }: { projectId: string }) {
  const pathname = usePathname();
  const { user } = useSession();
  const project = useProject(projectId);
  const base = `/projects/${projectId}`;
  // Anything under `/plan` is stage 02; everything else under the project —
  // the overview, a run console, a report, evidence — is stage 01.
  const guidelines = useGuidelines(projectId);
  const attention = useGuidelineAttention(projectId);
  // Longest-prefix first would matter if one route were a prefix of another;
  // these three are disjoint, so the order is only about reading order.
  const active = pathname.startsWith(`${base}/creative`)
    ? "creative"
    : pathname.startsWith(`${base}/guidelines`)
      ? "guidelines"
      : pathname.startsWith(`${base}/plan`)
        ? "plan"
        : "research";
  // Rendered only once the list has loaded, and only when there is something
  // to say — see `stageChip`. A chip shown while the answer is still in flight
  // is a chip a person would act on before it is true.
  const chip = guidelines.data ? stageChip(guidelines.data.versions, attention.data) : null;
  // Same rule as the chip: rendered only once the answer has arrived. A dot
  // that appears a second late is a dot somebody has already decided is absent.
  const badges = attention.data ? stageBadges(attention.data) : [];
  const creative = useCreativeStatus(projectId, user.id);

  /** Each row's status, keyed by stage. Rows 01 and 02 carry none here. */
  const status: Record<string, { chip: string | null; badges: StageBadge[]; lock?: string | null }> = {
    guidelines: { chip, badges },
    creative: { chip: creative.chip, badges: creative.badges, lock: creative.lock },
  };

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
          const row = status[stage.id];
          const lock = row?.lock ?? null;
          return (
            <li key={stage.id}>
              <Link
                href={`${base}${stage.href}`}
                aria-current={current ? "page" : undefined}
                title={lock ? `${stage.label} · Locked: ${lock}` : stage.label}
                className={cn(
                  ROW,
                  "transition-colors",
                  // `accent-soft-fg`, not `accent`: on the tint, dark
                  // `--accent` measures 4.0:1, under AA for 14px text.
                  current
                    ? "bg-accent-soft font-medium text-accent-soft-fg"
                    : "text-fg-muted hover:bg-surface-hover hover:text-fg",
                )}
              >
                {/* The badges ride the icon rather than the far edge, so they
                    survive the narrow rail — where the row *is* the icon and
                    the chip has already been dropped for want of room. */}
                {/* On the narrow rail the row *is* its icon, so a locked row
                    swaps its icon for the padlock there; above `md` the stage
                    keeps its own icon and the padlock trails the label. */}
                <span className="relative shrink-0">
                  {lock ? (
                    <>
                      <Lock className="size-4 md:hidden" aria-hidden />
                      <Icon className="hidden size-4 md:block" aria-hidden />
                    </>
                  ) : (
                    <Icon className="size-4" aria-hidden />
                  )}
                  {row && row.badges.length > 0 ? <StageBadges badges={row.badges} /> : null}
                </span>
                {/* Stages 03 and 04 carry their own status, independent of
                    the other rows — and they carry it on a second line rather than
                    beside the label. A chip and a label competing for 240px
                    is a fight the label loses: "Amendments pending" truncated
                    `03 Content guidelines` to `03…`, which is a row that has
                    stopped naming anything. Hidden on the narrow rail, where
                    the row is an icon and there is nowhere to put either. */}
                {row?.chip || lock ? (
                  <span className={cn(LABEL, "flex flex-col gap-0.5")}>
                    <span className="flex items-center gap-1.5">
                      <span className="truncate">{stage.label}</span>
                      {lock ? <Lock className="size-3.5 shrink-0 text-fg-subtle" aria-hidden /> : null}
                    </span>
                    {/* `fg-subtle` clears AA on the panel but not on the
                        current row's accent tint (4.4:1 light, 4.35:1 dark),
                        so the current row's chip is `fg-muted` (7.1, 5.7). */}
                    {row?.chip ? (
                      <span
                        className={cn(
                          "truncate text-xs font-normal",
                          current ? "text-fg-muted" : "text-fg-subtle",
                        )}
                      >
                        {row.chip}
                      </span>
                    ) : null}
                  </span>
                ) : (
                  <span className={LABEL}>{stage.label}</span>
                )}
                {/* The lock's sentence, in the link's accessible name — the
                    server's words, so "locked" is never the whole story. */}
                {lock ? <span className="sr-only">Locked: {lock}</span> : null}
              </Link>
            </li>
          );
        })}
      </ul>
    </nav>
  );
}
