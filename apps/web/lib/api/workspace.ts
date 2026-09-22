/**
 * Workspaces: the one you are in, and the ones the installation has.
 *
 * Two shapes on purpose, mirroring the API. `/workspace` is the active one and
 * needs no id; `/workspaces` is the collection, and everything that writes to
 * it needs `platform_admin` — which is how one company's admin is kept out of
 * another company's workspace (PRD §13.4 E).
 */
import { apiFetch, type Me } from "@/lib/api";
import type { Role } from "@/lib/permissions";

export type WorkspaceSettings = {
  /** Task class → OpenRouter model id. Projects may override it. */
  models: Record<string, string>;
  /** Null means the deployment default below applies. */
  max_run_cost_usd: string | null;
  max_plan_cost_usd: string | null;
};

export type Workspace = {
  id: string;
  name: string;
  created_at: string;
  settings: WorkspaceSettings;
  /** SMTP is environment configuration, so this is reported, not edited. */
  smtp_configured: boolean;
  default_max_run_cost_usd: string;
  default_max_plan_cost_usd: string;
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
  max_plan_cost_usd?: string;
}): Promise<Workspace> {
  return apiFetch("/workspace", { method: "PATCH", body: JSON.stringify(body) });
}


/** One row of the administration table, or of the switcher. */
export type WorkspaceSummary = {
  id: string;
  name: string;
  created_at: string;
  archived_at: string | null;
  /** Active members. Null for a caller who is not the system administrator. */
  member_count: number | null;
  /** Null when the caller reaches it as the system administrator only. */
  role: Role | null;
  is_member: boolean;
  current: boolean;
  /**
   * Set only by `createWorkspace`, and only when an admin email was given:
   * whether the invite was actually written. The toast says "an invite is on
   * its way" on the strength of this, not on the strength of having asked.
   */
  admin_invited?: boolean | null;
};

export type ArchiveResult = {
  id: string;
  name: string;
  archived_at: string;
  sessions_ended: number;
};

export function listWorkspaces(includeArchived = false): Promise<{
  workspaces: WorkspaceSummary[];
}> {
  return apiFetch(`/workspaces${includeArchived ? "?include_archived=true" : ""}`);
}

export function createWorkspace(body: {
  name: string;
  admin_email?: string;
}): Promise<WorkspaceSummary> {
  return apiFetch("/workspaces", { method: "POST", body: JSON.stringify(body) });
}

export function renameWorkspace(id: string, name: string): Promise<WorkspaceSummary> {
  return apiFetch(`/workspaces/${id}`, { method: "PATCH", body: JSON.stringify({ name }) });
}

export function archiveWorkspace(id: string): Promise<ArchiveResult> {
  return apiFetch(`/workspaces/${id}`, { method: "DELETE" });
}

export function restoreWorkspace(id: string): Promise<WorkspaceSummary> {
  return apiFetch(`/workspaces/${id}/restore`, { method: "POST" });
}

/**
 * Move this browser into another workspace.
 *
 * The API replaces the session, so the response is a whole new `Me` and the
 * caller must throw away everything it had cached for the old workspace —
 * `useSwitchWorkspace` in `lib/queries` does that by clearing the query cache
 * rather than by invalidating keys one at a time.
 */
export function switchWorkspace(workspaceId: string): Promise<Me> {
  return apiFetch("/auth/workspace", {
    method: "POST",
    body: JSON.stringify({ workspace_id: workspaceId }),
  });
}
