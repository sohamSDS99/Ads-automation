"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Mail } from "lucide-react";
import { useState } from "react";

import { CredentialCard } from "@/components/setup/credential-card";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody, CardFooter, CardHeader } from "@/components/ui/card";
import { Combobox, type ComboboxItem } from "@/components/ui/combobox";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { credentialFor } from "@/lib/api/credentials";
import { formatContext, formatPerMillion, type ModelCatalogue } from "@/lib/api/models";
import { updateWorkspace, type Workspace } from "@/lib/api/workspace";
import { usd } from "@/lib/format";
import { errorMessage, keys, useCredentials, useModels, useWorkspace } from "@/lib/queries";

export default function WorkspaceSettingsPage() {
  const workspace = useWorkspace();
  const credentials = useCredentials();
  const catalogue = useModels(true);
  const error = errorMessage(workspace);

  const openrouterSpec = (credentials.data?.kinds ?? []).find((kind) => kind.kind === "openrouter");

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="Settings could not be loaded">
          {error}
        </Alert>
      ) : null}

      {workspace.isPending ? <Skeleton className="h-64 w-full" /> : null}
      {workspace.data ? (
        <WorkspaceForm workspace={workspace.data} catalogue={catalogue.data} />
      ) : null}

      {openrouterSpec ? (
        <Card>
          <CardHeader
            title="OpenRouter key"
            description="Every research node is a call through this key. It is stored encrypted and never readable again — replacing it is how it changes."
          />
          <CardBody>
            <CredentialCard
              spec={openrouterSpec}
              credential={credentialFor(credentials.data?.credentials ?? [], "openrouter")}
              canWrite
              reason=""
            />
          </CardBody>
        </Card>
      ) : null}

      {workspace.data ? <EmailCard workspace={workspace.data} /> : null}
    </div>
  );
}

/**
 * Name, budget cap and the workspace-wide model defaults, saved together.
 *
 * One form and one Save: the API takes the whole thing in a single PATCH, so
 * two admins editing different fields cannot each blank out the other's.
 */
function WorkspaceForm({
  workspace,
  catalogue,
}: {
  workspace: Workspace;
  catalogue: ModelCatalogue | undefined;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(workspace.name);
  const [cap, setCap] = useState(workspace.settings.max_run_cost_usd ?? "");
  const [models, setModels] = useState<Record<string, string>>(workspace.settings.models);

  const save = useMutation({
    mutationFn: () =>
      updateWorkspace({
        name: name.trim(),
        models,
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

  const items: ComboboxItem[] = (catalogue?.models ?? []).map((model) => ({
    value: model.id,
    search: `${model.id} ${model.name}`,
    label: <span className="font-mono text-xs">{model.id}</span>,
    render: (
      <span className="flex flex-col gap-0.5">
        <span className="truncate font-mono text-xs text-fg">{model.id}</span>
        <span data-numeric className="text-[11px] text-fg-subtle">
          {formatContext(model.context_length)} context ·{" "}
          {formatPerMillion(model.prompt_per_million)}/M in ·{" "}
          {formatPerMillion(model.completion_per_million)}/M out
        </span>
      </span>
    ),
  }));

  return (
    <Card>
      <CardHeader
        title="Workspace"
        description="The defaults every project inherits until it says otherwise."
      />
      <CardBody className="space-y-5">
        <Field
          label="Workspace name"
          value={name}
          maxLength={120}
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

        <fieldset className="space-y-3">
          <legend className="text-sm font-medium text-fg">Default model routing</legend>
          <p className="-mt-1 text-xs text-fg-muted">
            {catalogue
              ? "A project can override any of these on its own setup screen."
              : "The catalogue could not be read, so these cannot be changed right now. Runs keep using the built-in defaults."}
          </p>
          <ul className="divide-y rounded-[var(--radius)] border">
            {(catalogue?.task_classes ?? []).map((taskClass) => (
              <li
                key={taskClass.task_class}
                className="grid gap-2 px-3 py-3 sm:grid-cols-[10rem_1fr] sm:items-center"
              >
                <label
                  htmlFor={`default-${taskClass.task_class}`}
                  className="text-sm font-medium text-fg"
                >
                  {taskClass.label}
                </label>
                <Combobox
                  id={`default-${taskClass.task_class}`}
                  items={items}
                  value={models[taskClass.task_class] ?? null}
                  onChange={(value) =>
                    setModels((current) => ({ ...current, [taskClass.task_class]: value }))
                  }
                  triggerLabel={
                    <span className="font-mono text-xs text-fg-muted">
                      {taskClass.default_model}
                      <span className="ml-2 font-sans text-fg-subtle">built-in</span>
                    </span>
                  }
                />
              </li>
            ))}
            {catalogue ? null : (
              <li className="px-3 py-4 text-sm text-fg-subtle">Nothing to choose from yet.</li>
            )}
          </ul>
        </fieldset>
      </CardBody>
      <CardFooter>
        <Button onClick={() => save.mutate()} disabled={save.isPending || !name.trim()}>
          {save.isPending ? <Spinner label="Saving" /> : null}
          Save workspace
        </Button>
      </CardFooter>
    </Card>
  );
}

/**
 * Email delivery, reported rather than edited.
 *
 * SMTP is deployment configuration — environment variables, not workspace state
 * (PRD §18 law 9) — so this screen can say whether it works and what happens
 * when it does not, and nothing else.
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
              appears once, for you to copy and send. Set <code className="font-mono text-xs">SMTP_HOST</code>{" "}
              and <code className="font-mono text-xs">SMTP_FROM</code> on the deployment to turn
              email on.
            </p>
          )}
        </div>
      </CardBody>
    </Card>
  );
}
