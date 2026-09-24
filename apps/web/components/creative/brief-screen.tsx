"use client";

import { ArrowLeft, FileClock } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { BriefDocument } from "@/components/creative/brief-document";
import { BriefGateCard } from "@/components/creative/brief-gate-card";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { ApiError } from "@/lib/api";
import type { BriefEdits } from "@/lib/api/creative-runs";
import { errorMessage, useApprovals, useCreativeBrief } from "@/lib/queries";

/**
 * The brief page (Stage 04 PRD §15.3 `…/runs/[runId]/brief`, §15.4 D): the
 * document on the left at its reading measure, G7's card in a sticky column
 * on the right. On a phone the card comes first, so what approving
 * authorises is read before the document it authorises.
 *
 * Whether anyone may decide is the approvals surface's `can_decide` on the
 * run's G7 item — read here and handed to the card, never re-derived.
 */
export function BriefScreen({ projectId, runId }: { projectId: string; runId: string }) {
  const brief = useCreativeBrief(runId);
  const approvals = useApprovals({ run_id: runId });
  const [edits, setEdits] = useState<BriefEdits | null>(null);

  const g7 =
    approvals.data?.items
      .filter((item) => item.gate_key === "G7")
      .sort((a, b) => b.created_at.localeCompare(a.created_at))[0] ?? null;
  const consoleHref = `/projects/${projectId}/creative/runs/${runId}`;

  const back = (
    <Link
      href={consoleHref}
      className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
    >
      <ArrowLeft className="size-4 shrink-0" aria-hidden />
      Back to the Creative Console
    </Link>
  );

  if (brief.isPending || approvals.isPending) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <div className="flex flex-col gap-6 lg:flex-row lg:items-start">
          <div className="flex min-w-0 flex-1 flex-col gap-4">
            <Skeleton className="h-10 w-2/3" />
            <Skeleton className="h-40 w-full max-w-measure" />
            <Skeleton className="h-40 w-full max-w-measure" />
          </div>
          <Skeleton className="order-first h-64 w-full lg:order-last lg:w-96" />
        </div>
      </div>
    );
  }

  if (brief.error instanceof ApiError && brief.error.status === 404) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <EmptyState
          icon={FileClock}
          title="The brief is not written yet"
          description="Node 4.1.1 writes it first, from the frozen plan and the published ruleset. It appears here the moment that node finishes, and G7 opens with it."
        />
      </div>
    );
  }

  if (!brief.data) {
    return (
      <div className="flex flex-col gap-6">
        {back}
        <Alert tone="error" title="The brief could not be loaded">
          {errorMessage(brief) ?? "Refresh the page to try again."}
        </Alert>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      {back}
      <div className="flex flex-col gap-8 lg:flex-row lg:items-start">
        <div className="min-w-0 flex-1">
          <BriefDocument
            view={brief.data}
            projectId={projectId}
            edits={edits}
            onEdit={(path, text) => setEdits((current) => ({ ...(current ?? {}), [path]: text }))}
          />
        </div>
        <aside className="order-first w-full shrink-0 lg:sticky lg:top-6 lg:order-last lg:w-96">
          <BriefGateCard
            view={brief.data}
            approval={g7}
            edits={edits}
            onEdit={() => setEdits({})}
            onDiscard={() => setEdits(null)}
          />
        </aside>
      </div>
    </div>
  );
}
