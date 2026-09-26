/**
 * Projects: the list, one project, and the edits the setup wizard makes.
 *
 * Every project carries a `version` — an opaque revision token — which goes
 * back out as `If-Match` on save. Two people editing the same project get a 412
 * and a prompt instead of one silently overwriting the other (PRD §16).
 *
 * Not `If-Unmodified-Since`: that header carries HTTP-date, which resolves to
 * whole seconds, so two saves inside the same second look identical to it.
 */
import { apiFetch } from "@/lib/api";

export type Market = { country: string; language: string; currency: string };

export type ProductContext = {
  summary: string;
  products: string[];
  pricing: string;
  icp: string;
  differentiators: string[];
  site_url: string;
};

export type ModelRouting = {
  extract: string | null;
  classify: string | null;
  synthesize: string | null;
  critique: string | null;
};

export type TaskClassName = keyof ModelRouting;

export type GateInfo = {
  node_id: string;
  stage: string;
  name: string;
  audience: string;
  description: string;
  required_role: string;
  assignee_id: string | null;
  assignee_name: string | null;
  sla_hours: number | null;
};

export type RunStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  // The run-level twin of the node status: halted for one named person.
  | "awaiting_human_task"
  | "succeeded"
  | "failed"
  | "cancelled";

export type RunSummary = {
  id: string;
  project_id: string;
  status: RunStatus;
  mode: "full" | "partial";
  trigger: "manual" | "schedule";
  triggered_by: string | null;
  triggered_by_name: string | null;
  /** The run this one followed. Null means there is nothing to compare against. */
  parent_run_id: string | null;
  started_at: string | null;
  finished_at: string | null;
  cost_usd: string;
  token_in: number;
  token_out: number;
  error: Record<string, unknown> | null;
};

export type ProjectRequirement = { code: string; detail: string; blocking: boolean };

export type ProjectSummary = {
  id: string;
  name: string;
  domain: string;
  created_at: string;
  updated_at: string;
  created_by: string;
  created_by_name: string | null;
  /** Opaque. Read it from a response, send it back as `If-Match`. */
  version: string;
  run_count: number;
  last_run: RunSummary | null;
};

/** Which step-1 fields this project asks the agent to work out for itself. */
export type AutofillSettings = {
  site_url: boolean;
  markets: boolean;
};

export type AutofillField = keyof AutofillSettings;

/** One field the agent worked out, and what it read to get there. */
export type AutofillFinding = {
  field: string;
  value: unknown;
  source: string;
  /** False when nothing could be read. Nothing is invented to fill the gap. */
  found: boolean;
};

export type ProjectDetail = ProjectSummary & {
  product_context: ProductContext;
  markets: Market[];
  models: ModelRouting;
  gates: GateInfo[];
  autofill: AutofillSettings;
  requirements: ProjectRequirement[];
};

export type ProjectPatch = {
  name?: string;
  domain?: string;
  product_context?: ProductContext;
  markets?: Market[];
  autofill?: AutofillSettings;
  models?: Partial<ModelRouting>;
  approvals?: Record<string, { assignee_id: string | null; sla_hours: number | null }>;
};

export function listProjects(): Promise<{ projects: ProjectSummary[] }> {
  return apiFetch("/projects");
}

export function getProject(id: string): Promise<ProjectDetail> {
  return apiFetch(`/projects/${id}`);
}

export function createProject(name: string, domain: string): Promise<ProjectDetail> {
  return apiFetch("/projects", { method: "POST", body: JSON.stringify({ name, domain }) });
}

/**
 * Save part of a project.
 *
 * `version` is the token from the project the caller last read. Sending it is
 * what turns a lost update into a 412 the person can act on.
 */
export function updateProject(
  id: string,
  patch: ProjectPatch,
  version?: string,
): Promise<ProjectDetail> {
  return apiFetch(`/projects/${id}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
    headers: version ? { "If-Match": version } : undefined,
  });
}

/**
 * Ask the server to work out the step-1 fields it can read for itself.
 *
 * Returns the whole project as well as the findings: the values it wrote are
 * ordinary project fields afterwards, and the wizard re-reads them from there
 * rather than keeping a second copy.
 */
export function autofillProject(
  id: string,
  fields: AutofillField[],
): Promise<{ findings: AutofillFinding[]; project: ProjectDetail }> {
  return apiFetch(`/projects/${id}/autofill`, {
    method: "POST",
    body: JSON.stringify({ fields }),
  });
}

export function listProjectRuns(id: string): Promise<{ runs: RunSummary[] }> {
  return apiFetch(`/projects/${id}/runs`);
}

export function launchRun(
  projectId: string,
  body: { mode?: "full" | "partial"; node_ids?: string[]; reuse_cache?: boolean } = {},
): Promise<{ id: string; status: RunStatus }> {
  return apiFetch(`/projects/${projectId}/runs`, {
    method: "POST",
    body: JSON.stringify({ mode: "full", reuse_cache: true, ...body }),
  });
}

/** The blocking half of `requirements` — what actually stops a launch. */
export function blockers(project: ProjectDetail): ProjectRequirement[] {
  return project.requirements.filter((item) => item.blocking);
}

export function warnings(project: ProjectDetail): ProjectRequirement[] {
  return project.requirements.filter((item) => !item.blocking);
}
