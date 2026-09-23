"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { Rulebook } from "@/components/guidelines/rulebook";
import { useProject } from "@/lib/queries";

/**
 * `/projects/[id]/guidelines/runs/[runId]/rulebook` — the draft (PRD §15.3 E).
 *
 * The same viewer as `/published`, reading the guideline this run produced.
 * Readable while the run is still going: a section whose node has not run says
 * so, which is what somebody standing at G5 needs to see.
 */
export default function DraftRulebookPage({
  params,
}: {
  params: Promise<{ id: string; runId: string }>;
}) {
  const { id, runId } = use(params);
  const project = useProject(id);

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-4">
      <Link
        href={`/projects/${id}/guidelines/runs/${runId}`}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4 shrink-0" aria-hidden />
        <span className="truncate">{project.data?.name ?? "Back to the run"}</span>
      </Link>
      <Rulebook projectId={id} runId={runId} mode="draft" />
    </div>
  );
}
