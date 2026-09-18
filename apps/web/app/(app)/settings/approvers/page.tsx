"use client";

import { ApproversEditor, type GateDraft } from "@/components/settings/approvers-editor";
import { ProjectForm } from "@/components/settings/project-form";
import { ProjectScoped } from "@/components/settings/project-picker";
import { Alert } from "@/components/ui/alert";
import { Skeleton } from "@/components/ui/skeleton";
import type { ProjectDetail } from "@/lib/api/projects";
import { errorMessage, useProject, useProjects } from "@/lib/queries";
import { useSession } from "@/lib/session";
import { useSelectedProject } from "@/lib/stores/settings";

/**
 * Who decides this project's three gates.
 *
 * A gate is never decided automatically. If nobody answers, the run waits —
 * which is why leaving one unassigned is a real choice rather than an empty
 * field: it goes to whoever holds the approver role and gets there first.
 */
export default function ApproversPage() {
  const { has } = useSession();
  const canWrite = has("project_write");
  const projects = useProjects();
  const { projectId, select } = useSelectedProject(projects.data?.projects);
  const project = useProject(projectId ?? "");
  const error = errorMessage(projects) ?? errorMessage(project);
  // Hoisted out of `project` so the narrowing survives into the render prop
  // below, which TypeScript will not do for a property access.
  const detail = project.data;

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="This project could not be loaded">
          {error}
        </Alert>
      ) : null}

      <ProjectScoped
        projects={projects.data?.projects}
        projectId={projectId}
        onSelect={select}
        loading={projects.isPending}
        what="Approvers"
      >
        {project.isPending && projectId ? <Skeleton className="h-96 w-full" /> : null}
        {detail ? (
          <ProjectForm
            project={detail}
            title="Approvers"
            description="Each gate can name the person who answers it, and how long to wait before reminding them. The reminder is all the SLA does — nothing is ever approved for anyone."
            from={gatesOf}
            patch={(approvals) => ({ approvals })}
            canWrite={canWrite}
            readOnlyNote="Your role can read this project but not change who approves its gates."
          >
            {(draft, setDraft) => (
              <ApproversEditor
                gates={detail.gates}
                drafts={draft}
                onChange={setDraft}
                disabled={!canWrite}
              />
            )}
          </ProjectForm>
        ) : null}
      </ProjectScoped>
    </div>
  );
}

/** The gates as the patch shape: node id → who and how long. */
function gatesOf(project: ProjectDetail): Record<string, GateDraft> {
  return Object.fromEntries(
    project.gates.map((gate) => [
      gate.node_id,
      { assignee_id: gate.assignee_id, sla_hours: gate.sla_hours },
    ]),
  );
}
