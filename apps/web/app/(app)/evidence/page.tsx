"use client";

import { useSearchParams } from "next/navigation";
import { Suspense } from "react";

import { EvidenceExplorer } from "@/components/evidence/explorer";
import { Skeleton } from "@/components/ui/skeleton";
import type { EvidenceSource } from "@/lib/api/evidence";

/**
 * `/evidence` — every fact this workspace holds, across projects.
 *
 * The project-scoped twin at `/projects/[id]/evidence` is the same explorer
 * with one filter pinned. PRD §13.3 names only the scoped route; this one is
 * where the navigation every role sees has always pointed, and a citation from
 * a report can land in either.
 */
export default function EvidencePage() {
  return (
    <div className="mx-auto flex h-full max-w-6xl flex-col gap-6">
      <header>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Evidence</h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Every fact a run works from is stored with its source and the time it was fetched. No
          claim in a report exists without at least one row here behind it.
        </p>
      </header>

      <Suspense fallback={<Skeleton className="h-64 w-full" />}>
        <FromUrl />
      </Suspense>
    </div>
  );
}

/**
 * The explorer, opened on whatever the link asked for.
 *
 * Its own component because `useSearchParams` suspends, and suspending the
 * whole page would blank the heading every time a citation is followed.
 */
function FromUrl() {
  const params = useSearchParams();
  const ids = params.getAll("ids");
  return (
    <EvidenceExplorer
      className="min-h-0 flex-1"
      initial={{
        ids: ids.length > 0 ? ids : undefined,
        runId: params.get("run_id") ?? undefined,
        source: (params.get("source") as EvidenceSource | null) ?? undefined,
      }}
    />
  );
}
