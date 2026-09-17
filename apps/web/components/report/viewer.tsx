"use client";

import { FileText, Info } from "lucide-react";
import { useMemo } from "react";

import { ExportButton } from "@/components/report/export-button";
import { ReadinessBadge } from "@/components/report/readiness-badge";
import {
  AccountSection,
  ActionsSection,
  BusinessSection,
  CompetitionSection,
  DemandSection,
  KeywordsSection,
  QuestionsSection,
  ReadinessSection,
  SECTIONS,
  SummarySection,
} from "@/components/report/sections";
import { Toc } from "@/components/report/toc";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import { SOURCE_LABEL, type EvidenceSource } from "@/lib/api/evidence";
import { citedEvidenceIds } from "@/lib/api/reports";
import { useCitations } from "@/lib/citations";
import { absoluteTime, relativeTime, usd } from "@/lib/format";
import { errorMessage, useReport } from "@/lib/queries";

/**
 * The report viewer (PRD §13.4 C).
 *
 * A reading surface first: one column of text at a readable measure, a table of
 * contents that tracks where you are, and every claim carrying the evidence it
 * rests on. The export button is the only thing on the page that is not the
 * report itself.
 *
 * `Compare with previous run` is not here. It needs `GET /runs/{id}/diff`,
 * which PRD §17 assigns to P8 along with delta runs; rendering a toggle for an
 * endpoint that does not exist would be the one dishonest control on the page.
 */
export function ReportViewer({ runId, projectId }: { runId: string; projectId: string }) {
  const report = useReport(runId);
  const payload = report.data?.payload;
  const citedIds = useMemo(() => (payload ? citedEvidenceIds(payload) : []), [payload]);
  const citations = useCitations(projectId, citedIds);

  if (report.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }

  if (!payload || !report.data) {
    const missing = report.error instanceof ApiError && report.error.status === 404;
    return missing ? (
      <EmptyState
        icon={FileText}
        title="No report for this run"
        description="A report is written by the last node of a full run. This run has not reached it — the console shows where it stopped."
      />
    ) : (
      <Alert tone="error" title="The report could not be loaded">
        {errorMessage(report)}
      </Alert>
    );
  }

  const degraded = payload.degraded_sources ?? [];

  return (
    <div className="flex flex-col gap-6">
      <header className="flex flex-wrap items-start justify-between gap-4 border-b pb-4">
        <div className="min-w-0 space-y-2">
          <ReadinessBadge readiness={payload.launch_readiness} />
          <p className="text-sm text-fg-muted">
            Generated{" "}
            <time
              dateTime={payload.generated_at}
              title={absoluteTime(payload.generated_at)}
              className="text-fg"
            >
              {relativeTime(payload.generated_at)}
            </time>
            {" · "}
            <span data-numeric>{usd(payload.cost_usd ?? 0)}</span> of model spend
            {" · schema "}
            <span className="font-mono text-xs">{payload.schema_version}</span>
          </p>
        </div>
        <ExportButton runId={runId} />
      </header>

      {degraded.length > 0 ? (
        <Alert tone="warning" title="Some sources were incomplete">
          <p>
            {degraded
              .map((source) => SOURCE_LABEL[source as EvidenceSource] ?? source)
              .join(", ")}{" "}
            returned less than a full answer during this run. Everything below still holds — it is
            simply based on less.
          </p>
        </Alert>
      ) : null}

      <div className="flex gap-8">
        <div className="hidden w-48 shrink-0 lg:block">
          <div className="sticky top-4">
            <Toc entries={SECTIONS} />
            <p className="mt-4 flex items-start gap-1.5 border-l py-1 pl-3 text-xs text-fg-subtle">
              <Info className="mt-0.5 size-3 shrink-0" aria-hidden />
              Every numbered chip opens the evidence behind that claim.
            </p>
          </div>
        </div>

        <article className="min-w-0 flex-1 space-y-10">
          <SummarySection report={payload} citations={citations} projectId={projectId} />
          <BusinessSection report={payload} citations={citations} projectId={projectId} />
          <AccountSection report={payload} citations={citations} projectId={projectId} />
          <CompetitionSection report={payload} citations={citations} projectId={projectId} />
          <DemandSection report={payload} citations={citations} projectId={projectId} />
          <ReadinessSection report={payload} citations={citations} projectId={projectId} />
          <KeywordsSection report={payload} citations={citations} projectId={projectId} />
          <ActionsSection report={payload} citations={citations} projectId={projectId} />
          <QuestionsSection report={payload} citations={citations} projectId={projectId} />
        </article>
      </div>
    </div>
  );
}
