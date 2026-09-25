/**
 * Query keys and the hooks built on them.
 *
 * Centralised so an invalidation after a write names the same key the read did.
 * Getting that wrong is invisible — the screen just quietly shows stale data.
 */
"use client";

import {
  keepPreviousData,
  useInfiniteQuery,
  useMutation,
  useQuery,
  useQueryClient,
  type UseQueryResult,
} from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useMemo } from "react";

import { listAudit, type AuditFilters } from "@/lib/api/audit";
import { listSessions } from "@/lib/api/account";
import { listAmendments } from "@/lib/api/amendments";
import { listApprovals, type ApprovalFilters } from "@/lib/api/approvals";
import { listClaims } from "@/lib/api/claims";
import { listHumanTasks, type TaskFilters } from "@/lib/api/tasks";
import { listConnections } from "@/lib/api/connections";
import {
  creativeBadges,
  creativeChip,
  estimateCreative,
  getCreativeEligibility,
  getCreativeOverview,
  isLegalExceptionTask,
  isLiveCreativeRun,
  lockSentence,
  startCreativeRun,
  type CreativeRequest,
} from "@/lib/api/creative";
import { listEvidence, type EvidenceQuery } from "@/lib/api/evidence";
import {
  getGuideline,
  getGuidelineAttention,
  getGuidelineEligibility,
  getPublishedRuleSet,
  listGuidelines,
  publishGuideline,
} from "@/lib/api/guidelines";
import {
  getMediaCatalogue,
  getMediaModels,
  getMediaSettings,
  type MediaModality,
} from "@/lib/api/media";
import { getModels } from "@/lib/api/models";
import {
  freezePlan,
  getPlan,
  getPlanDiff,
  getPlanEligibility,
  getPlanStructure,
  listPlanCalcs,
  listPlans,
} from "@/lib/api/plan";
import type { PlanCalcRow } from "@/lib/api/plan";
import { getProject, listProjectRuns, listProjects } from "@/lib/api/projects";
import { getReport } from "@/lib/api/reports";
import { getNodeRun, getRun, isLive } from "@/lib/api/runs";
import {
  checkGenerationJob,
  editCreativeAsset,
  getCreativeBrief,
  isJobInFlight,
  listCreativeAssets,
  listGenerationJobs,
  swapCreativeAsset,
  type CreativeAssetItem,
} from "@/lib/api/creative-runs";
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
  guidelineEligibility: (projectId: string) =>
    ["projects", projectId, "guidelines", "eligibility"] as const,
  guidelines: (projectId: string) => ["projects", projectId, "guidelines"] as const,
  guidelineAttention: (projectId: string) =>
    ["projects", projectId, "guidelines", "attention"] as const,
  guideline: (guidelineId: string) => ["guidelines", guidelineId] as const,
  publishedRuleSet: (projectId: string) =>
    ["projects", projectId, "guidelines", "ruleset"] as const,
  plans: (projectId: string) => ["projects", projectId, "plans"] as const,
  plan: (planRunId: string) => ["plans", planRunId] as const,
  planStructure: (planRunId: string) => ["plans", planRunId, "structure"] as const,
  planDiff: (planRunId: string, against: string) =>
    ["plans", planRunId, "diff", against] as const,
  planCalcs: (planRunId: string, nodeId: string) =>
    ["plans", planRunId, "calcs", nodeId] as const,
  // Stage 03 — S3-P8.
  claims: (guidelineId: string) => ["guidelines", guidelineId, "claims"] as const,
  humanTasks: (filters: TaskFilters) => ["human-tasks", filters] as const,
  signoffMatrix: (projectId: string) => ["projects", projectId, "signoff-matrix"] as const,
  matrixPreview: (projectId: string, legalOwnerId: string) =>
    ["projects", projectId, "signoff-matrix", "preview", legalOwnerId] as const,
  amendments: (filters: { status?: string; project_id?: string }) =>
    ["policy-amendments", filters] as const,
  // Stage 04. `creative` is a prefix of `creativeEligibility` on purpose: a
  // write that invalidates the stage invalidates whether it can start.
  creative: (projectId: string) => ["projects", projectId, "creative"] as const,
  creativeEligibility: (projectId: string) =>
    ["projects", projectId, "creative", "eligibility"] as const,
  mediaSettings: ["settings", "media"] as const,
  mediaCatalogue: (modality: MediaModality) => ["media", "catalogue", modality] as const,
  mediaModels: (modality: MediaModality, projectId: string) =>
    ["media", "models", modality, projectId] as const,
  // Under `creative(projectId)`, so a started run invalidates it with the rest.
  creativeEstimate: (projectId: string, request: string) =>
    ["projects", projectId, "creative", "estimate", request] as const,
  planCampaigns: (planRunId: string) => ["plans", planRunId, "campaigns"] as const,
  // A creative run's own reads, under `run(runId)`: whatever refreshes the run
  // — an SSE reconnect, a decided gate — refreshes these with it.
  creativeBrief: (runId: string) => ["runs", runId, "creative", "brief"] as const,
  creativeAssets: (runId: string) => ["runs", runId, "creative", "assets"] as const,
  generationJobs: (runId: string) => ["runs", runId, "creative", "generation-jobs"] as const,
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
/**
 * Stage 03 eligibility.
 *
 * Polls only while a run holds the lock, exactly as the plan version does —
 * and on `guideline_in_flight`, which is the *blocker* list. The warnings are
 * never a reason to poll: none of them resolves on its own.
 */
export function useGuidelineEligibility(projectId: string) {
  return useQuery({
    queryKey: keys.guidelineEligibility(projectId),
    queryFn: () => getGuidelineEligibility(projectId),
    enabled: Boolean(projectId),
    refetchInterval: (query) =>
      query.state.data?.blockers.some((item) => item.code === "guideline_in_flight")
        ? PLAN_POLL_MS
        : false,
  });
}

export function useGuidelines(projectId: string) {
  return useQuery({
    queryKey: keys.guidelines(projectId),
    queryFn: () => listGuidelines(projectId),
    enabled: Boolean(projectId),
  });
}

/**
 * One guideline version, payload included.
 *
 * Not polled. A draft's payload is written once, by 3.6.1, and the console's
 * SSE stream is what tells anybody that happened — the Rulebook Viewer is
 * opened *after* the run, and a poll here would re-fetch a 140KB payload every
 * few seconds to learn nothing.
 */
export function useGuideline(guidelineId: string | null) {
  return useQuery({
    queryKey: keys.guideline(guidelineId ?? ""),
    queryFn: () => getGuideline(guidelineId as string),
    enabled: Boolean(guidelineId),
  });
}

/**
 * What is waiting on a person, for the rail's badges and the landing's third block.
 *
 * `retry: false` because the only interesting failure is a project that is not
 * visible to this member, and retrying a 404 three times to draw two dots is
 * three requests too many.
 */
export function useGuidelineAttention(projectId: string) {
  return useQuery({
    queryKey: keys.guidelineAttention(projectId),
    queryFn: () => getGuidelineAttention(projectId),
    enabled: Boolean(projectId),
    retry: false,
  });
}

/**
 * The published ruleset.
 *
 * A `404` here means *nothing is published*, which is an answer this product
 * shows on purpose (contract rule 4) — so it is never retried and the caller
 * reads `isError` as "no published version", not as a failure.
 */
export function usePublishedRuleSet(projectId: string) {
  return useQuery({
    queryKey: keys.publishedRuleSet(projectId),
    queryFn: () => getPublishedRuleSet(projectId),
    enabled: Boolean(projectId),
    retry: false,
  });
}

/**
 * The ruleset at one exact pin — a creative run's, say — rather than whichever
 * is governing now. A pinned version never changes, so it is never re-read.
 */
export function usePinnedRuleSet(projectId: string, pin: string | null) {
  return useQuery({
    queryKey: [...keys.publishedRuleSet(projectId), "pin", pin ?? ""] as const,
    queryFn: () => getPublishedRuleSet(projectId, pin as string),
    enabled: Boolean(projectId) && Boolean(pin),
    staleTime: Infinity,
    retry: false,
  });
}

/**
 * Publish.
 *
 * The response is a receipt and shares no shape with `GuidelineDetail`, so
 * nothing here writes it into a read cache — every affected key is
 * invalidated instead. A `409` carries every blocker and is handled by the
 * dialog, which is why this does not swallow it.
 */
export function usePublishGuideline(guidelineId: string, projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (confirmVersion: number) => publishGuideline(guidelineId, confirmVersion),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.guideline(guidelineId) });
      void client.invalidateQueries({ queryKey: keys.guidelines(projectId) });
      void client.invalidateQueries({ queryKey: keys.publishedRuleSet(projectId) });
      void client.invalidateQueries({ queryKey: keys.guidelineAttention(projectId) });
      void client.invalidateQueries({ queryKey: keys.guidelineEligibility(projectId) });
    },
  });
}

/**
 * Stage 04 eligibility — the lock on the 04 entry and the landing's Action
 * block (PRD §15.1 rule 1). Never computed here: this is the only source of
 * "can creative start", and the rail and the landing read the same key.
 *
 * Polled only while a creative run holds the stage, as Stage 03 does; nothing
 * else on the blocker list resolves without somebody doing something, and
 * each of those somethings invalidates this key.
 */
export function useCreativeEligibility(projectId: string) {
  return useQuery({
    queryKey: keys.creativeEligibility(projectId),
    queryFn: () => getCreativeEligibility(projectId),
    enabled: Boolean(projectId),
    refetchInterval: (query) =>
      query.state.data?.blockers.some((item) => item.code === "creative_in_flight")
        ? PLAN_POLL_MS
        : false,
  });
}

/** Runs and package history (`GET /projects/{id}/creative`), polled while a run is live. */
export function useCreativeOverview(projectId: string) {
  return useQuery({
    queryKey: keys.creative(projectId),
    queryFn: () => getCreativeOverview(projectId),
    enabled: Boolean(projectId),
    refetchInterval: (query) => (isLiveCreativeRun(query.state.data?.runs[0]) ? PLAN_POLL_MS : false),
  });
}

/**
 * Everything the 04 entry and the landing say about the stage, composed from
 * the routes that already exist rather than a new one: eligibility and the
 * overview (§16), the latest run's nodes while it runs (for `14/24`), the
 * pending approvals on it while it is halted (G7, G8, G8b), and the project's
 * open H3 tasks (§16 rule 4 keeps H3 on the person-task surface).
 *
 * Each follow-up request is enabled only in the state that needs it, so an
 * idle project costs three reads, not five.
 */
export function useCreativeStatus(projectId: string, userId: string) {
  const eligibility = useCreativeEligibility(projectId);
  const overview = useCreativeOverview(projectId);
  const latest = overview.data?.runs[0];
  const live = isLiveCreativeRun(latest);
  const halted = latest?.status === "awaiting_approval";

  const run = useQuery({
    queryKey: keys.run(latest?.run_id ?? ""),
    queryFn: () => getRun(latest?.run_id as string),
    enabled: live && !halted && Boolean(latest?.run_id),
    refetchInterval: live && !halted ? PLAN_POLL_MS : false,
  });
  const gates = useApprovals(
    { run_id: latest?.run_id, status: "pending" },
    { enabled: halted && Boolean(latest?.run_id) },
  );
  const tasks = useHumanTasks({ project_id: projectId, status: "open" }, { enabled: Boolean(projectId) });

  return useMemo(() => {
    const legal = tasks.data?.items.filter(isLegalExceptionTask);
    const input = {
      overview: overview.data,
      run: run.data,
      gates: halted ? gates.data?.items : [],
      legal,
    };
    return {
      eligibility,
      overview,
      gates: input.gates ?? [],
      legal: legal ?? [],
      tasksLoaded: tasks.isSuccess,
      chip: creativeChip(input),
      badges: overview.data ? creativeBadges(input, userId) : [],
      lock: lockSentence(eligibility.data),
    };
  }, [eligibility, overview, run.data, gates.data, halted, tasks.data, tasks.isSuccess, userId]);
}

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

/**
 * Every calculation in a plan run, indexed by the evidence id a figure cites.
 *
 * The Plan Viewer puts a chip on every §12 `Number`, and a `Number` carries a
 * `calc_evidence_id` rather than the calculation itself. One request per chip
 * would be hundreds on a full plan, so the rows arrive once and the chips read
 * the map — the same shape `useCitations` uses for a report's evidence.
 *
 * `node_id` is deliberately unfiltered here: the viewer shows figures from
 * every stage at once, and the Calc tab is the caller that wants one node.
 */
export function usePlanCalcIndex(planRunId: string) {
  const query = useQuery({
    queryKey: keys.planCalcs(planRunId, ""),
    queryFn: () => listPlanCalcs(planRunId, null),
    enabled: Boolean(planRunId),
    staleTime: 5 * 60 * 1000,
  });

  const byEvidenceId = useMemo(() => {
    const index = new Map<string, PlanCalcRow>();
    for (const row of query.data?.items ?? []) {
      if (row.evidence_id) index.set(row.evidence_id, row);
    }
    return index;
  }, [query.data]);

  return { byEvidenceId, loading: query.isPending };
}

export function usePlan(planRunId: string) {
  return useQuery({
    queryKey: keys.plan(planRunId),
    queryFn: () => getPlan(planRunId),
    enabled: Boolean(planRunId),
  });
}

/**
 * The structure tree, one page of campaigns at a time.
 *
 * `useInfiniteQuery` rather than one request: §16 rule 4 paginates by campaign
 * precisely so a 4,000-keyword plan never arrives in one payload, and the tree
 * is virtualised over whatever has loaded so far.
 */
export function usePlanStructure(planRunId: string, enabled = true) {
  return useInfiniteQuery({
    queryKey: keys.planStructure(planRunId),
    queryFn: ({ pageParam }) => getPlanStructure(planRunId, pageParam),
    initialPageParam: null as string | null,
    getNextPageParam: (last) => last.next_cursor,
    enabled: enabled && Boolean(planRunId),
  });
}

export function usePlanDiff(planRunId: string | null, against: string | null) {
  return useQuery({
    queryKey: keys.planDiff(planRunId ?? "", against ?? ""),
    queryFn: () => getPlanDiff(planRunId as string, against as string),
    enabled: Boolean(planRunId && against),
  });
}

/**
 * Freezing a plan (§15.3 E). The transaction is S2-P5b's; this is the call.
 *
 * **Invalidated, never written into the cache.** The response is a receipt —
 * `{plan_id, version, status, superseded[], already_frozen}` — and not a
 * `PlanDetail`: no payload, no gates, no totals. An earlier version of this
 * hook did `setQueryData(keys.plan(...), result)`, which would have replaced
 * the viewer's plan with that receipt and blanked the screen behind the dialog
 * at the exact moment the freeze succeeded.
 *
 * Both keys go: a freeze mints a version, supersedes the previous one, and
 * changes the history table three screens away.
 */
export function useFreezePlan(planRunId: string, projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (confirmVersion: number) => freezePlan(planRunId, confirmVersion),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.plan(planRunId) });
      void client.invalidateQueries({ queryKey: keys.plans(projectId) });
      void client.invalidateQueries({ queryKey: keys.planStructure(planRunId) });
    },
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

/**
 * The workspace's media allowlist (`GET /settings/media`, READ). The editor is
 * admin-only, so the caller passes `enabled` rather than firing a read no
 * control on the page will use.
 */
export function useMediaSettings(enabled: boolean) {
  return useQuery({ queryKey: keys.mediaSettings, queryFn: getMediaSettings, enabled });
}

/**
 * The full live media catalogue for one modality (`GET /media/catalogue`,
 * SETTINGS_WRITE). `retry: false`: its interesting failures — no OpenRouter
 * key, a catalogue too stale to serve — do not change on a second ask.
 */
export function useMediaCatalogue(modality: MediaModality, enabled: boolean) {
  return useQuery({
    queryKey: keys.mediaCatalogue(modality),
    queryFn: () => getMediaCatalogue(modality),
    enabled,
    retry: false,
  });
}

/**
 * The models the Start dialog may offer for one modality (`GET /media/models`,
 * READ): the allowlist ∩ the live catalogue, each with its capability record
 * and its ratio coverage against this project's spec sheet.
 */
export function useMediaModels(modality: MediaModality, projectId: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.mediaModels(modality, projectId),
    queryFn: () => getMediaModels(modality, projectId),
    enabled,
    retry: false,
  });
}

/**
 * Every campaign of a frozen plan, for the Start dialog's checklist.
 *
 * All pages in one query, unlike `usePlanStructure`: a checklist that showed
 * the first page would scope a run to it without saying so.
 */
export function usePlanCampaigns(planRunId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: keys.planCampaigns(planRunId ?? ""),
    queryFn: async () => {
      const campaigns: { campaign_ref: string; name: string; type: string | null }[] = [];
      let cursor: string | null = null;
      do {
        const page = await getPlanStructure(planRunId as string, cursor);
        for (const campaign of page.campaigns) {
          campaigns.push({ campaign_ref: campaign.campaign_ref, name: campaign.name, type: campaign.type });
        }
        cursor = page.next_cursor;
      } while (cursor);
      return campaigns;
    },
    enabled: enabled && Boolean(planRunId),
    staleTime: Infinity, // a frozen plan does not change
  });
}

/**
 * The pre-flight estimate for exactly `request` (`POST /creative/estimate`).
 *
 * Keyed by the request itself, so the answer on screen is always the answer
 * *for* what is on screen — and toggling back to a scope already priced does
 * not ask again. `null` means the request is not complete yet (a modality is
 * on with no model), which is not a question worth sending. `retry: false`:
 * a 422 names a field and does not change on a second ask.
 */
export function useCreativeEstimate(projectId: string, request: CreativeRequest | null) {
  const serialised = request ? JSON.stringify(request) : "";
  return useQuery({
    queryKey: keys.creativeEstimate(projectId, serialised),
    queryFn: () => estimateCreative(projectId, request as CreativeRequest),
    enabled: request !== null,
    retry: false,
    staleTime: 60_000,
    // The last answer stays on screen, marked as updating, while the next is
    // priced — a bar that blanked on every toggle would read as $0.
    placeholderData: keepPreviousData,
  });
}

/**
 * Start a creative run. A `409` carries the server's blockers and a `422`
 * names the field, so the dialog handles both; on success everything under
 * the stage's key — overview, eligibility, the rail — is asked again.
 */
export function useStartCreativeRun(projectId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (request: CreativeRequest) => startCreativeRun(projectId, request),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.creative(projectId) });
    },
  });
}

export function useSessions() {
  return useQuery({ queryKey: keys.sessions, queryFn: listSessions });
}

/** How often a creative console re-reads its run, and its jobs while one moves. */
export const CREATIVE_POLL_MS = 5_000;

/** The brief on record (Stage 04 PRD §15.4 D). A 404 means 4.1.1 has not written it yet. */
export function useCreativeBrief(runId: string, enabled = true) {
  return useQuery({
    queryKey: keys.creativeBrief(runId),
    queryFn: () => getCreativeBrief(runId),
    enabled,
    retry: false,
  });
}

/** What the run wrote. The Assets tab filters it to one node for display. */
export function useCreativeAssets(runId: string, enabled = true) {
  return useQuery({
    queryKey: keys.creativeAssets(runId),
    queryFn: () => listCreativeAssets(runId),
    enabled,
  });
}

/**
 * Every media request of the run. Polled while a job is in flight — a video
 * moves on OpenRouter's side with no event of its own — or while a "Check
 * again" is waiting for the worker (`watching`).
 */
export function useGenerationJobs(runId: string, options: { enabled?: boolean; watching?: boolean } = {}) {
  return useQuery({
    queryKey: keys.generationJobs(runId),
    queryFn: () => listGenerationJobs(runId),
    enabled: options.enabled ?? true,
    refetchInterval: (query) =>
      options.watching || query.state.data?.items.some((job) => isJobInFlight(job.status))
        ? CREATIVE_POLL_MS
        : false,
  });
}

/** "Check again" (§16). The worker re-polls; the list is asked again at once. */
export function useCheckGenerationJob(runId: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) => checkGenerationJob(jobId),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.generationJobs(runId) });
    },
  });
}

/** Put the rows a write returned into the run's asset list, then re-read it. */
function useAssetWrite<Args, Result>(
  runId: string,
  write: (args: Args) => Promise<Result>,
  rows: (result: Result) => CreativeAssetItem[],
) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: write,
    onSuccess: (result) => {
      const changed = new Map(rows(result).map((row) => [row.id, row]));
      client.setQueryData<{ items: CreativeAssetItem[] }>(keys.creativeAssets(runId), (current) =>
        current ? { items: current.items.map((row) => changed.get(row.id) ?? row) } : current,
      );
      void client.invalidateQueries({ queryKey: keys.creativeAssets(runId) });
    },
  });
}

/** A person's rewrite of one headline or description (§16 PATCH /creative-assets/{id}). */
export function useEditCreativeAsset(runId: string) {
  return useAssetWrite(
    runId,
    ({ assetId, text }: { assetId: string; text: string }) => editCreativeAsset(assetId, text),
    (row) => [row],
  );
}

/** A reserve into the ad in place of one it carries (§16 POST /creative-assets/{id}/swap). */
export function useSwapCreativeAsset(runId: string) {
  return useAssetWrite(
    runId,
    ({ assetId, reserveId }: { assetId: string; reserveId: string }) => swapCreativeAsset(assetId, reserveId),
    (result) => [result.out, result.into],
  );
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
export function useRun(runId: string, options: { pollMs?: number } = {}) {
  return useQuery({
    queryKey: keys.run(runId),
    queryFn: () => getRun(runId),
    // Polled only when asked, and only while the run is live: the creative
    // console's spend meters move with media jobs, which the SSE feed does
    // not announce. Every other console is kept current by the stream alone.
    refetchInterval: (query) =>
      options.pollMs && query.state.data && isLive(query.state.data.status)
        ? options.pollMs
        : false,
  });
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

// ---------------------------------------------------------------------------
// Stage 03 — the claims register, person-tasks and the amendment inbox
// ---------------------------------------------------------------------------

/**
 * The register.
 *
 * `staleTime: 0` on purpose. Everywhere else a few seconds of staleness is a
 * kindness; here the response carries `set_hash`, and signing against a cached
 * hash is exactly the race the hash exists to catch. Paying a refetch is
 * cheaper than a 409 the signer has to recover from.
 */
export function useClaims(guidelineId: string) {
  return useQuery({
    queryKey: keys.claims(guidelineId),
    queryFn: () => listClaims(guidelineId),
    enabled: Boolean(guidelineId),
    staleTime: 0,
  });
}

export function useHumanTasks(
  filters: TaskFilters = {},
  options: { pollMs?: number; enabled?: boolean } = {},
) {
  return useQuery({
    queryKey: keys.humanTasks(filters),
    queryFn: () => listHumanTasks(filters),
    refetchInterval: options.pollMs ?? false,
    enabled: options.enabled ?? true,
  });
}

export function useAmendments(filters: { status?: string; project_id?: string } = {}) {
  return useQuery({
    queryKey: keys.amendments(filters),
    queryFn: () => listAmendments(filters),
  });
}
