/**
 * The model catalogue, and what a routing choice would cost.
 *
 * The API returns the token baseline for one full run per task class; the
 * estimate below multiplies it by the price of whichever model is selected.
 * That is what makes the figure move as you change the picker without a request
 * per keystroke (PRD §13.4 step 3).
 */
import { apiFetch } from "@/lib/api";
import type { ModelRouting, TaskClassName } from "@/lib/api/projects";

export type ModelOption = {
  id: string;
  name: string;
  context_length: number | null;
  prompt_per_million: string | null;
  completion_per_million: string | null;
  supports_structured_output: boolean;
};

export type TaskClassRouting = {
  task_class: TaskClassName;
  label: string;
  purpose: string;
  default_model: string;
  fallbacks: string[];
  calls_per_run: number;
  token_in_per_run: number;
  token_out_per_run: number;
  usage_source: "measured" | "assumed";
};

export type ModelCatalogue = {
  models: ModelOption[];
  task_classes: TaskClassRouting[];
  usage_source: "measured" | "assumed";
};

export function getModels(refresh = false): Promise<ModelCatalogue> {
  return apiFetch(`/models${refresh ? "?refresh=true" : ""}`);
}

const PER_MILLION = 1_000_000;

/** What one full run of this task class would cost at this model's prices. */
export function costOfClass(
  routing: TaskClassRouting,
  model: ModelOption | undefined,
): number | null {
  if (!model) return null;
  const prompt = Number(model.prompt_per_million ?? NaN);
  const completion = Number(model.completion_per_million ?? NaN);
  if (Number.isNaN(prompt) || Number.isNaN(completion)) return null;
  return (
    (routing.token_in_per_run * prompt) / PER_MILLION +
    (routing.token_out_per_run * completion) / PER_MILLION
  );
}

/**
 * The whole run, at the current routing.
 *
 * Null when any class prices to null — a total that silently skipped a class
 * would read as cheaper than the run actually is.
 */
export function estimateRunCost(
  catalogue: ModelCatalogue,
  chosen: Partial<ModelRouting>,
): number | null {
  const byId = new Map(catalogue.models.map((model) => [model.id, model]));
  let total = 0;
  for (const routing of catalogue.task_classes) {
    const id = chosen[routing.task_class] || routing.default_model;
    const cost = costOfClass(routing, byId.get(id));
    if (cost === null) return null;
    total += cost;
  }
  return total;
}

export function formatUsd(value: number | null, fractionDigits = 2): string {
  if (value === null) return "—";
  return `$${value.toFixed(fractionDigits)}`;
}

export function formatPerMillion(value: string | null): string {
  if (value === null) return "—";
  const amount = Number(value);
  if (Number.isNaN(amount)) return "—";
  if (amount === 0) return "free";
  return amount < 1 ? `$${amount.toFixed(3)}` : `$${amount.toFixed(2)}`;
}

export function formatContext(tokens: number | null): string {
  if (!tokens) return "—";
  return tokens >= 1000 ? `${Math.round(tokens / 1000)}K` : String(tokens);
}
