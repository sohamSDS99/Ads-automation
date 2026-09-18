"use client";

import { Info } from "lucide-react";
import { useId } from "react";

import { Badge } from "@/components/ui/badge";
import { Combobox, type ComboboxItem } from "@/components/ui/combobox";
import { Tooltip } from "@/components/ui/tooltip";
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

/**
 * Which model does which job, and what that costs.
 *
 * A node asks for a *kind* of thinking, never a model (PRD §8). So this is four
 * decisions, not twenty-one, and each one shows the figure it moves: the
 * estimate under the table recomputes as the picker changes, from the token
 * baseline the API measured or declared.
 *
 * One table, used twice — for the workspace defaults and for a project's
 * overrides. They used to be two different tables on two different screens,
 * one of which showed the price of a run and one of which did not, which is
 * how you end up with two answers to "what will this cost".
 */
export function RoutingTable({
  catalogue,
  routing,
  onChange,
  canWrite,
  inherited = "default",
}: {
  catalogue: ModelCatalogue;
  routing: ModelRouting;
  onChange: (routing: ModelRouting) => void;
  canWrite: boolean;
  /**
   * What an unset row falls back to, in one word. "default" on the workspace
   * table, because nothing is above it; "workspace" on a project's, because
   * something is.
   */
  inherited?: string;
}) {
  /**
   * This table renders twice on the models tab — once for the workspace
   * defaults and once for a project's overrides — so its field ids cannot be
   * derived from the task class alone. Two `id="routing-extract"` on one page
   * means the second table's labels point at the first table's controls, and
   * clicking a label focuses the wrong picker.
   */
  const uid = useId();
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
              inherited={inherited}
              fieldId={`${uid}-${routingClass.task_class}`}
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
  inherited,
  fieldId,
  onChange,
}: {
  routing: TaskClassRouting;
  items: ComboboxItem[];
  chosen: string | null;
  model: ModelOption | undefined;
  canWrite: boolean;
  inherited: string;
  /** Unique across both copies of this table — see `RoutingTable`. */
  fieldId: string;
  onChange: (value: string) => void;
}) {
  const cost = costOfClass(routing, model);
  const id = fieldId;

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
              <span className="ml-2 font-sans text-fg-subtle">{inherited}</span>
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

/** What someone without the permission sees, and what anyone sees before the catalogue loads. */
export function ReadOnlyRouting({ routing }: { routing: ModelRouting }) {
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
