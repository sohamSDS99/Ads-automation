"use client";

import { FolderKanban } from "lucide-react";
import Link from "next/link";
import type { ReactNode } from "react";

import { EmptyState } from "@/components/ui/empty-state";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { buttonVariants } from "@/components/ui/button-variants";
import type { ProjectSummary } from "@/lib/api/projects";
import { cn } from "@/lib/utils";

/**
 * Which project the tab below is editing.
 *
 * Two of these tabs configure a project rather than the workspace, and the
 * honest way to put them in Settings is to say so and let the person choose,
 * rather than to pretend a workspace has one brand. Nothing is asked when
 * there is nothing to ask: one project renders as a line of text, because a
 * picker with one option is a control that does nothing.
 *
 * The choice is remembered across tabs and mirrored into `?project=`, so
 * moving between business context and approvers does not ask twice.
 */
export function ProjectPicker({
  projects,
  projectId,
  onSelect,
  loading,
}: {
  projects: ProjectSummary[] | undefined;
  projectId: string | null;
  onSelect: (id: string) => void;
  loading: boolean;
}) {
  if (loading) return <Skeleton className="h-14 w-full" />;

  const selected = projects?.find((project) => project.id === projectId);

  return (
    <div className="flex flex-wrap items-center justify-between gap-3 rounded-[var(--radius)] border bg-surface px-4 py-3">
      <div className="flex min-w-0 items-center gap-3">
        <FolderKanban className="size-4 shrink-0 text-fg-subtle" aria-hidden />
        {projects && projects.length > 1 ? (
          <>
            <label htmlFor="settings-project" className="shrink-0 text-sm font-medium text-fg">
              Project
            </label>
            <Select
              id="settings-project"
              value={projectId ?? ""}
              onChange={(event) => onSelect(event.target.value)}
              className="w-56"
            >
              {projects.map((project) => (
                <option key={project.id} value={project.id}>
                  {project.name}
                </option>
              ))}
            </Select>
          </>
        ) : (
          <p className="min-w-0 truncate text-sm text-fg">
            <span className="font-medium">{selected?.name ?? "No project"}</span>
            {selected ? (
              <span className="ml-2 font-mono text-xs text-fg-subtle">{selected.domain}</span>
            ) : null}
          </p>
        )}
      </div>

      {selected ? (
        <Link
          href={`/projects/${selected.id}`}
          className="shrink-0 text-sm text-fg-muted transition-colors hover:text-fg"
        >
          Open project →
        </Link>
      ) : null}
    </div>
  );
}

/**
 * What a project-scoped tab shows when the workspace has no projects.
 *
 * Its own component because both tabs need it and both were otherwise going to
 * render an empty form against a project that does not exist.
 */
export function NoProjects({ what }: { what: string }) {
  return (
    <EmptyState
      icon={FolderKanban}
      title="No projects yet"
      description={`${what} belongs to a project, and this workspace does not have one. Create a project and this tab fills in.`}
      action={
        <Link href="/" className={cn(buttonVariants())}>
          Go to projects
        </Link>
      }
    />
  );
}

/** The shell every project-scoped tab shares: the picker, then its content. */
export function ProjectScoped({
  projects,
  projectId,
  onSelect,
  loading,
  what,
  children,
}: {
  projects: ProjectSummary[] | undefined;
  projectId: string | null;
  onSelect: (id: string) => void;
  loading: boolean;
  /** What this tab configures, for the empty state's sentence. */
  what: string;
  children: ReactNode;
}) {
  return (
    <div className="flex flex-col gap-5">
      <ProjectPicker
        projects={projects}
        projectId={projectId}
        onSelect={onSelect}
        loading={loading}
      />
      {!loading && !projects?.length ? <NoProjects what={what} /> : children}
    </div>
  );
}
