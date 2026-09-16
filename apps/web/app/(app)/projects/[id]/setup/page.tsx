"use client";

import { ArrowLeft } from "lucide-react";
import Link from "next/link";
import { use } from "react";

import { SetupWizard } from "@/components/setup/wizard";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage, useProject } from "@/lib/queries";
import { Guarded } from "@/components/auth/guarded";

export default function ProjectSetupPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params);
  const project = useProject(id);
  const error = errorMessage(project);

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6">
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
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Set up</h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Five steps, saved as you go. Leave whenever — what you have entered is kept.
        </p>
      </header>

      {error ? (
        <Alert tone="error" title="This project could not be loaded">
          {error}
        </Alert>
      ) : null}

      {project.isPending ? <Skeleton className="h-96 w-full" /> : null}

      {project.data ? (
        // The wizard writes through several endpoints; `project_write` is the
        // permission all of them have in common, and the API checks each one
        // again regardless (PRD §18 law 6).
        <Guarded permission="project_write" what="project setup">
          <SetupWizard project={project.data} />
        </Guarded>
      ) : null}
    </div>
  );
}
