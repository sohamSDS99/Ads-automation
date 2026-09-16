"use client";

import { ChevronRight, FolderPlus } from "lucide-react";
import Link from "next/link";

import { NewProjectDialog } from "@/components/projects/new-project-dialog";
import { Alert } from "@/components/ui/alert";
import { Card } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill } from "@/components/ui/status-pill";
import type { ProjectSummary } from "@/lib/api/projects";
import { absoluteTime, relativeTime, usd } from "@/lib/format";
import { errorMessage, useProjects } from "@/lib/queries";
import { Can, useSession } from "@/lib/session";

export default function ProjectsPage() {
  const projects = useProjects();
  const { has } = useSession();
  const error = errorMessage(projects);

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6">
      <header className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Projects</h1>
          <p className="mt-1 max-w-prose text-sm text-fg-muted">
            A project is one brand, one set of markets, and the evidence sources a research run
            draws on.
          </p>
        </div>
        <Can permission="project_write">
          <NewProjectDialog />
        </Can>
      </header>

      {error ? (
        <Alert tone="error" title="Projects could not be loaded">
          {error}
        </Alert>
      ) : null}

      {projects.isPending ? (
        <Card>
          <ul>
            {[0, 1, 2].map((row) => (
              <li key={row} className="flex items-center gap-4 border-b px-4 py-4 last:border-b-0">
                <Skeleton className="h-4 w-48" />
                <Skeleton className="ml-auto h-4 w-24" />
              </li>
            ))}
          </ul>
        </Card>
      ) : null}

      {projects.data?.projects.length === 0 ? (
        <EmptyState
          icon={FolderPlus}
          title="No projects yet"
          description={
            has("project_write")
              ? "Create one to describe a brand, connect its data sources, and launch the first research run."
              : "Nothing has been created in this workspace yet. An operator or admin starts a project."
          }
          action={
            <Can permission="project_write">
              <NewProjectDialog />
            </Can>
          }
        />
      ) : null}

      {projects.data && projects.data.projects.length > 0 ? (
        <Card>
          <ul>
            {projects.data.projects.map((project) => (
              <ProjectRow key={project.id} project={project} />
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}

function ProjectRow({ project }: { project: ProjectSummary }) {
  const run = project.last_run;
  return (
    <li className="border-b last:border-b-0">
      <Link
        href={`/projects/${project.id}`}
        className="group flex items-center gap-4 px-4 py-3.5 transition-colors hover:bg-surface-hover"
      >
        <div className="min-w-0 flex-1">
          <p className="truncate font-medium text-fg">{project.name}</p>
          <p className="truncate font-mono text-xs text-fg-subtle">{project.domain}</p>
        </div>

        <div className="hidden w-40 shrink-0 sm:block">
          {run ? (
            <>
              <StatusPill status={run.status} />
              <p className="mt-1 text-xs text-fg-subtle" title={absoluteTime(run.started_at)}>
                {relativeTime(run.started_at ?? null)}
              </p>
            </>
          ) : (
            <p className="text-xs text-fg-subtle">Never run</p>
          )}
        </div>

        <div className="hidden w-24 shrink-0 text-right md:block">
          <p data-numeric className="text-sm text-fg">
            {usd(run?.cost_usd ?? null)}
          </p>
          <p className="text-xs text-fg-subtle">
            {project.run_count} {project.run_count === 1 ? "run" : "runs"}
          </p>
        </div>

        <ChevronRight
          aria-hidden
          className="size-4 shrink-0 text-fg-subtle transition-transform group-hover:translate-x-0.5"
        />
      </Link>
    </li>
  );
}
