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

// ---------------------------------------------------------------------------
// Stage 2.2 — the budget gate, and the two node outputs its card reads
// ---------------------------------------------------------------------------

/**
 * These mirror `agent.nodes.plan.stage_2_2`. They are hand-written, like every
 * other contract in `lib/api/` — PRD §15.4 rule 5 asks for `zod` schemas
 * generated from the API's JSON Schema, and no generator exists in this repo
 * yet, so inventing a private one for three models here would leave the other
 * twenty contracts hand-written and the rule half-kept. Worth doing once, for
 * all of them, when `CampaignPlan` lands.
 *
 * Node output arrives as `Record<string, unknown>`, so each of the three has a
 * reader below that checks the fields the screen actually renders and returns
 * null otherwise. A cast would put a crash in a budget owner's card at the
 * moment they are trying to decide a gate.
 */

export type ScenarioName = "cautious" | "expected" | "aggressive";

/** One campaign x market x funnel stage, and what it is given. */
export type AllocationLine = {
  campaign_ref: string;
  market: string;
  funnel_stage: string;
  usd: number;
  pct: number;
  forecast_cpa_usd: number;
  target_cpa_usd: number;
  efficiency: number;
  est_conv: number;
  est_clicks: number | null;
  avg_cpc_usd: number | null;
  /** What this unit can absorb. Null means no measured ceiling, not "no limit". */
  max_spend_usd: number | null;
  floor_applied: boolean;
  cap_applied: boolean;
  below_floor: boolean;
};

export type BudgetScenario = {
  name: ScenarioName;
  monthly_total_usd: number;
  quarterly_total_usd: number;
  allocated_usd: number;
  unallocated_usd: number;
  working_budget_usd: number;
  experiment_reserve_usd: number;
  experiment_reserve_pct: number;
  est_clicks: number;
  est_conv: number;
  est_cpa: number | null;
  est_pipeline_usd: number | null;
  allocation: AllocationLine[];
  floor_applied: boolean;
  headroom_capped: boolean;
  case_for: string;
  case_against: string;
};

/** 2.2.3 — three envelopes, each already split across the plan. */
export type BudgetScenariosOutput = {
  scenarios: BudgetScenario[];
  recommended: ScenarioName;
  recommendation_reason: string;
  minimum_viable_envelope_usd: number;
  forecast_monthly_usd: number;
  forecast_cpa_usd: number | null;
  infeasible_reason: string | null;
  notes: string;
};

export type Envelope = {
  monthly_cap_usd: number;
  quarterly_cap_usd: number;
  currency: string;
  scenario_total_usd: number;
  unallocated_usd: number;
  unallocated_reason: string | null;
};

/** A campaign this envelope cannot get out of the learning period. */
export type LearningWarning = {
  campaign_ref: string;
  forecast_conv_30d: number;
  threshold: number;
  verdict: string;
  remedy: string | null;
};

export type ConfidenceBand = {
  /** `point_estimate` means there was no CPC range to widen the forecast with. */
  basis: "cpc_range" | "point_estimate";
  low_pct: number;
  high_pct: number;
};

/** 2.2.4 ⛳ G3 — the envelope and the split a human is asked to approve. */
export type BudgetAllocationProposal = {
  chosen_scenario: ScenarioName;
  rationale: string;
  what_would_change_it: string;
  envelope: Envelope;
  allocation: AllocationLine[];
  experiment_reserve_pct: number;
  experiment_reserve_usd: number;
  learning_warnings: LearningWarning[];
  confidence_band: ConfidenceBand;
  degraded_sources: string[];
};

/**
 * One month of the forecast.
 *
 * `month` is a label the plan chose — `2027-01`, `Jan` and `wave 2` are all
 * legal. Render it verbatim; parsing it as a date is how `wave 2` becomes
 * "Invalid Date" on somebody's axis.
 */
export type MonthlyTotals = {
  month: string;
  impressions: number;
  clicks: number;
  conversions: number;
  cost_usd: number;
  ctr_pct: number;
  avg_cpc_usd: number;
  cvr_pct: number;
  cpa_usd: number | null;
};

/** 2.2.1 — impressions to cost, per cluster per market per month. */
export type DemandForecastOutput = {
  monthly_totals: MonthlyTotals[];
  totals: Omit<MonthlyTotals, "month">;
  method: string;
  confidence_band: ConfidenceBand;
  impression_share_headroom_pct: number | null;
  method_notes: string;
  caveats: string[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The G3 proposal, or null when this gate is carrying something else. */
export function asBudgetProposal(value: unknown): BudgetAllocationProposal | null {
  if (!isRecord(value)) return null;
  if (!Array.isArray(value.allocation) || !isRecord(value.envelope)) return null;
  if (typeof value.envelope.monthly_cap_usd !== "number") return null;
  return value as unknown as BudgetAllocationProposal;
}

export function asScenariosOutput(value: unknown): BudgetScenariosOutput | null {
  if (!isRecord(value) || !Array.isArray(value.scenarios) || value.scenarios.length === 0) {
    return null;
  }
  return value as unknown as BudgetScenariosOutput;
}

export function asDemandForecast(value: unknown): DemandForecastOutput | null {
  if (!isRecord(value) || !isRecord(value.confidence_band)) return null;
  const months = value.monthly_totals;
  // A forecast with no monthly breakdown has no line to draw. The node can
  // legitimately produce one — a single-month plan — and the caller shows the
  // totals instead of an axis with one tick on it.
  if (!Array.isArray(months) || months.length < 2) return null;
  if (!months.every((row) => isRecord(row) && typeof row.month === "string")) return null;
  return value as unknown as DemandForecastOutput;
}
