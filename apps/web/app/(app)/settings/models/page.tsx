"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import Link from "next/link";
import { useState } from "react";

import { ReadOnlyRouting, RoutingTable } from "@/components/settings/model-routing";
import { ProjectForm } from "@/components/settings/project-form";
import { ProjectScoped } from "@/components/settings/project-picker";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import type { ModelCatalogue } from "@/lib/api/models";
import type { ModelRouting, TaskClassName } from "@/lib/api/projects";
import { updateWorkspace, type Workspace } from "@/lib/api/workspace";
import { errorMessage, keys, useModels, useProject, useProjects, useWorkspace } from "@/lib/queries";
import { useSelectedProject } from "@/lib/stores/settings";
import { useSession } from "@/lib/session";

/**
 * Which model does which job — for the workspace, and then for one project.
 *
 * Two tables, same component, in the order the values resolve: a project falls
 * back to the workspace, and the workspace falls back to the build's defaults.
 * Reading them top to bottom is reading the chain.
 */
export default function ModelsPage() {
  const { has } = useSession();
  const canWrite = has("settings_write");
  const workspace = useWorkspace();
  const projects = useProjects();
  // `GET /models` needs `settings_write`. Asking for it as an operator is a
  // guaranteed 403, and the read-only table below renders without it anyway.
  const catalogue = useModels(canWrite);
  const { projectId, select } = useSelectedProject(projects.data?.projects);
  const project = useProject(projectId ?? "");
  const error = errorMessage(workspace) ?? errorMessage(projects);

  const noKey =
    catalogue.isError && catalogue.error instanceof ApiError && catalogue.error.status === 409;

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="Model settings could not be loaded">
          {error}
        </Alert>
      ) : null}

      {!canWrite ? (
        <Alert tone="info" title="Model routing is set by an admin">
          You can see what a run will use. Changing it, and the cost behind it, is an admin action.
        </Alert>
      ) : null}

      {noKey ? (
        <Alert tone="warning" title="Add the model key to choose models">
          The catalogue and its prices come from OpenRouter, so these tables stay on their defaults
          until a key is stored.{" "}
          <Link href="/settings/connections" className="text-accent hover:underline">
            Connect OpenRouter
          </Link>
          .
        </Alert>
      ) : null}

      {catalogue.isError && !noKey ? (
        <Alert tone="error" title="The model catalogue could not be loaded">
          {catalogue.error instanceof Error ? catalogue.error.message : "Try again in a moment."}
          {" A run uses the default models until it can be read."}
        </Alert>
      ) : null}

      {workspace.isPending ? <Skeleton className="h-96 w-full" /> : null}
      {workspace.data ? (
        <WorkspaceRouting
          workspace={workspace.data}
          catalogue={catalogue.data}
          canWrite={canWrite}
        />
      ) : null}

      <ProjectScoped
        projects={projects.data?.projects}
        projectId={projectId}
        onSelect={select}
        loading={projects.isPending}
        what="Model routing"
      >
        {project.isPending && projectId ? <Skeleton className="h-72 w-full" /> : null}
        {project.data ? (
          <ProjectForm
            project={project.data}
            title="This project's models"
            description="Only what this project does differently. Anything left unset uses the workspace default above."
            from={(saved) => saved.models}
            patch={(models) => ({ models })}
            canWrite={canWrite}
            readOnlyNote="An admin sets which models a project uses."
          >
            {(draft, setDraft) =>
              catalogue.data ? (
                <RoutingTable
                  catalogue={catalogue.data}
                  routing={draft}
                  onChange={setDraft}
                  canWrite={canWrite}
                  inherited="workspace"
                />
              ) : (
                <ReadOnlyRouting routing={draft} />
              )
            }
          </ProjectForm>
        ) : null}
      </ProjectScoped>
    </div>
  );
}

/**
 * The defaults every project inherits until it says otherwise.
 *
 * Saved with the workspace name alongside it, because the API takes `name` on
 * every PATCH — that is what stops two admins on two tabs from each blanking
 * out the other's field.
 */
function WorkspaceRouting({
  workspace,
  catalogue,
  canWrite,
}: {
  workspace: Workspace;
  catalogue: ModelCatalogue | undefined;
  canWrite: boolean;
}) {
  const queryClient = useQueryClient();
  const [routing, setRouting] = useState<ModelRouting>(() => toRouting(workspace.settings.models));
  const dirty = JSON.stringify(routing) !== JSON.stringify(toRouting(workspace.settings.models));

  const save = useMutation({
    mutationFn: () => updateWorkspace({ name: workspace.name, models: toSettings(routing) }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.workspace });
      toast.success("Default models saved");
    },
    onError: (error) =>
      toast.error("Not saved", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  return (
    <Card>
      <CardHeader
        title="Workspace defaults"
        description="What every project uses unless it overrides it below."
      />
      <CardBody>
        {catalogue ? (
          <RoutingTable
            catalogue={catalogue}
            routing={routing}
            onChange={setRouting}
            canWrite={canWrite}
          />
        ) : (
          <ReadOnlyRouting routing={routing} />
        )}
      </CardBody>
      {canWrite ? (
        <CardFooter>
          {dirty ? (
            <>
              <Button
                variant="ghost"
                onClick={() => setRouting(toRouting(workspace.settings.models))}
              >
                Discard
              </Button>
              <Button onClick={() => save.mutate()} disabled={save.isPending}>
                {save.isPending ? <Spinner label="Saving" /> : null}
                Save defaults
              </Button>
            </>
          ) : (
          <p className="text-sm text-fg-subtle">No unsaved changes</p>
          )}
        </CardFooter>
      ) : null}
    </Card>
  );
}

const CLASSES: TaskClassName[] = ["extract", "classify", "synthesize", "critique"];

/** The workspace stores a loose map; the table speaks the four-key shape. */
function toRouting(models: Record<string, string>): ModelRouting {
  return {
    extract: models.extract ?? null,
    classify: models.classify ?? null,
    synthesize: models.synthesize ?? null,
    critique: models.critique ?? null,
  };
}

/** …and back, dropping the classes nobody chose rather than storing nulls. */
function toSettings(routing: ModelRouting): Record<string, string> {
  return Object.fromEntries(
    CLASSES.flatMap((name) => (routing[name] ? [[name, routing[name] as string]] : [])),
  );
}
