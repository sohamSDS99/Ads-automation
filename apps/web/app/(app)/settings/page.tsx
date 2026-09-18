"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Mail } from "lucide-react";
import { useState } from "react";

import { ScheduleEditor } from "@/components/settings/schedule-editor";
import { StorageCard } from "@/components/settings/storage-card";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { updateWorkspace, type Workspace } from "@/lib/api/workspace";
import { usd } from "@/lib/format";
import { errorMessage, keys, useProjects, useWorkspace } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * The workspace itself: what it is called, what a run may cost, and the two
 * facts about this deployment that are reported rather than edited.
 *
 * Model routing used to be on this screen too. It moved to its own tab, which
 * is what made this one short enough to take in at once.
 */
export default function WorkspaceSettingsPage() {
  const { has } = useSession();
  const workspace = useWorkspace();
  const projects = useProjects();
  const error = errorMessage(workspace);

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="Settings could not be loaded">
          {error}
        </Alert>
      ) : null}

      {workspace.isPending ? <Skeleton className="h-64 w-full" /> : null}
      {workspace.data ? (
        <WorkspaceForm workspace={workspace.data} canWrite={has("settings_write")} />
      ) : null}

      {workspace.data ? <EmailCard workspace={workspace.data} /> : null}

      <ScheduleEditor projects={projects.data?.projects ?? []} />

      {/* `GET /storage` needs `settings_write` — it reports the deployment's
          volume, not workspace state. Rendering the card without it means
          firing a request that can only 403. */}
      {has("settings_write") ? <StorageCard /> : null}
    </div>
  );
}

/**
 * Name and budget cap, saved together.
 *
 * One form and one Save: the API takes `name` on every PATCH, so two admins
 * editing different fields cannot each blank out the other's.
 */
function WorkspaceForm({ workspace, canWrite }: { workspace: Workspace; canWrite: boolean }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(workspace.name);
  const [cap, setCap] = useState(workspace.settings.max_run_cost_usd ?? "");
  // Same settled/dirty behaviour as every other tab. Two tabs that disagree
  // about what a Save button means is the small kind of mess that adds up.
  const dirty = name !== workspace.name || cap !== (workspace.settings.max_run_cost_usd ?? "");

  const save = useMutation({
    mutationFn: () =>
      updateWorkspace({
        name: name.trim(),
        ...(cap.trim() ? { max_run_cost_usd: cap.trim() } : {}),
      }),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: keys.workspace });
      toast.success("Settings saved");
    },
    onError: (error) =>
      toast.error("Not saved", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  return (
    <Card>
      <CardHeader
        title="Workspace"
        description="The name on every invite, and the ceiling on what one run may spend."
      />
      <CardBody className="space-y-5">
        <Field
          label="Workspace name"
          value={name}
          maxLength={120}
          disabled={!canWrite}
          onChange={(event) => setName(event.target.value)}
          hint="Shown in the header and on every invite."
        />

        <div className="flex flex-col gap-1.5">
          <label htmlFor="cap" className="text-sm font-medium text-fg">
            Budget cap per run
          </label>
          <div className="flex items-center gap-2">
            <span className="text-sm text-fg-muted">$</span>
            <Input
              id="cap"
              type="number"
              min="0.01"
              step="0.01"
              inputMode="decimal"
              value={cap}
              disabled={!canWrite}
              placeholder={workspace.default_max_run_cost_usd}
              onChange={(event) => setCap(event.target.value)}
              className="w-32"
            />
          </div>
          <p className="min-h-4 text-xs text-fg-muted">
            A run that reaches this stops, keeps everything it has finished, and reports as partial.
            Empty falls back to the deployment default of{" "}
            {usd(workspace.default_max_run_cost_usd)}.
          </p>
        </div>
      </CardBody>
      <CardFooter>
        {canWrite ? (
          dirty ? (
            <>
              <Button
                variant="ghost"
                onClick={() => {
                  setName(workspace.name);
                  setCap(workspace.settings.max_run_cost_usd ?? "");
                }}
              >
                Discard
              </Button>
              <Button onClick={() => save.mutate()} disabled={save.isPending || !name.trim()}>
                {save.isPending ? <Spinner label="Saving" /> : null}
                Save workspace
              </Button>
            </>
          ) : (
            <p className="text-sm text-fg-subtle">No unsaved changes</p>
          )
        ) : (
          <p className="mr-auto text-sm text-fg-muted">
            An admin sets the workspace name and the budget cap.
          </p>
        )}
      </CardFooter>
    </Card>
  );
}

/**
 * Email delivery, reported rather than edited.
 *
 * SMTP is deployment configuration — environment variables, not workspace
 * state (PRD §18 law 9) — so this card can say whether it works and what
 * happens when it does not, and nothing else.
 */
function EmailCard({ workspace }: { workspace: Workspace }) {
  return (
    <Card>
      <CardHeader title="Email" description="How invites and approval reminders reach people." />
      <CardBody>
        <div className="flex gap-3">
          <Mail className="mt-0.5 size-4 shrink-0 text-fg-subtle" aria-hidden />
          {workspace.smtp_configured ? (
            <p className="text-sm text-fg">
              SMTP is configured. Invites are emailed, and the link is still shown so you can send
              it another way if you prefer.
            </p>
          ) : (
            <p className="max-w-prose text-sm text-fg-muted">
              No SMTP server is configured, so nothing is emailed. Invites still work — the link
              appears once, for you to copy and send. Set{" "}
              <code className="font-mono text-xs">SMTP_HOST</code> and{" "}
              <code className="font-mono text-xs">SMTP_FROM</code> on the deployment to turn email
              on.
            </p>
          )}
        </div>
      </CardBody>
    </Card>
  );
}
