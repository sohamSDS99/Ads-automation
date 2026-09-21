/**
 * Stage 02: the handshake between research and campaign planning.
 *
 * Eligibility is deliberately not computed here. The server returns the whole
 * list of what is wrong and the Start button reads it — a second opinion in
 * TypeScript would eventually disagree with the first, and the disagreement
 * would show up as a button that does nothing (Stage 02 PRD §4.2).
 */
import { apiFetch } from "@/lib/api";
import type { FieldChange, SectionDiff } from "@/lib/api/diff";
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

// ---------------------------------------------------------------------------
// the plan itself — the read side (Stage 02 PRD §15.3 D, E, F; §16)
// ---------------------------------------------------------------------------

/**
 * §12 `Number` — a figure with its calculation behind it.
 *
 * Every traced figure in the plan is one of these, which is what lets the
 * viewer put a citation chip on it. Sections still carry plain numbers where
 * §12 does (`media_plan.allocation[].usd`, priced keywords), so the renderer
 * below accepts either and only draws a chip when there is something to open.
 */
export type PlanNumber = {
  value: number;
  unit: "usd" | "pct" | "count" | "days" | "months" | "ratio";
  calc_evidence_id: string | null;
  confidence: "high" | "medium" | "low";
};

/** What a figure can arrive as. The payload is written by 2.6.1, not by us. */
export type PlanFigure = PlanNumber | number | string | null | undefined;

export function asPlanNumber(value: PlanFigure): PlanNumber | null {
  if (typeof value !== "object" || value === null) return null;
  return typeof (value as PlanNumber).value === "number" ? (value as PlanNumber) : null;
}

/** The scalar behind a figure, whichever shape it arrived in. */
export function figureValue(value: PlanFigure): number | null {
  const traced = asPlanNumber(value);
  if (traced) return traced.value;
  if (typeof value === "number") return value;
  if (typeof value === "string" && value.trim() !== "" && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return null;
}

export type PlanGateDecision = {
  gate_key: string;
  node_id: string;
  /** `approved` | `rejected` | `pending` | `expired` | `not_reached`. */
  status: string;
  decided_by: string | null;
  decided_by_name: string | null;
  decided_at: string | null;
  note: string | null;
  edited: boolean;
};

export type PlanCritique = {
  verdict: string | null;
  blocking: string[];
  advisory: string[];
  checked_at: string | null;
};

export type PlanTotals = {
  campaigns: number;
  ad_groups: number;
  keywords: number;
};

/**
 * `GET /plans/{plan_run_id}` — one plan version, whole.
 *
 * `payload` is the §12 object exactly as node 2.6.1 wrote it, deliberately
 * untyped past the sections this app reads. It is not validated here: §15.4
 * rule 5 asks for zod generated from the API's JSON Schema, and the API does
 * not declare a schema for it — the contract lives in `plan_contract.py` and is
 * filled by a phase that ships after this one. So each section reads what it
 * needs and renders an honest absence when it is not there, which is also the
 * behaviour a partial payload from an early plan run needs.
 *
 * Where `status` and `payload.plan_status` disagree, `status` wins: it is the
 * column the freeze transaction locks on, and a plan frozen a second ago
 * reports `frozen` here while its payload still says `ready_to_freeze`.
 */
export type PlanDetail = {
  id: string;
  project_id: string;
  plan_run_id: string;
  /** 0 until the plan is frozen. Rendered as an em dash, never as "version 0". */
  version: number;
  /** What a freeze would mint, and the number the freeze dialog asks for. */
  next_version: number;
  status: PlanStatus;
  schema_version: string;
  source_superseded: boolean;
  payload: Record<string, unknown>;
  markdown: string;
  frozen_at: string | null;
  frozen_by: string | null;
  frozen_by_name: string | null;
  frozen_approval_ids: string[];
  created_at: string;
  updated_at: string;
  source: AcceptedSource | null;
  gates: PlanGateDecision[];
  critique: PlanCritique | null;
  totals: PlanTotals;
};

export type PlanStructureKeyword = {
  term: string;
  match_type: string | null;
  forecast_cpc_usd: number | null;
  search_volume: number | null;
};

export type PlanStructureAdGroup = {
  name: string;
  theme: string | null;
  landing_url: string | null;
  primary_message: string | null;
  market: string | null;
  coherence: number | null;
  negatives: string[];
  /** `null` means unchecked — 2.4.1 emitted no regex — not "failed". */
  name_valid: boolean | null;
  keywords: PlanStructureKeyword[];
  keyword_count: number;
};

export type PlanStructureCampaign = {
  campaign_ref: string;
  name: string;
  type: string | null;
  market: string | null;
  language: string | null;
  monthly_budget_usd: number | null;
  daily_budget_usd: number | null;
  bid_strategy: string | null;
  target: number | null;
  locations: string[];
  negatives: string[];
  name_valid: boolean | null;
  /** 2.4.3's verdict — the learning-threshold badge. */
  verdict: string | null;
  threshold: number | null;
  forecast_conv_30d: number | null;
  action: string | null;
  remedy: string | null;
  reason: string | null;
  ad_groups: PlanStructureAdGroup[];
  ad_group_count: number;
  keyword_count: number;
};

export type PlanStructurePage = {
  plan_run_id: string;
  version: number;
  status: PlanStatus;
  /** The whole plan, never this page. */
  totals: PlanTotals;
  campaigns: PlanStructureCampaign[];
  next_cursor: string | null;
  validator_regex: string | null;
  /** `checked` or `skipped` — whether the live-account collision pass ran. */
  collision_check: string | null;
  /**
   * Names that failed the convention's regex.
   *
   * `null` means node 2.4.2 never checked; `[]` means it checked and every name
   * passed. The two must not be rendered the same way — a clean bill of health
   * on a tree nobody validated is the same error as a green tick on an
   * unchecked name.
   */
  invalid_names: string[] | null;
  /** Keywords in more than one ad group. Three-state, as above. */
  duplicate_terms: string[] | null;
  account_negatives: string[];
  orphan_terms: string[];
};

export type PlanDiff = {
  plan_run_id: string;
  against_plan_run_id: string;
  version: number;
  against_version: number;
  generated_at: string | null;
  against_generated_at: string | null;
  scalars: FieldChange[];
  sections: SectionDiff[];
  unchanged: boolean;
};

export function getPlan(planRunId: string): Promise<PlanDetail> {
  return apiFetch(`/plans/${planRunId}`);
}

export function getPlanStructure(
  planRunId: string,
  cursor?: string | null,
  limit?: number,
): Promise<PlanStructurePage> {
  const query = new URLSearchParams();
  if (cursor) query.set("cursor", cursor);
  if (limit) query.set("limit", String(limit));
  const suffix = query.size ? `?${query}` : "";
  return apiFetch(`/plans/${planRunId}/structure${suffix}`);
}

export function getPlanDiff(planRunId: string, against: string): Promise<PlanDiff> {
  return apiFetch(`/plans/${planRunId}/diff?against=${encodeURIComponent(against)}`);
}

/**
 * What a freeze answers with — a receipt, not the plan.
 *
 * Deliberately not a `PlanDetail`: it carries no payload, no gates and no
 * totals. Typing it as one and writing it into the plan cache would blank the
 * viewer behind the dialog on a *successful* freeze, which is the worst possible
 * moment for a screen to go empty.
 */
export type PlanFreezeResult = {
  plan_id: string;
  plan_run_id: string;
  project_id: string;
  version: number;
  status: PlanStatus;
  frozen_at: string | null;
  frozen_by: string | null;
  frozen_approval_ids: string[];
  /** Plan ids this freeze marked superseded. */
  superseded: string[];
  /** True on the idempotent 200 — already frozen at this same version. */
  already_frozen: boolean;
};

/**
 * `POST /plans/{plan_run_id}/freeze` — S2-P5b's transaction, this app's dialog.
 *
 * Idempotent on `confirm_version` (§16 rule 2): freezing an already-frozen plan
 * at the same version answers 200 with `already_frozen: true` rather than an
 * error, so a double submit is not a failure the approver has to interpret. A
 * mismatched version, an undecided gate or a blocking critique answers 409 with
 * a `blockers[]` array — plus `expected_version` and `submitted_version` on a
 * version race — which the dialog renders with the same component the
 * eligibility panel uses.
 */
export function freezePlan(
  planRunId: string,
  confirmVersion: number,
): Promise<PlanFreezeResult> {
  return apiFetch(`/plans/${planRunId}/freeze`, {
    method: "POST",
    body: JSON.stringify({ confirm_version: confirmVersion }),
  });
}

/**
 * A payload figure, read from the first path that answers.
 *
 * `export/plan_contract.py` names these fields and §12's sketch did not, so
 * every headline figure has the contract's spelling first and the node output's
 * second. Every section of that contract is `extra="allow"`, so a reader that
 * insists on one spelling goes blank on the next rename — and a blank figure on
 * a media plan is indistinguishable from one that is genuinely absent.
 *
 * The fallbacks are meant to decay. When `plan_contract.py` has been stable for
 * a while, the second entry in each list should be deleted rather than kept.
 */
export const FIGURE_PATHS = {
  monthlyEnvelope: ["media_plan.envelope.monthly_cap", "media_plan.envelope.monthly_cap_usd"],
  quarterlyEnvelope: [
    "media_plan.envelope.quarterly_cap",
    "media_plan.envelope.quarterly_cap_usd",
  ],
  experimentReserve: ["media_plan.experiment_reserve"],
  blendedTarget: ["objectives.blended_target_cpl", "objectives.blended_target_cpa_usd"],
  blendedCeiling: ["objectives.blended_max_cpa_won", "objectives.max_cpa_ceiling_usd"],
  blendedMaxCpl: ["objectives.blended_max_cpl"],
  northStar: ["objectives.north_star_target"],
} as const;
