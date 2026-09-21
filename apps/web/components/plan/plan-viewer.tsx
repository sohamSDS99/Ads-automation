"use client";

/**
 * The Plan Viewer (Stage 02 PRD §15.3 D).
 *
 * "Sticky TOC on the left, plan on the right, header bar pinned." That is the
 * Stage 01 report viewer's shape, and the TOC is literally its component — the
 * only thing Stage 02 added to `report/toc.tsx` was the ability to say what it
 * is a table of contents *for* (law 19).
 *
 * The header is where this screen differs from a report. A report is read; a
 * plan is *signed*. So the pinned bar carries the four facts somebody needs
 * before they can commit — which version this is, whether it is frozen, what
 * research it came from, and what it costs per month — and the freeze control
 * sits beside them rather than at the bottom of a long page.
 *
 * Sections render from whatever the payload holds. A plan run that halted at
 * gate G3 has objectives and a media plan and no structure, and this page opens
 * and says so per section, because the gate decisions somebody came to read are
 * on it too.
 */

import { ExternalLink, Snowflake } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { Figure } from "@/components/plan/figure";
import { FreezeButton } from "@/components/plan/freeze-dialog";
import {
  BacklogSection,
  ChannelSlateSection,
  MeasurementSection,
  MediaPlanSection,
  ObjectivesSection,
  SectionMissing,
  at,
  firstAt,
  textAt,
  type SortKey,
} from "@/components/plan/plan-sections";
import { StructureTree } from "@/components/plan/structure-tree";
import { Toc, type TocEntry } from "@/components/report/toc";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import {
  FIGURE_PATHS,
  figureValue,
  type PlanDetail,
  type PlanStatus,
} from "@/lib/api/plan";
import { absoluteTime, relativeTime, usd } from "@/lib/format";
import { usePlan, usePlanCalcIndex, usePlanStructure } from "@/lib/queries";
import { Can } from "@/lib/session";
import { cn } from "@/lib/utils";

const SECTIONS: TocEntry[] = [
  { id: "plan-summary", label: "Summary" },
  { id: "plan-objectives", label: "Objectives" },
  { id: "plan-media", label: "Media plan" },
  { id: "plan-slate", label: "Channel slate" },
  { id: "plan-structure", label: "Account structure" },
  { id: "plan-measurement", label: "Measurement" },
  { id: "plan-backlog", label: "Test backlog" },
  { id: "plan-decisions", label: "Decisions & risks" },
];

/** How a plan's lifecycle state reads, and what each state means for the reader. */
const STATUS: Record<PlanStatus, { label: string; tone: string; hint: string }> = {
  draft: {
    label: "Draft",
    tone: "border-border text-fg-muted",
    hint: "Still being decided. Nothing here is signed off.",
  },
  blocked: {
    label: "Blocked",
    tone: "border-[var(--status-failed)] text-status-failed",
    hint: "A gate was rejected. The plan needs a new run before it can go further.",
  },
  ready_to_freeze: {
    label: "Ready to freeze",
    tone: "border-[var(--status-gate)] text-status-gate",
    hint: "All four gates are approved and the critique raised nothing blocking.",
  },
  frozen: {
    label: "Frozen",
    tone: "border-[var(--status-success)] text-status-success",
    hint: "Signed off and immutable. Changes mean a new version from a new plan run.",
  },
  superseded: {
    label: "Superseded",
    tone: "border-border text-fg-subtle",
    hint: "A newer version has been frozen. Kept as the record of what was agreed then.",
  },
};

export function PlanViewer({
  planRunId,
  projectId,
}: {
  planRunId: string;
  projectId: string;
}) {
  const plan = usePlan(planRunId);
  const calcs = usePlanCalcIndex(planRunId);
  const [sort, setSort] = useState<SortKey>("ice");

  if (plan.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-64 w-full" />
      </div>
    );
  }

  if (plan.isError || !plan.data) {
    return (
      <Alert tone="warning" title="No plan on this run yet">
        A plan exists once the run has synthesised one, at node 2.6.1. Until then the run console
        is where its progress is.
      </Alert>
    );
  }

  const detail = plan.data;
  const payload = detail.payload;
  // Through `firstAt` and `figureValue`: the envelope is a §12 `Number` whose
  // field `plan_contract.py` calls `monthly_cap`, and either a hard-coded path
  // or a bare `typeof === "number"` prints an em dash on the header of a
  // correctly-assembled plan.
  const envelopeUsd = figureValue(firstAt(payload, FIGURE_PATHS.monthlyEnvelope) as never);

  return (
    <div className="space-y-4">
      <PlanHeader plan={detail} projectId={projectId} envelopeUsd={envelopeUsd} calcs={calcs} />

      {detail.source_superseded ? (
        <Alert tone="warning" title="The research behind this plan has been re-accepted">
          A newer research report was accepted after this plan was built. The plan is still the
          record of what was decided; a plan built on the newer research needs a new run.
        </Alert>
      ) : null}

      {/* KNOWN DEFECT, measured and not yet fixed: at 390px this page scrolls
          sideways by ~302px. Bounding the three prose columns took it from
          520px (a `min-w-max` table sizes itself to an unwrapped sentence —
          `Table`'s own header warns about exactly that) and neutralising
          `min-w-max` on all six tables would take it to 123px, so the tables
          are most but not all of it.

          `overflow-x-clip` here and on the content column below both changed
          the measurement by zero, which rules out "a descendant leaks out of
          this box" and means the earlier section-removal bisect was misleading:
          removing a large block reflows the whole page, so what it named is not
          necessarily what contains the cause. Left uncontained rather than
          papered over — `make browser-s2p6c` fails on it, which is where a
          known defect belongs. */}
      <div className="flex flex-col gap-6 lg:flex-row">
        {/* Sticky, and only on wide screens: a 390px viewport has no column to
            spare, and a horizontal TOC above the plan would push the plan
            itself below the fold. */}
        <Toc
          entries={SECTIONS}
          label="Plan contents"
          className="hidden shrink-0 lg:block lg:w-44 lg:self-start lg:sticky lg:top-32"
        />

        <div className="min-w-0 flex-1 space-y-8">
          <Section id="plan-summary" title="Summary">
            {textAt(payload, "executive_summary") ? (
              // 65-75ch measure: this is the one part of the plan that is prose
              // rather than a table, and it is the part most likely to be read
              // end to end.
              <p className="max-w-[70ch] text-sm leading-relaxed text-fg-muted">
                {textAt(payload, "executive_summary")}
              </p>
            ) : (
              <SectionMissing
                what="No summary yet"
                why="Node 2.6.1 writes the executive summary when it assembles the plan."
              />
            )}
          </Section>

          <Section id="plan-objectives" title="Objectives">
            <ObjectivesSection payload={payload} calcs={calcs} projectId={projectId} />
          </Section>

          <Section id="plan-media" title="Media plan">
            <MediaPlanSection payload={payload} calcs={calcs} projectId={projectId} />
          </Section>

          <Section id="plan-slate" title="Channel slate">
            <ChannelSlateSection payload={payload} calcs={calcs} projectId={projectId} />
          </Section>

          <Section id="plan-structure" title="Account structure">
            <StructureSection planRunId={planRunId} hasPlan={Boolean(payload)} />
          </Section>

          <Section id="plan-measurement" title="Measurement">
            <MeasurementSection payload={payload} calcs={calcs} projectId={projectId} />
          </Section>

          <Section id="plan-backlog" title="Test backlog">
            <BacklogSection
              payload={payload}
              calcs={calcs}
              projectId={projectId}
              sort={sort}
              onSort={setSort}
            />
          </Section>

          <Section id="plan-decisions" title="Decisions & risks">
            <DecisionsSection plan={detail} />
          </Section>
        </div>
      </div>
    </div>
  );
}

/**
 * The pinned bar.
 *
 * `top-16` clears the app header this sits under; the TOC's `top-32` clears
 * both. Two stacked sticky elements is the one place in this app where those
 * numbers have to agree, so they are written next to each other.
 */
function PlanHeader({
  plan,
  projectId,
  envelopeUsd,
  calcs,
}: {
  plan: PlanDetail;
  projectId: string;
  envelopeUsd: number | null;
  calcs: ReturnType<typeof usePlanCalcIndex>;
}) {
  const status = STATUS[plan.status];
  const blended = firstAt(plan.payload, FIGURE_PATHS.blendedTarget);

  return (
    <div className="sticky top-16 z-10 -mx-4 border-b bg-bg/95 px-4 py-3 backdrop-blur-sm sm:-mx-6 sm:px-6">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
            <h1 className="text-[length:var(--text-lg)] font-medium tracking-tight text-fg">
              {/* Version 0 is what every unfrozen plan carries (migration
                  0014), so it is never printed as a number — "version 0" reads
                  as a real version that happens to be zero. */}
              {plan.version > 0 ? `Campaign plan v${plan.version}` : "Campaign plan — unversioned"}
            </h1>
            <span
              title={status.hint}
              className={cn(
                "shrink-0 rounded-full border px-2 py-0.5 text-xs font-medium",
                status.tone,
              )}
            >
              {plan.status === "frozen" ? (
                <Snowflake aria-hidden className="mr-1 inline size-3 align-[-1px]" />
              ) : null}
              {status.label}
            </span>
          </div>

          <p className="mt-1 flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-xs text-fg-muted">
            {plan.source ? (
              <Link
                href={`/projects/${projectId}/runs/${plan.source.research_run_id}/report`}
                className="inline-flex items-baseline gap-1 text-accent hover:underline"
              >
                Research run {plan.source.research_run_id.slice(0, 8)}
                <ExternalLink aria-hidden className="size-3 self-center" />
              </Link>
            ) : null}
            {plan.source ? (
              <span>
                accepted by {plan.source.accepted_by_name}{" "}
                <time
                  dateTime={plan.source.accepted_at}
                  title={absoluteTime(plan.source.accepted_at)}
                >
                  {relativeTime(plan.source.accepted_at)}
                </time>
              </span>
            ) : null}
            {plan.frozen_at ? (
              <span>
                frozen by {plan.frozen_by_name ?? "someone no longer here"}{" "}
                <time dateTime={plan.frozen_at} title={absoluteTime(plan.frozen_at)}>
                  {relativeTime(plan.frozen_at)}
                </time>
              </span>
            ) : null}
          </p>
        </div>

        <div className="flex flex-wrap items-end gap-x-6 gap-y-2">
          <dl className="flex flex-wrap gap-x-6 gap-y-2">
            <div>
              <dt className="text-xs text-fg-muted">Monthly envelope</dt>
              <dd data-numeric className="mt-0.5 text-sm font-medium text-fg">
                {envelopeUsd === null ? "—" : usd(envelopeUsd, 0)}
              </dd>
            </div>
            <div>
              <dt className="text-xs text-fg-muted">Blended target cost per lead</dt>
              <dd className="mt-0.5 text-sm font-medium">
                <Figure
                  value={
                    typeof blended === "number" || (blended && typeof blended === "object")
                      ? (blended as never)
                      : null
                  }
                  unit="usd"
                  calcs={calcs}
                  projectId={projectId}
                  label="Blended target cost per lead"
                />
              </dd>
            </div>
          </dl>

          {/* Absent, not disabled, without the permission (§15.4 rule 3). A
              disabled Freeze tells a viewer that freezing is something they
              might do once some condition passes, and none will. */}
          <Can permission="plan_freeze">
            <FreezeButton plan={plan} projectId={projectId} envelopeUsd={envelopeUsd} />
          </Can>
        </div>
      </div>
    </div>
  );
}

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: React.ReactNode;
}) {
  return (
    // `scroll-mt` so an anchor jump lands the heading below the pinned header
    // rather than underneath it.
    <section id={id} className="scroll-mt-32 space-y-3">
      <h2 className="text-[length:var(--text-md)] font-medium tracking-tight text-fg">{title}</h2>
      {children}
    </section>
  );
}

/** The tree, fetched page by page (§16 rule 4). */
function StructureSection({ planRunId, hasPlan }: { planRunId: string; hasPlan: boolean }) {
  const structure = usePlanStructure(planRunId, hasPlan);
  const pages = useMemo(() => structure.data?.pages ?? [], [structure.data]);

  return (
    <StructureTree
      pages={pages}
      loading={structure.isPending}
      hasNextPage={structure.hasNextPage}
      fetchingNextPage={structure.isFetchingNextPage}
      onNeedMore={() => {
        if (structure.hasNextPage && !structure.isFetchingNextPage) void structure.fetchNextPage();
      }}
    />
  );
}

/**
 * The four decisions, then what the plan admits it does not know.
 *
 * Assumptions and risks sit at the bottom rather than in a preamble, and they
 * are last on purpose: a reader who has just read the whole plan is the reader
 * for whom "this rests on DE keyword coverage from one priced term" means
 * something.
 */
function DecisionsSection({ plan }: { plan: PlanDetail }) {
  const dependencies = claims(plan.payload, "open_dependencies");
  const assumptions = claims(plan.payload, "assumptions");
  const risks = claims(plan.payload, "risks");

  return (
    <div className="space-y-4">
      <ul className="space-y-1">
        {plan.gates.map((gate) => (
          <li
            key={gate.gate_key}
            className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-b pb-1.5 text-sm last:border-0"
          >
            <span className="w-7 shrink-0 font-mono text-xs text-fg-subtle">{gate.gate_key}</span>
            <span
              className={cn(
                "shrink-0",
                gate.status === "approved" ? "text-fg" : "text-status-gate",
              )}
            >
              {gate.status === "not_reached" ? "not reached" : gate.status.replace(/_/g, " ")}
            </span>
            {gate.decided_by_name ? (
              <span className="text-fg-muted">
                by {gate.decided_by_name}
                {gate.decided_at ? (
                  <>
                    {" · "}
                    <time dateTime={gate.decided_at} title={absoluteTime(gate.decided_at)}>
                      {relativeTime(gate.decided_at)}
                    </time>
                  </>
                ) : null}
              </span>
            ) : null}
            {gate.note ? (
              <span className="min-w-0 flex-1 text-xs text-fg-subtle">“{gate.note}”</span>
            ) : null}
          </li>
        ))}
      </ul>

      {plan.critique?.advisory.length ? (
        <ClaimList title="The critique's advisory notes" items={plan.critique.advisory} />
      ) : null}
      {dependencies.length ? <ClaimList title="Still open" items={dependencies} /> : null}
      {assumptions.length ? <ClaimList title="Assumed" items={assumptions} /> : null}
      {risks.length ? <ClaimList title="Risks" items={risks} /> : null}
    </div>
  );
}

function claims(payload: unknown, key: string): string[] {
  const value = at(payload, key);
  if (!Array.isArray(value)) return [];
  return value
    .map((row) => {
      if (typeof row === "string") return row;
      if (row && typeof row === "object" && "statement" in row) {
        const statement = (row as { statement?: unknown }).statement;
        const owner = (row as { owner?: unknown }).owner;
        const blocking = (row as { blocking?: unknown }).blocking === true;
        if (typeof statement !== "string") return null;
        const suffix = typeof owner === "string" && owner ? ` — ${owner}` : "";
        return `${statement}${suffix}${blocking ? " (blocking)" : ""}`;
      }
      return null;
    })
    .filter((row): row is string => Boolean(row));
}

function ClaimList({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <h3 className="text-sm font-medium text-fg">{title}</h3>
      <ul className="mt-1 space-y-1 text-sm text-fg-muted">
        {items.map((item) => (
          <li key={item} className="flex gap-2">
            <span aria-hidden className="text-fg-subtle">
              ·
            </span>
            <span className="min-w-0">{item}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
