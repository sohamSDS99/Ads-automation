"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { RunConsole } from "@/components/run/console";
import { useProject } from "@/lib/queries";

/**
 * `/projects/[id]/runs/[runId]` — the run console (PRD §13.3).
 *
 * Read-only for a `viewer` by construction: every control inside is wrapped in
 * the permission it needs, and the API refuses the rest regardless.
 */
export default function RunConsolePage({
  params,
}: {
  params: Promise<{ id: string; runId: string }>;
}) {
  const { id, runId } = use(params);
  const project = useProject(id);

  return (
    <div className="flex h-full flex-col gap-3">
      <Link
        href={`/projects/${id}/runs`}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4" aria-hidden />
        {project.data?.name ?? "Runs"}
      </Link>
      <div className="min-h-0 flex-1">
        <RunConsole runId={runId} projectId={id} />
      </div>
    </div>
  );
}
