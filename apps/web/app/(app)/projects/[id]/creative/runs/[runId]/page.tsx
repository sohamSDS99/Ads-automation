"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { RunConsole } from "@/components/run/console";
import { useProject } from "@/lib/queries";

/**
 * `/projects/[id]/creative/runs/[runId]` — the Creative Console (Stage 04 PRD
 * §15.3, §15.4 C): the Stage 01 Run Console with `stage="creative"`. The rail,
 * the DAG and the node panel render whatever the api returns; the stage adds
 * the media lane, the Assets and Jobs tabs and the two spend meters.
 *
 * The only route that draws the DAG, so the only one that loads `reactflow`
 * (§15.5 item 7): the brief page and the landing never import the canvas.
 */
export default function CreativeConsolePage({
  params,
}: {
  params: Promise<{ id: string; runId: string }>;
}) {
  const { id, runId } = use(params);
  const project = useProject(id);

  // `xl:h-main`, not `h-full`: the project layout above has no height of its
  // own, so `h-full` resolves to auto and a 24-node rail stretched the console
  // — and the canvas, and its fitted graph — far below the fold. On a phone the
  // console stacks and grows with its content, as every other console does.
  return (
    <div className="flex flex-col gap-3 xl:h-main">
      {/* Back to the Stage 04 landing: where this run was started and where
          its packages will be listed. */}
      <Link
        href={`/projects/${id}/creative`}
        className="inline-flex w-fit items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
      >
        <ArrowLeft className="size-4 shrink-0" aria-hidden />
        <span className="truncate">{project.data?.name ?? "Copy & creative"}</span>
      </Link>
      <div className="min-h-0 flex-1">
        <RunConsole runId={runId} projectId={id} stage="creative" />
      </div>
    </div>
  );
}
