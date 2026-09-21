"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { PlanViewer } from "@/components/plan/plan-viewer";

/**
 * `/projects/[id]/plan/runs/[planRunId]/plan` — the Plan Viewer (§15.2, §15.3 D).
 *
 * A sibling of the console rather than a tab inside it. The console is a live
 * surface — a rail, a DAG, an SSE stream — and the plan is a document; putting
 * a document inside a console means the reader loses their place in it every
 * time a node finishes.
 */
export default function PlanViewerPage({
  params,
}: {
  params: Promise<{ id: string; planRunId: string }>;
}) {
  const { id, planRunId } = use(params);

  return (
    <div className="space-y-3">
      <Link
        href={`/projects/${id}/plan/runs/${planRunId}`}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4 shrink-0" aria-hidden />
        <span className="truncate">Back to the plan run</span>
      </Link>
      <PlanViewer planRunId={planRunId} projectId={id} />
    </div>
  );
}
