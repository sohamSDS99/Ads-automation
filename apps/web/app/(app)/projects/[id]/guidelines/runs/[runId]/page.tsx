"use client";

import { ArrowLeft, BookOpen } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { RunConsole } from "@/components/run/console";
import { buttonVariants } from "@/components/ui/button-variants";
import { useProject } from "@/lib/queries";

/**
 * `/projects/[id]/guidelines/runs/[runId]` — the Guideline Console (PRD §15.3 B).
 *
 * The Stage 01 Run Console with `stage="guideline"`, for the same reason the
 * Plan Console is: the rail, the DAG and the node panel already render
 * whatever the API returns, so a third pipeline needs the same console told
 * which one it is looking at — not a third console.
 *
 * What `stage` changes here is one thing: the node panel gains its **Rules**
 * tab, which is where a guideline node's actual output lives. Everything else
 * — the SSE stream, the gate cards, the evidence tab, the cost metrics — is
 * Stage 01's, unchanged.
 *
 * Read-only for a `viewer` by construction: every control inside is wrapped in
 * the permission it needs and the API refuses the rest.
 */
export default function GuidelineConsolePage({
  params,
}: {
  params: Promise<{ id: string; runId: string }>;
}) {
  const { id, runId } = use(params);
  const project = useProject(id);

  return (
    <div className="flex h-full flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        {/* Back to the Stage 03 landing, not to the project: that is where
            this run was started and where its version history lives. */}
        <Link
          href={`/projects/${id}/guidelines`}
          className="inline-flex min-w-0 items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4 shrink-0" aria-hidden />
          <span className="truncate">{project.data?.name ?? "Content guidelines"}</span>
        </Link>
        {/* Offered from the moment the console opens rather than once the run
            finishes. The rulebook is readable while the run is still going —
            it is how somebody answering G5 sees what they are confirming — and
            the page behind this link says plainly how much of it exists yet. */}
        <Link
          href={`/projects/${id}/guidelines/runs/${runId}/rulebook`}
          className={buttonVariants({ variant: "secondary", size: "sm" })}
        >
          <BookOpen className="size-4" aria-hidden />
          Read the draft rulebook
        </Link>
      </div>
      <div className="min-h-0 flex-1">
        <RunConsole runId={runId} projectId={id} stage="guideline" />
      </div>
    </div>
  );
}
