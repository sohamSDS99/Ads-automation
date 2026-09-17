"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, use } from "react";

import { ReportViewer } from "@/components/report/viewer";
import { useProject } from "@/lib/queries";

/**
 * `/projects/[id]/runs/[runId]/report` — the Research Report (PRD §13.3).
 *
 * Readable and exportable by every role, `viewer` included: PRD §4.1 grants
 * both to all four.
 */
export default function ReportPage({
  params,
}: {
  params: Promise<{ id: string; runId: string }>;
}) {
  const { id, runId } = use(params);
  const project = useProject(id);

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6">
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <Link
          href={`/projects/${id}/runs/${runId}`}
          className="inline-flex items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4" aria-hidden />
          Run console
        </Link>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">
          {project.data?.name ?? "Research"} report
        </h1>
      </div>

      {/* `useSearchParams` opts the subtree into client-side rendering, so it
          needs a boundary of its own or the whole page renders as a fallback
          during prerender. */}
      <Suspense fallback={null}>
        <ViewerWithCompareParam runId={runId} projectId={id} />
      </Suspense>
    </div>
  );
}

function ViewerWithCompareParam({ runId, projectId }: { runId: string; projectId: string }) {
  const params = useSearchParams();
  return (
    <ReportViewer
      runId={runId}
      projectId={projectId}
      compareByDefault={params.get("compare") === "1"}
    />
  );
}
