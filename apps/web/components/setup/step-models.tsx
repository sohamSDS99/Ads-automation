"use client";

import { Info } from "lucide-react";

import { CredentialCard } from "@/components/setup/credential-card";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Combobox, type ComboboxItem } from "@/components/ui/combobox";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import { credentialFor } from "@/lib/api/credentials";
import {
  costOfClass,
  estimateRunCost,
  formatContext,
  formatPerMillion,
  formatUsd,
  type ModelCatalogue,
  type ModelOption,
  type TaskClassRouting,
} from "@/lib/api/models";
import type { ModelRouting, TaskClassName } from "@/lib/api/projects";
import { compactNumber } from "@/lib/format";
import { useCredentials, useModels } from "@/lib/queries";

/**
 * Step 3 — which model does which job, and what that costs.
 *
 * A node asks for a *kind* of thinking, never a model (PRD §8). So this is four
 * decisions, not twenty-one, and each one shows the figure it moves: the
 * estimate under the table recomputes as the picker changes, from the token
 * baseline the API measured or declared.
 *
 * Admin-only. An operator sees the same table with the choices already made and
 * a line saying who can change them.
 */
export function StepModels({
  routing,
  onChange,
  canWrite,
}: {
  routing: ModelRouting;
  onChange: (routing: ModelRouting) => void;
  canWrite: boolean;
}) {
  const credentials = useCredentials();
  const catalogue = useModels(canWrite);

  const openrouterSpec = (credentials.data?.kinds ?? []).find((kind) => kind.kind === "openrouter");
  const openrouter = credentialFor(credentials.data?.credentials ?? [], "openrouter");
  const noKey = catalogue.isError && catalogue.error instanceof ApiError && catalogue.error.status === 409;

  return (
    <div className="space-y-6">
      {openrouterSpec ? (
        <section className="space-y-3">
          <div>
            <h3 className="text-sm font-medium text-fg">The key everything runs through</h3>
            <p className="text-sm text-fg-muted">
              One OpenRouter key reaches every model. Testing it reports the credit left on the
              account.
            </p>
          </div>
          <CredentialCard
            spec={openrouterSpec}
            credential={openrouter}
            canWrite={canWrite}
            reason="Only an admin can store the OpenRouter key."
          />
        </section>
      ) : null}

      {!canWrite ? (
        <Alert tone="info" title="Model routing is set by an admin">
          You can see what this project will use. Changing it, and the budget behind it, is an admin
          action.
        </Alert>
      ) : null}

      {noKey ? (
        <Alert tone="warning" title="Add the key to choose models">
          The catalogue and its prices come from OpenRouter, so this table stays on its defaults
          until a key is stored above.
        </Alert>
      ) : null}

      {catalogue.isError && !noKey ? (
        <Alert tone="error" title="The model catalogue could not be loaded">
          {catalogue.error instanceof Error ? catalogue.error.message : "Try again in a moment."}
          {" The run will use the default models until it can be read."}
        </Alert>
      ) : null}

      {catalogue.isPending && canWrite ? <Skeleton className="h-64 w-full" /> : null}

      {catalogue.data ? (
        <RoutingTable
          catalogue={catalogue.data}
          routing={routing}
          onChange={onChange}
          canWrite={canWrite}
        />
      ) : (
        <ReadOnlyRouting routing={routing} />
      )}
    </div>
  );
}

function RoutingTable({
  catalogue,
  routing,
  onChange,
  canWrite,
}: {
  catalogue: ModelCatalogue;
  routing: ModelRouting;
  onChange: (routing: ModelRouting) => void;
  canWrite: boolean;
}) {
  const byId = new Map(catalogue.models.map((model) => [model.id, model]));
  const total = estimateRunCost(catalogue, routing);
  const assumed = catalogue.usage_source === "assumed";

  const items: ComboboxItem[] = catalogue.models.map((model) => ({
    value: model.id,
    search: `${model.id} ${model.name}`,
    label: <span className="font-mono text-xs">{model.id}</span>,
    render: <ModelRow model={model} />,
  }));

  return (
    <section className="space-y-3">
      <div className="overflow-hidden rounded-[var(--radius)] border bg-surface-raised">
        <ul className="divide-y">
          {catalogue.task_classes.map((routingClass) => (
            <TaskClassRow
              key={routingClass.task_class}
              routing={routingClass}
              items={items}
              chosen={routing[routingClass.task_class]}
              model={byId.get(routing[routingClass.task_class] || routingClass.default_model)}
              canWrite={canWrite}
              onChange={(value) =>
                onChange({ ...routing, [routingClass.task_class]: value } as ModelRouting)
              }
            />
          ))}
        </ul>

        <div className="flex flex-wrap items-baseline justify-between gap-2 border-t bg-surface px-4 py-3.5">
          <div className="flex items-center gap-2">
            <span className="text-sm text-fg-muted">Estimated cost of one full run</span>
            <Tooltip
              content={
                assumed
                  ? "Token counts are this build's declared assumption for a 21-node run. They become measured once three full runs have finished."
                  : "Token counts are averaged from the full runs this workspace has already completed."
              }
            >
              <span className="inline-flex text-fg-subtle">
                <Info className="size-3.5" aria-hidden />
                <span className="sr-only">How this is calculated</span>
              </span>
            </Tooltip>
            <Badge tone={assumed ? "neutral" : "accent"}>
              {assumed ? "estimate" : "measured"}
            </Badge>
          </div>
          <span data-numeric className="text-[length:var(--text-lg)] font-medium tracking-tight">
            {formatUsd(total)}
          </span>
        </div>
      </div>
    </section>
  );
}

function TaskClassRow({
  routing,
  items,
  chosen,
  model,
  canWrite,
  onChange,
}: {
  routing: TaskClassRouting;
  items: ComboboxItem[];
  chosen: string | null;
  model: ModelOption | undefined;
  canWrite: boolean;
  onChange: (value: string) => void;
}) {
  const cost = costOfClass(routing, model);
  const id = `routing-${routing.task_class}`;

  return (
    <li className="grid gap-3 px-4 py-4 md:grid-cols-[14rem_1fr_6rem] md:items-center">
      <div>
        <label htmlFor={id} className="font-medium text-fg">
          {routing.label}
        </label>
        <p className="max-w-prose text-sm text-fg-muted">{routing.purpose}</p>
        <p className="mt-1 text-xs text-fg-subtle" data-numeric>
          {routing.calls_per_run} calls · {compactNumber(routing.token_in_per_run)} in ·{" "}
          {compactNumber(routing.token_out_per_run)} out
        </p>
      </div>

      <div className="min-w-0">
        <Combobox
          id={id}
          items={items}
          value={chosen}
          disabled={!canWrite}
          onChange={onChange}
          placeholder="Search models…"
          triggerLabel={
            <span className="font-mono text-xs text-fg-muted">
              {routing.default_model}
              <span className="ml-2 font-sans text-fg-subtle">default</span>
            </span>
          }
        />
        <p className="mt-1.5 text-xs text-fg-subtle">
          Falls back to {routing.fallbacks.join(", ") || "nothing"} if it errors out.
          {model && !model.supports_structured_output ? (
            <span className="text-status-gate">
              {" "}
              This model does not advertise strict JSON schemas — the gateway will have to coax it.
            </span>
          ) : null}
        </p>
      </div>

      <p data-numeric className="text-sm text-fg md:text-right">
        {formatUsd(cost)}
      </p>
    </li>
  );
}

function ModelRow({ model }: { model: ModelOption }) {
  return (
    <span className="flex min-w-0 flex-col gap-0.5">
      <span className="flex items-baseline gap-2">
        <span className="truncate font-mono text-xs text-fg">{model.id}</span>
        {model.supports_structured_output ? null : (
          <span className="shrink-0 text-[11px] text-fg-subtle">no strict JSON</span>
        )}
      </span>
      <span data-numeric className="text-[11px] text-fg-subtle">
        {formatContext(model.context_length)} context ·{" "}
        {formatPerMillion(model.prompt_per_million)}/M in ·{" "}
        {formatPerMillion(model.completion_per_million)}/M out
      </span>
    </span>
  );
}

/** What an operator sees, and what an admin sees before the catalogue loads. */
function ReadOnlyRouting({ routing }: { routing: ModelRouting }) {
  const rows: [TaskClassName, string][] = [
    ["extract", "Extract"],
    ["classify", "Classify"],
    ["synthesize", "Synthesize"],
    ["critique", "Critique"],
  ];
  return (
    <ul className="divide-y overflow-hidden rounded-[var(--radius)] border bg-surface-raised">
      {rows.map(([key, label]) => (
        <li key={key} className="flex items-baseline justify-between gap-3 px-4 py-3">
          <span className="font-medium text-fg">{label}</span>
          <span className="font-mono text-xs text-fg-muted">
            {routing[key] ?? "workspace default"}
          </span>
        </li>
      ))}
    </ul>
  );
}
