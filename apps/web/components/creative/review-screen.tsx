"use client";

import { ArrowLeft, Images } from "lucide-react";
import Link from "next/link";
import { useMemo, useState } from "react";

import { ReviewWorkspace } from "@/components/creative/review-workspace";
import { Alert } from "@/components/ui/alert";
import { buttonVariants } from "@/components/ui/button-variants";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import type { ApprovalDecision, ApprovalItem } from "@/lib/api/approvals";
import { GATE_LABEL } from "@/lib/api/creative";
import { isReviewGate, reviewCard } from "@/lib/api/review";
import { tally, tallyLine, recordedState } from "@/lib/creative/review";
import { errorMessage, useApprovals, useGenerationJobs, useMediaReferences, useProject } from "@/lib/queries";

/** Picks up a G8b opening after 4.4.6, or somebody else recording the gate. */
const REVIEW_POLL_MS = 15_000;

/**
 * The gate this screen is about: an open G8b, else an open G8, else the most
 * recent of the two — so after G8 is recorded with regenerations, the screen
 * moves to G8b by itself when it opens.
 */
function pickGate(items: ApprovalItem[]): ApprovalItem | null {
  const gates = items.filter((item) => isReviewGate(item.gate_key));
  const open = (key: string) => gates.find((item) => item.gate_key === key && item.status === "pending");
  return (
    open("G8b") ??
    open("G8") ??
    [...gates].sort((a, b) => b.created_at.localeCompare(a.created_at))[0] ??
    null
  );
}

/** "Approve 14 · Reject 2 · Regenerate 3", as the decided card recorded it. */
function recordedLine(approval: ApprovalItem): string {
  const items = reviewCard(approval).items;
  const counts = tally(recordedState(items), items.map((item) => item.asset_id));
  return tallyLine(counts, approval.gate_key === "G8b" ? "G8b" : "G8");
}

/**
 * `/projects/[id]/creative/runs/[runId]/review` (Stage 04 PRD §15.3, §15.4 H):
 * the G8 / G8b review workspace. Any role looks; the decide controls exist
 * for the gate's decider alone, as the approvals surface says (`can_decide`).
 */
export function ReviewScreen({ projectId, runId }: { projectId: string; runId: string }) {
  const project = useProject(projectId);
  const approvals = useApprovals({ run_id: runId }, { pollMs: REVIEW_POLL_MS });
  const approval = useMemo(() => pickGate(approvals.data?.items ?? []), [approvals.data]);
  const references = useMediaReferences(projectId, Boolean(approval));
  const jobs = useGenerationJobs(runId, { enabled: Boolean(approval) });
  const [recorded, setRecorded] = useState<ApprovalDecision | null>(null);

  const models = useMemo(() => {
    const byAsset = new Map<string, string>();
    const newestFirst = [...(jobs.data?.items ?? [])].sort((a, b) => b.created_at.localeCompare(a.created_at));
    for (const job of newestFirst) {
      if (job.asset_id && !byAsset.has(job.asset_id)) byAsset.set(job.asset_id, job.model_id);
    }
    return byAsset;
  }, [jobs.data]);

  const home = `/projects/${projectId}/creative/runs/${runId}`;
  const card = approval ? reviewCard(approval) : null;
  const title = approval ? (GATE_LABEL[approval.gate_key] ?? approval.gate_key) : "Media review";

  return (
    <div className="flex flex-col gap-3 lg:h-main lg:min-h-0">
      <Link
        href={home}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4 shrink-0" aria-hidden />
        <span className="truncate">{project.data?.name ?? "Creative console"} · console</span>
      </Link>

      <header className="flex flex-wrap items-end justify-between gap-x-6 gap-y-1">
        <h1 className="text-xl font-semibold tracking-tight text-fg">{title}</h1>
        {approval && card ? (
          <p className="text-sm tabular-nums text-fg-muted">
            {approval.gate_key} · round {card.round} · {card.items.length}{" "}
            {card.items.length === 1 ? "asset" : "assets"} ·{" "}
            {approval.status === "pending"
              ? `waiting on ${approval.assignee_email ?? "the brand owner"}`
              : approval.status}
          </p>
        ) : null}
      </header>

      {recorded ? (
        <Alert tone="info" title={`${GATE_LABEL[recorded.approval.gate_key] ?? recorded.approval.gate_key} recorded`}>
          {recordedLine(recorded.approval)}.{" "}
          {recorded.resumed
            ? "The run resumed."
            : `The run is ${recorded.run_status.replaceAll("_", " ")}.`}
        </Alert>
      ) : null}

      {approvals.isPending ? <Skeleton className="h-96 w-full lg:flex-1" /> : null}
      {approvals.isError ? (
        <Alert tone="error" title="The review could not be read">
          {errorMessage(approvals)}
        </Alert>
      ) : null}

      {approvals.data && !approval ? (
        <EmptyState
          icon={Images}
          title="Media review has not opened"
          description="G8 opens once this run has made its images and videos (4.4.2–4.4.4). Every AI-made asset is then reviewed here, one at a time."
          action={
            <Link href={home} className={buttonVariants({ variant: "secondary", size: "sm" })}>
              Open the console
            </Link>
          }
        />
      ) : null}

      {approval && card?.status === "not_required" ? (
        <EmptyState
          icon={Images}
          title={`${title} was not needed`}
          description={card.why ?? "There was nothing AI-made to review."}
        />
      ) : null}

      {approval && card?.status === "review" && card.items.length > 0 ? (
        <ReviewWorkspace
          key={`${approval.id}:${approval.status}`}
          approval={approval}
          references={references.data ?? []}
          referencesError={references.isError ? errorMessage(references) : null}
          models={models}
          onRecorded={(result) => {
            setRecorded(result);
            void approvals.refetch();
          }}
        />
      ) : null}
    </div>
  );
}
