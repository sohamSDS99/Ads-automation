"use client";

import { ArrowLeft, ArrowLeftRight, FileStack, Lock } from "lucide-react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { use, useState } from "react";

import { EligibilityLock } from "@/components/plan/eligibility-lock";
import { StartPlanDialog } from "@/components/plan/start-plan-dialog";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import {
  advisory,
  stopping,
  type AcceptedSource,
  type PlanEligibility,
  type PlanVersion,
} from "@/lib/api/plan";
import { absoluteTime, relativeTime, shortDate } from "@/lib/format";
import { errorMessage, usePlanEligibility, usePlans, useProject } from "@/lib/queries";

/**
 * `/projects/[id]/plan` — the Stage 02 landing (Stage 02 PRD §15.3 A).
 *
 * Three stacked blocks, and the order is the question a person arrives with:
 * *what would this be planned from*, *can I start*, *what has been planned
 * before*. When nothing has been accepted the first block is the one that is
 * missing, so the page leads with the named reason rather than with an empty
 * table.
 *
 * Every role can read this page. What a `viewer` sees is the same page with
 * one more row in the blocker list saying, in words, that starting is not
 * theirs to do.
 */
export default function PlanLandingPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const project = useProject(id);
  const eligibility = usePlanEligibility(id);
  const plans = usePlans(id);

  const error = errorMessage(eligibility);
  if (error) {
    return (
      <div className="mx-auto w-full max-w-4xl">
        <Alert tone="error" title="Campaign planning could not be loaded">
          {error}
        </Alert>
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-4xl flex-col gap-6">
      <header className="min-w-0">
        {/* The project name is the link back to stage 01, not a splice into
            the sentence below: a long name would swallow what the sentence is
            for, and nothing else on this page says which project it is. */}
        <Link
          href={`/projects/${id}`}
          className="inline-flex max-w-full items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4 shrink-0" aria-hidden />
          <span className="truncate">{project.data?.name ?? "Project"}</span>
        </Link>
        {/* Not truncated: this heading is two fixed words. The variable-length
            thing on this screen is the project name above, which is where the
            truncation belongs — clipping "Campaign planning" at 390px was the
            copied-pattern bug. */}
        <h1 className="mt-2 text-[length:var(--text-xl)] font-semibold tracking-tight">
          Campaign planning
        </h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Turns an accepted research report into a signed-off media plan. Nothing is written to
          the Google Ads account.
        </p>
      </header>

      {eligibility.isPending ? (
        <>
          <Skeleton className="h-40 w-full" />
          <Skeleton className="h-28 w-full" />
        </>
      ) : (
        <>
          <SourceBlock projectId={id} source={eligibility.data?.source ?? null} />
          <ActionBlock projectId={id} eligibility={eligibility.data} />
        </>
      )}

      <HistoryBlock
        projectId={id}
        versions={plans.data?.items ?? []}
        pending={plans.isPending}
      />
    </div>
  );
}

/** What this plan would be built from — or the fact that nothing is. */
function SourceBlock({ projectId, source }: { projectId: string; source: AcceptedSource | null }) {
  if (!source) {
    return (
      <Card>
        <CardHeader
          title="Source research"
          description="The one report a plan is built from."
        />
        <CardBody>
          <p className="text-sm text-fg-muted">
            Nothing accepted yet. A plan reads one finished research report and never re-runs a
            research node, so accepting one is what starts this stage.
          </p>
        </CardBody>
      </Card>
    );
  }

  return (
    <Card>
      <CardHeader
        title="Source research"
        description="The one report this plan is built from."
        actions={<ReadinessBadge readiness={source.launch_readiness} />}
      />
      <CardBody className="space-y-4">
        <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-2">
          <Detail term="Research run">
            {/* The id *and* the way in. A label whose only value is a verb
                reads as a missing field. */}
            <span className="font-mono text-xs" title={source.research_run_id}>
              {source.research_run_id.slice(0, 8)}
            </span>{" "}
            <Link
              href={`/projects/${projectId}/runs/${source.research_run_id}/report`}
              className="text-accent hover:underline"
            >
              Read the report
            </Link>
          </Detail>
          <Detail term="Accepted">
            <span title={absoluteTime(source.accepted_at)}>
              {relativeTime(source.accepted_at)} by {source.accepted_by_name}
            </span>{" "}
            <AgeChip days={source.age_days} />
          </Detail>
          <Detail term="Degraded sources">
            {source.degraded_sources.length ? (
              source.degraded_sources.join(", ")
            ) : (
              <span className="text-fg-subtle">None — every connector answered</span>
            )}
          </Detail>
          <Detail term="Research schema">
            <span className="font-mono text-xs">{source.research_schema_version}</span>
          </Detail>
        </dl>

        {source.override_reason ? (
          <Alert tone="warning" title="Accepted over a no_go verdict">
            {source.override_reason}
          </Alert>
        ) : null}
        {source.note ? (
          <p className="max-w-prose text-sm leading-relaxed text-fg-muted">{source.note}</p>
        ) : null}
      </CardBody>
    </Card>
  );
}

/** The Start button, and everything standing in front of it. */
function ActionBlock({
  projectId,
  eligibility,
}: {
  projectId: string;
  eligibility: PlanEligibility | undefined;
}) {
  const blocking = stopping(eligibility);
  const warnings = advisory(eligibility);
  const source = eligibility?.source;

  return (
    <Card>
      <CardHeader
        title="Start a plan"
        description="Twenty nodes, four approval gates, about twenty minutes of machine time."
      />
      <CardBody className="space-y-4">
        {source && eligibility?.eligible ? (
          <StartPlanDialog projectId={projectId} source={source} />
        ) : (
          <p className="inline-flex items-center gap-2 text-sm text-fg-muted">
            <Lock className="size-4 shrink-0 text-fg-subtle" aria-hidden />
            Campaign planning is locked for this project.
          </p>
        )}
        <EligibilityLock blockers={[...blocking, ...warnings]} />
      </CardBody>
    </Card>
  );
}

/** Every version this project has produced, newest first. */
function HistoryBlock({
  projectId,
  versions,
  pending,
}: {
  projectId: string;
  versions: PlanVersion[];
  pending: boolean;
}) {
  const router = useRouter();
  // Newest first in the list, so `selected[0]` is the newer of the pair and the
  // compare link can put it on the right without asking which is which.
  const [selected, setSelected] = useState<string[]>([]);

  function onSelect(planRunId: string) {
    setSelected((current) =>
      current.includes(planRunId)
        ? current.filter((item) => item !== planRunId)
        : [...current, planRunId].slice(-2),
    );
  }

  return (
    <Card>
      <CardHeader
        title="Plan versions"
        description="Newest first. A frozen plan is immutable."
        actions={
          versions.length >= 2 ? (
            <Button
              variant="secondary"
              disabled={selected.length !== 2}
              title={
                selected.length === 2
                  ? undefined
                  : "Tick two versions to compare them."
              }
              onClick={() =>
                router.push(
                  `/projects/${projectId}/plan/compare?a=${selected[1]}&b=${selected[0]}`,
                )
              }
            >
              <ArrowLeftRight aria-hidden className="size-3.5" />
              Compare
            </Button>
          ) : undefined
        }
      />
      <CardBody className={versions.length ? "p-0" : undefined}>
        {pending ? (
          <Skeleton className="h-20 w-full" />
        ) : versions.length === 0 ? (
          <EmptyState
            icon={FileStack}
            title="No plans yet"
            description="The first plan run writes version 1. Freezing it makes that version immutable; a change after that is a new version from a new run."
          />
        ) : (
          <Table label="Plan versions for this project">
            <thead>
              <Tr>
                <Th>Version</Th>
                <Th>Status</Th>
                <Th>Created</Th>
                <Th>Frozen</Th>
              </Tr>
            </thead>
            <tbody>
              {versions.map((plan) => (
                <Tr key={plan.id}>
                  <Td>
                    <span className="flex items-center gap-2">
                      {/* One checkbox per row, at most two checked, and the
                          Compare button carries the pair. A pair of radio
                          groups would be the other way to say "these two", and
                          it reads as an ordering decision the reader has not
                          been asked to make. */}
                      <input
                        type="checkbox"
                        aria-label={`Compare ${versionName(plan)}`}
                        checked={selected.includes(plan.plan_run_id)}
                        disabled={
                          !selected.includes(plan.plan_run_id) && selected.length >= 2
                        }
                        onChange={() => onSelect(plan.plan_run_id)}
                        className="size-3.5 shrink-0 accent-[var(--accent)] disabled:opacity-40"
                      />
                      <Link
                        href={`/projects/${projectId}/plan/runs/${plan.plan_run_id}/plan`}
                        className="text-accent hover:underline"
                      >
                        {versionName(plan)}
                      </Link>
                    </span>
                    {plan.source_superseded ? (
                      <span className="ml-2 text-xs text-fg-subtle">
                        newer research accepted since
                      </span>
                    ) : null}
                  </Td>
                  <Td>
                    <Badge tone={plan.status === "frozen" ? "accent" : "neutral"}>
                      {PLAN_STATUS_LABEL[plan.status]}
                    </Badge>
                  </Td>
                  <Td className="whitespace-nowrap text-fg-muted" title={absoluteTime(plan.created_at)}>
                    {shortDate(plan.created_at)}
                  </Td>
                  <Td className="whitespace-nowrap text-fg-muted">
                    {plan.frozen_at ? (
                      <span title={absoluteTime(plan.frozen_at)}>
                        {shortDate(plan.frozen_at)}
                        {plan.frozen_by_name ? ` · ${plan.frozen_by_name}` : ""}
                      </span>
                    ) : (
                      <span className="text-fg-subtle">—</span>
                    )}
                  </Td>
                </Tr>
              ))}
            </tbody>
          </Table>
        )}
      </CardBody>
    </Card>
  );
}

/**
 * How old the accepted research is.
 *
 * Amber past 30 days rather than only at the 90-day hard limit: a forecast
 * ages continuously and the chip is the only place that says so before the
 * blocker appears. The exact thresholds are the server's — this renders the
 * `age_days` it returns and nothing more.
 */
function AgeChip({ days }: { days: number }) {
  if (days <= 30) return null;
  return (
    <Badge tone={days > 90 ? "danger" : "warning"}>
      {days} days old
    </Badge>
  );
}

function ReadinessBadge({ readiness }: { readiness: AcceptedSource["launch_readiness"] }) {
  const tone = readiness === "no_go" ? "danger" : readiness === "go" ? "accent" : "warning";
  return <Badge tone={tone}>{READINESS_LABEL[readiness]}</Badge>;
}

function Detail({ term, children }: { term: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs font-medium tracking-wide text-fg-subtle">{term}</dt>
      <dd className="mt-1 text-sm text-fg">{children}</dd>
    </div>
  );
}

const READINESS_LABEL: Record<AcceptedSource["launch_readiness"], string> = {
  go: "Go",
  go_with_fixes: "Go, with fixes",
  no_go: "No go",
};

/**
 * What to call a version that has not been minted yet.
 *
 * Every unfrozen plan sits at version 0, so "v0" would name three different
 * drafts identically. The day it was created is the only thing that tells them
 * apart until one of them is frozen.
 */
function versionName(plan: PlanVersion): string {
  return plan.version > 0 ? `v${plan.version}` : `draft ${shortDate(plan.created_at)}`;
}

const PLAN_STATUS_LABEL: Record<PlanVersion["status"], string> = {
  draft: "Draft",
  blocked: "Blocked",
  ready_to_freeze: "Ready to freeze",
  frozen: "Frozen",
  superseded: "Superseded",
};
