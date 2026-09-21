/**
 * Query keys and the hooks built on them.
 *
 * Centralised so an invalidation after a write names the same key the read did.
 * Getting that wrong is invisible — the screen just quietly shows stale data.
 */
"use client";

import {
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useRouter } from "next/navigation";

import { listAudit, type AuditFilters } from "@/lib/api/audit";
import { listSessions } from "@/lib/api/account";
import { listApprovals, type ApprovalFilters } from "@/lib/api/approvals";
import { listConnections } from "@/lib/api/connections";
import { listEvidence, type EvidenceQuery } from "@/lib/api/evidence";
import { getModels } from "@/lib/api/models";
import { getPlanEligibility, listPlanCalcs, listPlans } from "@/lib/api/plan";
import { getProject, listProjectRuns, listProjects } from "@/lib/api/projects";
import { getReport } from "@/lib/api/reports";
import { getNodeRun, getRun, isLive } from "@/lib/api/runs";
import { listUsers } from "@/lib/api/users";
import { getRunDiff } from "@/lib/api/diff";
import { listSchedules } from "@/lib/api/schedules";
import { getStorageUsage } from "@/lib/api/storage";
import { listAccounts } from "@/lib/api/platform";
import { getWorkspace, listWorkspaces, switchWorkspace } from "@/lib/api/workspace";

export const keys = {
  projects: ["projects"] as const,
  project: (id: string) => ["projects", id] as const,
  projectRuns: (id: string) => ["projects", id, "runs"] as const,
  connections: ["connections"] as const,
  models: ["models"] as const,
  users: ["users"] as const,
  workspace: ["workspace"] as const,
  workspaces: (includeArchived: boolean) => ["workspaces", includeArchived] as const,
  accounts: ["platform", "accounts"] as const,
  audit: (filters: AuditFilters) => ["audit", filters] as const,
  sessions: ["sessions"] as const,
  run: (runId: string) => ["runs", runId] as const,
  nodeRun: (runId: string, nodeId: string) => ["runs", runId, "nodes", nodeId] as const,
  approvals: (filters: ApprovalFilters) => ["approvals", filters] as const,
  report: (runId: string) => ["reports", runId] as const,
  evidence: (query: EvidenceQuery) => ["evidence", query] as const,
  schedules: (projectId?: string) => ["schedules", projectId ?? "all"] as const,
  storage: ["storage"] as const,
  documents: (projectId: string) => ["projects", projectId, "documents"] as const,
  runDiff: (runId: string, against?: string) => ["runs", runId, "diff", against ?? "parent"] as const,
  planEligibility: (projectId: string) => ["projects", projectId, "plan", "eligibility"] as const,
  plans: (projectId: string) => ["projects", projectId, "plans"] as const,
  planCalcs: (planRunId: string, nodeId: string) =>
    ["plans", planRunId, "calcs", nodeId] as const,
};

/** How often the approvals badge asks again when no run is streaming (PRD §13.4 F). */
export const APPROVAL_POLL_MS = 60_000;

/** How often the Stage 02 landing re-asks while a plan run holds the lock. */
export const PLAN_POLL_MS = 10_000;

export function useProjects() {
  return useQuery({ queryKey: keys.projects, queryFn: listProjects });
}

/**
 * One project.
 *
 * `enabled` defaults to on, and an empty id turns it off regardless: the
 * settings tabs render before a project has been chosen, and firing
 * `GET /projects/` at the API is a request that can only fail.
 */
export function useProject(id: string, enabled = true) {
  return useQuery({
    queryKey: keys.project(id),
    queryFn: () => getProject(id),
    enabled: enabled && Boolean(id),
  });
}

export function useProjectRuns(id: string) {
  return useQuery({ queryKey: keys.projectRuns(id), queryFn: () => listProjectRuns(id) });
}

/**
 * Whether campaign planning can start, and what is stopping it (Stage 02 §4.2).
 *
 * Polled while a plan run holds the lock, so the Start button re-enables when
 * that run finishes without anyone reloading the page. Left alone otherwise:
 * eligibility only changes when somebody does something, and every one of
 * those somethings invalidates this key.
 */
export function usePlanEligibility(projectId: string) {
  return useQuery({
    queryKey: keys.planEligibility(projectId),
    queryFn: () => getPlanEligibility(projectId),
    enabled: Boolean(projectId),
    refetchInterval: (query) =>
      query.state.data?.blockers.some((item) => item.code === "plan_in_flight")
        ? PLAN_POLL_MS
        : false,
  });
}

/**
 * The calculations one node of a plan run produced (Stage 02 PRD §15.3 B).
 *
 * Keyed by node because that is how the Calc tab asks: an approver reading one
 * node wants that node's arithmetic, not the run's. Unlike `useNodeRun` this
 * does not 404 on a node that has not run — it answers `[]`, so an empty list
 * and "nothing computed here" are the same state and need no error branch.
 */
export function usePlanCalcs(planRunId: string, nodeId: string | null, enabled = true) {
  return useQuery({
    queryKey: keys.planCalcs(planRunId, nodeId ?? ""),
    queryFn: () => listPlanCalcs(planRunId, nodeId),
    enabled: enabled && Boolean(nodeId),
  });
}

export function usePlans(projectId: string) {
  return useQuery({
    queryKey: keys.plans(projectId),
    queryFn: () => listPlans(projectId),
    enabled: Boolean(projectId),
  });
}

export function useConnections() {
  return useQuery({ queryKey: keys.connections, queryFn: listConnections });
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


/**
 * Every workspace this caller can open.
 *
 * `enabled` so the administration screen can ask for archived ones without a
 * second hook, and so the switcher never fires it for a caller who has only
 * the one workspace in `me.workspaces`.
 */
export function useWorkspaces(includeArchived = false, enabled = true) {
  return useQuery({
    queryKey: keys.workspaces(includeArchived),
    queryFn: () => listWorkspaces(includeArchived),
    enabled,
  });
}

export function useAccounts(enabled = true) {
  return useQuery({ queryKey: keys.accounts, queryFn: listAccounts, enabled });
}

/**
 * Switch workspace, then forget everything.
 *
 * `clear()`, not `invalidateQueries()`. Every cached list, project, run and
 * report belongs to the workspace it was fetched in, and invalidating leaves
 * the stale data on screen while the refetch is in flight — which means a
 * beat during which someone is looking at another company's project names.
 * `router.refresh()` afterwards re-runs the server layout so the shell picks
 * up the new session.
 */
export function useSwitchWorkspace() {
  const queryClient = useQueryClient();
  const router = useRouter();
  return useMutation({
    mutationFn: (workspaceId: string) => switchWorkspace(workspaceId),
    onSuccess: () => {
      queryClient.clear();
      router.push("/");
      router.refresh();
    },
  });
}
