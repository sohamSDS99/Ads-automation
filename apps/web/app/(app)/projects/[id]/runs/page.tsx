"use client";

import { ArrowLeft, GitCompare, History } from "lucide-react";
import Link from "next/link";
import { use, useEffect, useState } from "react";

import { RunTrigger } from "@/components/projects/run-trigger";
import { Alert } from "@/components/ui/alert";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill } from "@/components/ui/status-pill";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import type { RunSummary } from "@/lib/api/projects";
import { absoluteTime, duration, relativeTime, usd } from "@/lib/format";
import { errorMessage, useProject, useProjectRuns } from "@/lib/queries";
import { Can } from "@/lib/session";
import { cn } from "@/lib/utils";

export default function ProjectRunsPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const project = useProject(id);
  const runs = useProjectRuns(id);
  const error = errorMessage(project) ?? errorMessage(runs);
  const highlighted = useHashTarget();

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6">
      <div>
        <Link
          href={`/projects/${id}`}
          className="inline-flex items-center gap-1.5 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          <ArrowLeft className="size-4" aria-hidden />
          {project.data?.name ?? "Project"}
        </Link>
      </div>

      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Run history</h1>
          <p className="mt-1 text-sm text-fg-muted">
            Every research run against this project, newest first.
          </p>
        </div>
        {project.data ? (
          <Can permission="run_execute">
            <RunTrigger project={project.data} />
          </Can>
        ) : null}
      </header>

      {error ? (
        <Alert tone="error" title="Run history could not be loaded">
          {error}
        </Alert>
      ) : null}

      {runs.isPending ? <Skeleton className="h-48 w-full" /> : null}

      {runs.data?.runs.length === 0 ? (
        <EmptyState
          icon={History}
          title="No runs yet"
          description="Once a run starts it appears here with its status, who launched it and what it cost."
        />
      ) : null}

      {runs.data && runs.data.runs.length > 0 ? (
        <Card className="overflow-hidden">
          <Table label="Run history">
            <thead>
              <tr>
                <Th>Status</Th>
                <Th>Started</Th>
                <Th>Triggered by</Th>
                <Th>Duration</Th>
                <Th className="text-right">Cost</Th>
                <Th>Outcome</Th>
              </tr>
            </thead>
            <tbody>
              {runs.data.runs.map((run) => (
                <RunRow key={run.id} run={run} highlighted={highlighted === run.id} />
              ))}
            </tbody>
          </Table>
        </Card>
      ) : null}

      <p className="text-xs text-fg-subtle">
        Open a run to follow it node by node, read what each one produced, and decide the gates it
        stops on.
      </p>
    </div>
  );
}

function RunRow({ run, highlighted }: { run: RunSummary; highlighted: boolean }) {
  const error = run.error as { message?: string; code?: string } | null;
  return (
    <Tr
      className={cn(
        "scroll-mt-20 transition-colors",
        // The row a launch or a lock conflict just pointed at. Tinted rather
        // than outlined so it does not read as an error state.
        highlighted && "bg-accent-soft hover:bg-accent-soft",
      )}
    >
      <Td className="whitespace-nowrap">
        <Link
          id={`run-${run.id}`}
          href={`/projects/${run.project_id}/runs/${run.id}`}
          className="block rounded-[var(--radius)]"
        >
          <StatusPill status={run.status} />
          <span className="sr-only">Open the console for this run</span>
        </Link>
      </Td>
      <Td className="whitespace-nowrap text-fg-muted" title={absoluteTime(run.started_at)}>
        {relativeTime(run.started_at)}
      </Td>
      <Td className="whitespace-nowrap">
        {run.trigger === "schedule" ? (
          <span className="text-fg-muted">Schedule</span>
        ) : (
          (run.triggered_by_name ?? <span className="text-fg-subtle">Unknown</span>)
        )}
      </Td>
      <Td data-numeric className="whitespace-nowrap text-fg-muted">
        {duration(run.started_at, run.finished_at)}
      </Td>
      <Td data-numeric className="whitespace-nowrap text-right">
        {usd(run.cost_usd)}
      </Td>
      <Td className="max-w-80">
        {error ? (
          <span className="text-fg-muted">{error.message ?? error.code ?? "Failed"}</span>
        ) : run.status === "succeeded" ? (
          <span className="flex flex-wrap items-baseline gap-x-3">
            <Link
              href={`/projects/${run.project_id}/runs/${run.id}/report`}
              className="text-accent hover:underline"
            >
              Report
            </Link>
            {/* PRD §13.3's diff-vs-previous column. Offered only when there is
                a previous run to diff against, rather than shown disabled —
                a control that can never do anything is noise in every row of
                a project's first week. */}
            {run.parent_run_id ? (
              <Link
                href={`/projects/${run.project_id}/runs/${run.id}/report?compare=1`}
                className="inline-flex items-center gap-1 text-sm text-fg-muted hover:text-fg"
              >
                <GitCompare className="size-3.5" aria-hidden />
                Changes
              </Link>
            ) : null}
          </span>
        ) : (
          <span className="text-fg-subtle">
            {run.mode === "partial" ? "Partial run" : "Full run"}
          </span>
        )}
      </Td>
    </Tr>
  );
}

/**
 * The run id in the URL fragment, if there is one.
 *
 * A launch and a lock conflict both send people here pointing at one row, and
 * `window.location.hash` is not available during the server render — reading it
 * in an effect is what keeps the first paint identical on both sides.
 */
function useHashTarget(): string | null {
  const [target, setTarget] = useState<string | null>(null);
  useEffect(() => {
    const read = () => setTarget(window.location.hash.replace(/^#run-/, "") || null);
    read();
    window.addEventListener("hashchange", read);
    return () => window.removeEventListener("hashchange", read);
  }, []);
  return target;
}
