"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { BusinessContextFields } from "@/components/settings/business-context";
import { CrmUpload } from "@/components/settings/crm-upload";
import { DocumentUpload } from "@/components/settings/document-upload";
import { ProjectForm } from "@/components/settings/project-form";
import { ProjectScoped } from "@/components/settings/project-picker";
import { Alert } from "@/components/ui/alert";
import { Card, CardBody, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  autofillProject,
  updateProject,
  type AutofillField,
  type AutofillFinding,
  type ProjectDetail,
} from "@/lib/api/projects";
import { errorMessage, keys, useProject, useProjects } from "@/lib/queries";
import { useSession } from "@/lib/session";
import { useSelectedProject } from "@/lib/stores/settings";

/**
 * What one project sells, and where.
 *
 * Three cards, in the order the run reads them: the short text that goes into
 * every prompt, the documents it may cite, and the deal history no ad platform
 * can tell it. They were one step of a wizard, which meant the two uploads sat
 * behind a Save button they had nothing to do with.
 */
export default function BusinessContextPage() {
  const { has } = useSession();
  const canWrite = has("project_write");
  const projects = useProjects();
  const { projectId, select } = useSelectedProject(projects.data?.projects);
  const project = useProject(projectId ?? "");
  const error = errorMessage(projects) ?? errorMessage(project);

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
        what="Business context"
      >
        {project.isPending && projectId ? <Skeleton className="h-96 w-full" /> : null}
        {project.data ? <ContextCards project={project.data} canWrite={canWrite} /> : null}
      </ProjectScoped>
    </div>
  );
}

function ContextCards({ project, canWrite }: { project: ProjectDetail; canWrite: boolean }) {
  const queryClient = useQueryClient();
  /** What the last autofill read, kept beside the value so it can be judged. */
  const [findings, setFindings] = useState<AutofillFinding[]>([]);

  /**
   * Hand a field to the agent.
   *
   * The draft is committed first — see `ProjectForm`'s `commit` — because this
   * writes on the server and moves the project's revision.
   */
  const autofill = useMutation({
    mutationFn: async ({ field, commit }: { field: AutofillField; commit: () => Promise<boolean> }) => {
      // The save that has to happen first reports its own failure — a stale
      // revision raises the reload prompt. Returning null stops this from
      // saying the same thing a second time in a different voice.
      if (!(await commit())) return null;
      return autofillProject(project.id, [field]);
    },
    onSuccess: async (result) => {
      if (!result) return;
      setFindings((current) => [
        ...current.filter((item) => !result.findings.some((fresh) => fresh.field === item.field)),
        ...result.findings,
      ]);
      for (const finding of result.findings) {
        if (finding.found) toast.success("Worked it out", { description: finding.source });
        else toast.error("Nothing to read yet", { description: finding.source });
      }
      await queryClient.invalidateQueries({ queryKey: keys.project(project.id) });
    },
    onError: (error) =>
      toast.error("Could not work it out", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const stopAutofill = useMutation({
    mutationFn: (field: AutofillField) =>
      updateProject(project.id, { autofill: { ...project.autofill, [field]: false } }, project.version),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.project(project.id) }),
  });

  return (
    <>
      <ProjectForm
        project={project}
        title="Business context"
        description="The text every research node is grounded on. Specifics beat adjectives — a vague answer here produces a vague report and no error anywhere."
        from={(saved) => ({ context: saved.product_context, markets: saved.markets })}
        patch={(draft) => ({ product_context: draft.context, markets: draft.markets })}
        canWrite={canWrite}
        readOnlyNote="Your role can read this project but not change it."
      >
        {(draft, setDraft, commit) => (
          <BusinessContextFields
            context={draft.context}
            markets={draft.markets}
            onContextChange={(context) => setDraft({ ...draft, context })}
            onMarketsChange={(markets) => setDraft({ ...draft, markets })}
            disabled={!canWrite}
            autofill={project.autofill}
            findings={findings}
            onAutofill={(field) => autofill.mutate({ field, commit })}
            onAutofillOff={(field) => stopAutofill.mutate(field)}
            autofilling={autofill.isPending ? (autofill.variables?.field ?? null) : null}
          />
        )}
      </ProjectForm>

      <Card>
        <CardHeader
          title="Documents"
          description="A positioning deck, a price list, a battlecard. These become citable evidence rather than prompt text, which is how a 30-page document gets used without being put in front of twenty-three model calls."
        />
        <CardBody>
          <DocumentUpload projectId={project.id} disabled={!canWrite} />
        </CardBody>
      </Card>

      <Card>
        <CardHeader
          title="CRM export"
          description="Closed-won and closed-lost deals. This is how a run learns which customers were worth winning, which no ad platform can tell it."
        />
        <CardBody>
          {canWrite ? (
            <CrmUpload projectId={project.id} disabled={false} />
          ) : (
            <p className="text-sm text-fg-muted">
              Your role cannot upload files to this project.
            </p>
          )}
        </CardBody>
      </Card>
    </>
  );
}
