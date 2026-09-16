/**
 * Query keys and the hooks built on them.
 *
 * Centralised so an invalidation after a write names the same key the read did.
 * Getting that wrong is invisible — the screen just quietly shows stale data.
 */
"use client";

import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { listAudit, type AuditFilters } from "@/lib/api/audit";
import { listSessions } from "@/lib/api/account";
import { listCredentials } from "@/lib/api/credentials";
import { getModels } from "@/lib/api/models";
import { getProject, listProjectRuns, listProjects } from "@/lib/api/projects";
import { listUsers } from "@/lib/api/users";
import { getWorkspace } from "@/lib/api/workspace";

export const keys = {
  projects: ["projects"] as const,
  project: (id: string) => ["projects", id] as const,
  projectRuns: (id: string) => ["projects", id, "runs"] as const,
  credentials: ["credentials"] as const,
  models: ["models"] as const,
  users: ["users"] as const,
  workspace: ["workspace"] as const,
  audit: (filters: AuditFilters) => ["audit", filters] as const,
  sessions: ["sessions"] as const,
};

export function useProjects() {
  return useQuery({ queryKey: keys.projects, queryFn: listProjects });
}

export function useProject(id: string) {
  return useQuery({ queryKey: keys.project(id), queryFn: () => getProject(id) });
}

export function useProjectRuns(id: string) {
  return useQuery({ queryKey: keys.projectRuns(id), queryFn: () => listProjectRuns(id) });
}

export function useCredentials() {
  return useQuery({ queryKey: keys.credentials, queryFn: listCredentials });
}

/**
 * The model catalogue.
 *
 * `enabled` rather than an unconditional fetch: the endpoint needs
 * `settings_write` and an OpenRouter key, so an operator opening the wizard
 * would otherwise generate a 403 and a 409 on every visit.
 */
export function useModels(enabled: boolean) {
  return useQuery({
    queryKey: keys.models,
    queryFn: () => getModels(),
    enabled,
    // The catalogue is the same for everyone and the API caches it too; this
    // stops a tab switch from asking again.
    staleTime: 10 * 60 * 1000,
    retry: false,
  });
}

export function useUsers() {
  return useQuery({ queryKey: keys.users, queryFn: listUsers });
}

export function useWorkspace() {
  return useQuery({ queryKey: keys.workspace, queryFn: getWorkspace });
}

export function useAudit(filters: AuditFilters) {
  return useQuery({ queryKey: keys.audit(filters), queryFn: () => listAudit(filters) });
}

export function useSessions() {
  return useQuery({ queryKey: keys.sessions, queryFn: listSessions });
}

/** The one-line message for a failed query, without leaking a stack trace. */
export function errorMessage(query: UseQueryResult<unknown, unknown>): string | null {
  if (!query.isError) return null;
  const error = query.error;
  return error instanceof Error ? error.message : "Something went wrong.";
}
