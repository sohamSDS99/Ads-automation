"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useRef, useState, type ReactNode } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { updateProject, type ProjectDetail, type ProjectPatch } from "@/lib/api/projects";
import { keys } from "@/lib/queries";

/**
 * A card that edits part of one project and saves it on its own.
 *
 * This is what is left of the wizard once the stepper is gone. The wizard's
 * value was never the five arrows — it was that each step saved only the
 * fields it owned, so two people editing different parts of the same project
 * could not blank out each other's work, and a stale revision became a prompt
 * rather than a silent loss (PRD §16). All of that lives here now, once,
 * instead of being threaded through five steps and a footer.
 *
 * Save is disabled until something actually changes, which is the other half
 * of why the wizard felt like work: "Save and continue" on a step you only
 * read made every screen feel like it was asking for a decision.
 */
export function ProjectForm<T>({
  project,
  title,
  description,
  from,
  patch,
  canWrite,
  readOnlyNote,
  children,
}: {
  project: ProjectDetail;
  title: string;
  description?: string;
  /** The fields this card owns, read off the saved project. */
  from: (project: ProjectDetail) => T;
  /** Those same fields as a patch. Only these are sent. */
  patch: (draft: T) => ProjectPatch;
  canWrite: boolean;
  /** Shown in place of the save button when `canWrite` is false. */
  readOnlyNote?: string;
  /**
   * `commit` saves the current draft without announcing it, and reports
   * whether it landed. It exists for the one control that has to write before
   * it can act: handing a field to the agent moves the project's revision on
   * the server, which resyncs this draft — so anything typed and not yet saved
   * would be replaced by the copy the server still had. Saving first makes
   * that resync a no-op instead of a loss.
   */
  children: (draft: T, setDraft: (next: T) => void, commit: () => Promise<boolean>) => ReactNode;
}) {
  const queryClient = useQueryClient();
  const [stale, setStale] = useState(false);
  /**
   * The draft, keyed to the revision it was read from.
   *
   * A refetch that brings a newer row in replaces the draft rather than
   * letting someone keep editing a copy of the old one. `version` only moves
   * once a write has landed, so this never fires mid-save; assigning during
   * render is what keeps the draft from lagging a render behind the data.
   */
  const [state, setState] = useState(() => ({
    version: project.version,
    draft: from(project),
  }));
  if (state.version !== project.version) {
    setState({ version: project.version, draft: from(project) });
  }

  const baseline = JSON.stringify(from(project));
  const dirty = JSON.stringify(state.draft) !== baseline;

  /**
   * Whether the save in flight should announce itself.
   *
   * A ref rather than a mutation variable because the only consumer is the
   * toast: `commit` saves on someone else's behalf, and a "Saved" toast
   * followed immediately by "Worked it out" reports one action twice.
   */
  const quiet = useRef(false);

  const save = useMutation({
    mutationFn: () => updateProject(project.id, patch(state.draft), project.version),
    onSuccess: async () => {
      setStale(false);
      await queryClient.invalidateQueries({ queryKey: keys.project(project.id) });
      await queryClient.invalidateQueries({ queryKey: keys.projects });
      if (!quiet.current) toast.success("Saved");
      quiet.current = false;
    },
    onError: (error) => {
      quiet.current = false;
      if (error instanceof ApiError && error.status === 412) {
        setStale(true);
        return;
      }
      toast.error("Not saved", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      });
    },
  });

  return (
    <Card>
      <CardHeader title={title} description={description} />
      <CardBody className="space-y-5">
        {stale ? (
          <Alert tone="warning" title="Someone else edited this project">
            Your changes were not saved, so nothing of theirs was lost. Reload to see the current
            version, then make your edit again.
            <div className="mt-2">
              <Button
                size="sm"
                variant="secondary"
                onClick={async () => {
                  await queryClient.invalidateQueries({ queryKey: keys.project(project.id) });
                  setStale(false);
                }}
              >
                Reload this project
              </Button>
            </div>
          </Alert>
        ) : null}

        {children(
          state.draft,
          (draft) => setState((current) => ({ ...current, draft })),
          async () => {
            if (!dirty) return true;
            quiet.current = true;
            try {
              await save.mutateAsync();
              return true;
            } catch {
              return false;
            }
          },
        )}
      </CardBody>
      <CardFooter>
        {canWrite ? (
          // A settled card says so in a line of text, not with a disabled
          // primary button. A filled button at half opacity is the loudest
          // thing on the card and it does nothing — which reads as broken
          // rather than as finished.
          dirty ? (
            <>
              <Button
                variant="ghost"
                onClick={() => setState({ version: project.version, draft: from(project) })}
              >
                Discard
              </Button>
              <Button onClick={() => save.mutate()} disabled={save.isPending}>
                {save.isPending ? <Spinner label="Saving" /> : null}
                Save changes
              </Button>
            </>
          ) : (
          <p className="text-sm text-fg-subtle">No unsaved changes</p>
          )
        ) : (
          <p className="mr-auto text-sm text-fg-muted">{readOnlyNote}</p>
        )}
      </CardFooter>
    </Card>
  );
}
