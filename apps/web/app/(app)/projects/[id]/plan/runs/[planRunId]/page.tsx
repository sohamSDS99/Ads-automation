"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { RunConsole } from "@/components/run/console";
import { useProject } from "@/lib/queries";

/**
 * `/projects/[id]/plan/runs/[planRunId]` — the Plan Console (Stage 02 PRD §15.3 B).
 *
 * The Stage 01 Run Console with `stage="plan"`, which is the whole point:
 * §22 law 19 says reuse the executor, the SSE channel and the approvals inbox
 * as they are. The rail, the DAG and the node panel already render whatever
 * the API returns, so a plan run needs no second console — it needs the same
 * one told which pipeline it is looking at.
 *
 * Read-only for a `viewer` by construction, exactly as Stage 01: every control
 * inside is wrapped in the permission it needs and the API refuses the rest.
 */
export default function PlanConsolePage({
  params,
}: {
  params: Promise<{ id: string; planRunId: string }>;
}) {
  const { id, planRunId } = use(params);
  const project = useProject(id);

  return (
    <div className="flex h-full flex-col gap-3">
      {/* Back to the Stage 02 landing, not to the project: that is where this
          run was started from and where its version history lives. */}
      <Link
        href={`/projects/${id}/plan`}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4 shrink-0" aria-hidden />
        <span className="truncate">{project.data?.name ?? "Campaign planning"}</span>
      </Link>
      <div className="min-h-0 flex-1">
        <RunConsole runId={planRunId} projectId={id} stage="plan" />
      </div>
    </div>
  );
}
