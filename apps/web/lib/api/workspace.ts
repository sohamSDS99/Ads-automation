/** The workspace singleton (PRD §13.4 E). */
import { apiFetch } from "@/lib/api";

export type WorkspaceSettings = {
  /** Task class → OpenRouter model id. Projects may override it. */
  models: Record<string, string>;
  /** Null means the deployment default below applies. */
  max_run_cost_usd: string | null;
};

export type Workspace = {
  id: string;
  name: string;
  created_at: string;
  settings: WorkspaceSettings;
  /** SMTP is environment configuration, so this is reported, not edited. */
  smtp_configured: boolean;
  default_max_run_cost_usd: string;
};

export function getWorkspace(): Promise<Workspace> {
  return apiFetch("/workspace");
}

/**
 * Save the workspace.
 *
 * One request for the whole form: `name` always travels, so two admins saving
 * different fields cannot each blank out the other's.
 */
export function updateWorkspace(body: {
  name: string;
  models?: Record<string, string>;
  max_run_cost_usd?: string;
}): Promise<Workspace> {
  return apiFetch("/workspace", { method: "PATCH", body: JSON.stringify(body) });
}
