/**
 * Stage 02: the handshake between research and campaign planning.
 *
 * Eligibility is deliberately not computed here. The server returns the whole
 * list of what is wrong and the Start button reads it — a second opinion in
 * TypeScript would eventually disagree with the first, and the disagreement
 * would show up as a button that does nothing (Stage 02 PRD §4.2).
 */
import { apiFetch } from "@/lib/api";
import type { LaunchReadiness } from "@/lib/api/reports";
import type { RunStatus } from "@/lib/api/projects";

/** Mirrors `agent.api.schemas_plan.BlockerCode`. */
export type BlockerCode =
  | "no_accepted_research"
  | "research_schema_unsupported"
  | "research_says_no_go"
  | "plan_in_flight"
  | "plan_already_frozen"
  | "missing_credential"
  | "source_stale"
  | "missing_permission";

export type Blocker = {
  code: BlockerCode;
  /** A whole sentence about this project. Rendered verbatim — never paraphrased. */
  detail: string;
  fix_url: string;
  severity: "blocker" | "warning";
};

export type AcceptedSource = {
  acceptance_id: string;
  research_run_id: string;
  research_report_id: string;
  research_schema_version: string;
  accepted_by: string;
  accepted_by_name: string;
  accepted_at: string;
  note: string | null;
  override_reason: string | null;
  launch_readiness: LaunchReadiness;
  degraded_sources: string[];
  age_days: number;
};

export type PlanEligibility = {
  eligible: boolean;
  blockers: Blocker[];
  source: AcceptedSource | null;
};

export type ResearchAcceptance = {
  id: string;
  project_id: string;
  run_id: string;
  report_id: string;
  accepted_by: string;
  accepted_by_name: string;
  accepted_at: string;
  note: string | null;
  launch_readiness_at_acceptance: string;
  override_reason: string | null;
  is_current: boolean;
};

export type PlanRunAccepted = {
  run_id: string;
  status: RunStatus;
  source_run_id: string;
  input_hash: string;
};

export type PlanStatus = "draft" | "blocked" | "ready_to_freeze" | "frozen" | "superseded";

export type PlanVersion = {
  id: string;
  plan_run_id: string;
  version: number;
  status: PlanStatus;
  schema_version: string;
  source_superseded: boolean;
  frozen_at: string | null;
  frozen_by: string | null;
  frozen_by_name: string | null;
  created_at: string;
};

/** Only the blockers that stop a run. Warnings are shown beside them, not with them. */
export function stopping(eligibility: PlanEligibility | undefined): Blocker[] {
  return (eligibility?.blockers ?? []).filter((item) => item.severity === "blocker");
}

export function advisory(eligibility: PlanEligibility | undefined): Blocker[] {
  return (eligibility?.blockers ?? []).filter((item) => item.severity === "warning");
}

export function getPlanEligibility(projectId: string): Promise<PlanEligibility> {
  return apiFetch<PlanEligibility>(`/projects/${projectId}/plan/eligibility`);
}

export function listPlans(projectId: string): Promise<{ items: PlanVersion[] }> {
  return apiFetch<{ items: PlanVersion[] }>(`/projects/${projectId}/plans`);
}

export function acceptResearch(
  runId: string,
  body: { note?: string | null; override_reason?: string | null },
): Promise<ResearchAcceptance> {
  return apiFetch<ResearchAcceptance>(`/runs/${runId}/accept`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function withdrawAcceptance(runId: string): Promise<ResearchAcceptance> {
  return apiFetch<ResearchAcceptance>(`/runs/${runId}/accept`, { method: "DELETE" });
}

export function startPlanRun(projectId: string): Promise<PlanRunAccepted> {
  return apiFetch<PlanRunAccepted>(`/projects/${projectId}/plan/runs`, { method: "POST" });
}

/**
 * One calculation behind one number in the plan (Stage 02 PRD §15.3 B).
 *
 * Law 14 says the model never does arithmetic: every figure a node asserts
 * comes from a registered `@formula` and leaves one of these rows behind. The
 * Calc tab renders them so an approver can audit a number without leaving the
 * console.
 */
export type PlanCalcRow = {
  id: string;
  node_id: string;
  /** The registry key, e.g. `economics.max_cpa_v1` — not a description. */
  formula_id: string;
  /** Constants-file version plus code version: what the number is reproducible against. */
  calc_version: string;
  inputs: Record<string, unknown>;
  result: Record<string, unknown>;
  /** Null once the `derived` Evidence row has been pruned. The calculation survives it. */
  evidence_id: string | null;
  created_at: string;
};

/** `GET /plans/{planRunId}/calcs` — oldest first, the order they computed in. */
export function listPlanCalcs(
  planRunId: string,
  nodeId?: string | null,
): Promise<{ items: PlanCalcRow[] }> {
  const query = nodeId ? `?node_id=${encodeURIComponent(nodeId)}` : "";
  return apiFetch<{ items: PlanCalcRow[] }>(`/plans/${planRunId}/calcs${query}`);
}
