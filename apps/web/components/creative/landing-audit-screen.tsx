"use client";

import { ArrowLeft, FileClock } from "lucide-react";
import Link from "next/link";

import { LandingAuditCard } from "@/components/creative/landing-audit-card";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import type { LandingAuditVerdict } from "@/lib/api/creative-runs";
import { useLandingAudits } from "@/lib/queries";

const ORDER: LandingAuditVerdict[] = ["blocking_for_launch", "unreachable", "needs_change", "ok"];
const COUNT_LABEL: Record<LandingAuditVerdict, string> = {
  blocking_for_launch: "block launch",
  unreachable: "unreachable",
  needs_change: "need a change",
  ok: "OK",
};

/**
 * The Landing audit (Stage 04 PRD §15.3 `…/runs/[runId]/landing`, §15.4 J):
 * one `LandingAuditCard` per landing URL, in the order 4.5.1 audited them.
 * Read-only for every role — Stage 04 changes no website (law 41); what a
 * card offers is the patch to hand over.
 */
export function LandingAuditScreen({ projectId, runId }: { projectId: string; runId: string }) {
  const audits = useLandingAudits(runId);
  const back = (
    <Link
      href={`/projects/${projectId}/creative/runs/${runId}`}
      className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
    >
      <ArrowLeft className="size-4 shrink-0" aria-hidden />
      Back to the Creative Console
    </Link>
  );

  if (audits.isPending) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <Skeleton className="h-8 w-48" />
        <Skeleton className="h-96 w-full" />
      </div>
    );
  }
  if (audits.error) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <Alert tone="error" title="The landing audits could not be loaded">
          {audits.error instanceof ApiError ? audits.error.detail : "Refresh the page to try again."}
        </Alert>
      </div>
    );
  }
  const items = audits.data.items;
  if (items.length === 0) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <EmptyState
          icon={FileClock}
          title="No landing pages are audited yet"
          description="Nodes 4.5.1 and 4.5.2 render each ad group's landing page on a phone and a desktop once the copy is written, and audit the H1, the offer and the form. Their findings appear here as they finish."
          action={
            <Link href={`/projects/${projectId}/creative/runs/${runId}`} className="text-sm text-accent hover:underline">
              Follow the run in the console
            </Link>
          }
        />
      </div>
    );
  }
  const counts = ORDER.map((verdict) => [verdict, items.filter((item) => item.verdict === verdict).length] as const).filter(
    ([, count]) => count > 0,
  );

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-col gap-4">
        {back}
        <div>
          <h1 className="text-lg font-medium tracking-tight text-fg">Landing audit</h1>
          <p className="text-sm text-fg-muted tabular-nums">
            {items.length} {items.length === 1 ? "page" : "pages"} ·{" "}
            {counts.map(([verdict, count]) => `${count} ${COUNT_LABEL[verdict]}`).join(" · ")}
          </p>
        </div>
      </div>
      <div className="flex flex-col gap-6">
        {items.map((audit) => (
          <LandingAuditCard key={audit.id} audit={audit} />
        ))}
      </div>
    </div>
  );
}
