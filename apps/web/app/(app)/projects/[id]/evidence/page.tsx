"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, use } from "react";

import { EvidenceExplorer } from "@/components/evidence/explorer";
import { Skeleton } from "@/components/ui/skeleton";
import type { EvidenceSource } from "@/lib/api/evidence";
import { useProject } from "@/lib/queries";

/**
 * `/projects/[id]/evidence` — the explorer, pinned to one project (PRD §13.3).
 *
 * This is where a citation in a report lands: same screen, same filters, with
 * the cited ids already applied.
 */
export default function ProjectEvidencePage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const project = useProject(id);

  return (
    <div className="mx-auto flex h-full max-w-6xl flex-col gap-6">
      <div>
        <Link
          href={`/projects/${id}`}
          className="inline-flex items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4" aria-hidden />
          {project.data?.name ?? "Project"}
        </Link>
      </div>

      <header>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Evidence</h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Everything gathered for {project.data?.name ?? "this project"}, searchable by exact term
          and by meaning.
        </p>
      </header>

      <Suspense fallback={<Skeleton className="h-64 w-full" />}>
        <ScopedExplorer projectId={id} />
      </Suspense>
    </div>
  );
}

function ScopedExplorer({ projectId }: { projectId: string }) {
  const params = useSearchParams();
  const ids = params.getAll("ids");
  return (
    <EvidenceExplorer
      projectId={projectId}
      className="min-h-0 flex-1"
      initial={{
        ids: ids.length > 0 ? ids : undefined,
        runId: params.get("run_id") ?? undefined,
        source: (params.get("source") as EvidenceSource | null) ?? undefined,
      }}
    />
  );
}
