/**
 * Query keys and the hooks built on them.
 *
 * Centralised so an invalidation after a write names the same key the read did.
 * Getting that wrong is invisible — the screen just quietly shows stale data.
 */
"use client";

import { useInfiniteQuery, useQuery, type UseQueryResult } from "@tanstack/react-query";

import { listAudit, type AuditFilters } from "@/lib/api/audit";
import { listSessions } from "@/lib/api/account";
import { listApprovals, type ApprovalFilters } from "@/lib/api/approvals";
import { listCredentials } from "@/lib/api/credentials";
import { listEvidence, type EvidenceQuery } from "@/lib/api/evidence";
import { getModels } from "@/lib/api/models";
import { getProject, listProjectRuns, listProjects } from "@/lib/api/projects";
import { getReport } from "@/lib/api/reports";
import { getNodeRun, getRun, isLive } from "@/lib/api/runs";
import { listUsers } from "@/lib/api/users";
import { getRunDiff } from "@/lib/api/diff";
import { listSchedules } from "@/lib/api/schedules";
import { getStorageUsage } from "@/lib/api/storage";
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
  run: (runId: string) => ["runs", runId] as const,
  nodeRun: (runId: string, nodeId: string) => ["runs", runId, "nodes", nodeId] as const,
  approvals: (filters: ApprovalFilters) => ["approvals", filters] as const,
  report: (runId: string) => ["reports", runId] as const,
  evidence: (query: EvidenceQuery) => ["evidence", query] as const,
  schedules: (projectId?: string) => ["schedules", projectId ?? "all"] as const,
  storage: ["storage"] as const,
  runDiff: (runId: string, against?: string) => ["runs", runId, "diff", against ?? "parent"] as const,
};

/** How often the approvals badge asks again when no run is streaming (PRD §13.4 F). */
export const APPROVAL_POLL_MS = 60_000;

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

/**
 * One run, with its DAG and every node's state.
 *
 * No polling interval: a live run is pushed over SSE, and the console refetches
 * this on every reconnect. Polling as well would be a second source of truth
 * arriving at a different time.
 */
export function useRun(runId: string) {
  return useQuery({ queryKey: keys.run(runId), queryFn: () => getRun(runId) });
}

export function useNodeRun(runId: string, nodeId: string | null, enabled = true) {
  return useQuery({
    queryKey: keys.nodeRun(runId, nodeId ?? ""),
    queryFn: () => getNodeRun(runId, nodeId as string),
    enabled: enabled && Boolean(nodeId),
    // A node that has not run yet 404s, and asking four more times does not
    // change that. The console refetches on the SSE event instead.
    retry: false,
  });
}

/**
 * The approvals feed.
 *
 * Polled only when asked to: the inbox badge wants a heartbeat, a console
 * already has one over SSE and would be asking for the same rows twice.
 */
export function useApprovals(
  filters: ApprovalFilters,
  options: { pollMs?: number; enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: keys.approvals(filters),
    queryFn: () => listApprovals(filters),
    refetchInterval: options.pollMs ?? false,
    enabled: options.enabled ?? true,
  });
}

export function useReport(runId: string, enabled = true) {
  return useQuery({
    queryKey: keys.report(runId),
    queryFn: () => getReport(runId),
    enabled,
    retry: false,
  });
}

/** A page at a time, because the evidence table is the one screen with volume. */
export function useEvidence(query: EvidenceQuery, enabled = true) {
  return useInfiniteQuery({
    queryKey: keys.evidence(query),
    queryFn: ({ pageParam }) => listEvidence({ ...query, cursor: pageParam }),
    initialPageParam: null as string | null,
    getNextPageParam: (page) => page.next_cursor,
    enabled,
  });
}

export { isLive };

export function useSchedules(projectId?: string) {
  return useQuery({
    queryKey: keys.schedules(projectId),
    queryFn: () => listSchedules(projectId),
  });
}

export function useStorageUsage() {
  return useQuery({ queryKey: keys.storage, queryFn: getStorageUsage });
}

/**
 * The comparison behind the Report Viewer's toggle.
 *
 * Disabled until the toggle is on, so opening a report is one request rather
 * than two — most readers never compare, and the diff walks both payloads.
 */
export function useRunDiff(runId: string, against: string | undefined, enabled: boolean) {
  return useQuery({
    queryKey: keys.runDiff(runId, against),
    queryFn: () => getRunDiff(runId, against),
    enabled,
    // A finished run's report never changes, so neither does its diff.
    staleTime: Infinity,
    retry: false,
  });
}
