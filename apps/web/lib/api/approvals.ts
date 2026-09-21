/**
 * The three gates (PRD §10) as the inbox and the console both see them.
 *
 * `can_decide` is computed by the server and carried on every item. The screen
 * renders from it rather than re-deriving "approver, or assignee, or admin"
 * in TypeScript: two implementations of one rule is how a card ends up offering
 * a button the API then refuses (PRD §18 law 6).
 */
import { apiFetch } from "@/lib/api";
import type { RunStatus } from "@/lib/api/projects";

export type ApprovalStatus = "pending" | "approved" | "rejected" | "expired";

export type ApprovalItem = {
  id: string;
  run_id: string;
  project_id: string;
  node_id: string;
  node_name: string;
  /** `G1`-`G4` for a plan gate, `R0` for a research one. What the card renders by. */
  gate_key: string;
  status: ApprovalStatus;
  required_role: "admin" | "approver";
  assignee_id: string | null;
  assignee_email: string | null;
  proposal: Record<string, unknown>;
  edited_proposal: Record<string, unknown> | null;
  /**
   * The last what-if the server ran against this gate, or null if nobody has
   * asked for one. Restored into the editor so a second approver opens the card
   * on the working somebody else already reasoned about, rather than an edited
   * budget with no account of what it buys.
   */
  recalc_state: Recalc | null;
  decision_note: string | null;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string;
  run_status: RunStatus;
  project_name: string | null;
  run_triggered_by_name: string | null;
  /** Hours this project allows the gate, or null when it set none. */
  sla_hours: number | null;
  can_decide: boolean;
};

export type ApprovalDecision = {
  approval: ApprovalItem;
  run_status: RunStatus;
  resumed: boolean;
};

export type ApprovalFilters = {
  run_id?: string;
  /** Only gates this caller may act on — the inbox's whole premise. */
  mine?: boolean;
  status?: ApprovalStatus;
};

export function listApprovals(
  filters: ApprovalFilters = {},
): Promise<{ items: ApprovalItem[]; next_cursor: string | null }> {
  const query = new URLSearchParams();
  if (filters.run_id) query.set("run_id", filters.run_id);
  if (filters.mine) query.set("mine", "true");
  if (filters.status) query.set("status", filters.status);
  const suffix = query.size > 0 ? `?${query}` : "";
  return apiFetch(`/approvals${suffix}`);
}

export function decideApproval(
  approvalId: string,
  body: { decision: "approve" | "reject"; note?: string; edited_proposal?: Record<string, unknown> },
): Promise<ApprovalDecision> {
  return apiFetch(`/approvals/${approvalId}`, { method: "POST", body: JSON.stringify(body) });
}

export function reassignApproval(
  approvalId: string,
  assigneeId: string | null,
): Promise<ApprovalItem> {
  return apiFetch(`/approvals/${approvalId}/assignee`, {
    method: "PATCH",
    body: JSON.stringify({ assignee_id: assigneeId }),
  });
}

/** What the gate is actually asking about, whichever way it was answered. */
export function currentProposal(approval: ApprovalItem): Record<string, unknown> {
  return approval.edited_proposal ?? approval.proposal;
}

/**
 * How a gate stands against its SLA.
 *
 * Returns null when the project set no allowance — most do not, and inventing a
 * deadline nobody agreed to would make every gate look late.
 */
export function slaState(
  approval: ApprovalItem,
  now = Date.now(),
): { hoursLeft: number; late: boolean } | null {
  if (approval.sla_hours === null) return null;
  const due = Date.parse(approval.created_at) + approval.sla_hours * 3_600_000;
  const hoursLeft = (due - now) / 3_600_000;
  return { hoursLeft, late: hoursLeft < 0 };
}

// ---------------------------------------------------------------------------
// The budget gate's what-if
// ---------------------------------------------------------------------------

/** One edited line. Money and the unit it belongs to — deliberately no rate. */
export type RecalcLine = {
  campaign_ref: string;
  market: string;
  funnel_stage: string;
  /** What this line should get. Zero switches it off; it is not "below the floor". */
  usd: number;
};

/** One re-forecast line, as `allocation.whatif_v1` returns it. */
export type RecalcRow = {
  campaign_ref: string;
  market: string;
  funnel_stage: string;
  /** What was asked for. */
  requested_usd: number;
  /** What can actually be spent, after this line's absorption cap. */
  usd: number;
  wasted_usd: number;
  pct: number;
  baseline_usd: number;
  delta_usd: number;
  delta_pct: number | null;
  forecast_cpa_usd: number;
  est_conv: number;
  est_clicks: number | null;
  below_floor: boolean;
  cap_applied: boolean;
};

/**
 * What the edit would buy (Stage 02 PRD §15.3 C item 4).
 *
 * Every figure here is the server's. The editor holds no forecast arithmetic at
 * all — §15.4 rule 2 calls a duplicated formula in the frontend a bug rather
 * than an optimisation, and this is the response that makes keeping that rule
 * free.
 */
export type Recalc = {
  approval_id: string;
  envelope_usd: number;
  requested_usd: number;
  effective_usd: number;
  wasted_usd: number;
  delta_usd: number;
  delta_pct: number;
  envelope_breach: boolean;
  tolerance_pct: number;
  est_conv: number;
  est_cpa_usd: number | null;
  baseline_usd: number;
  allocation: RecalcRow[];
  capped: { unit: string; requested_usd: number; absorbable_usd: number }[];
  below_floor: { unit: string; usd: number; floor_usd: number }[];
  /** Lines sent that the proposal does not contain. Ignored by the server, and named. */
  unknown_lines: string[];
  switched_off: string[];
  /** Put this on the edited proposal so the approved figures cite the calculation. */
  calc_evidence_id: string;
  summary: string;
};

/**
 * Re-forecast an edited split without deciding the gate.
 *
 * The run does not move and no model is called, so this is safe to call as
 * often as an approver wants to try a number.
 */
export function recalcApproval(
  approvalId: string,
  body: { allocation: RecalcLine[]; envelope_usd?: number },
): Promise<Recalc> {
  return apiFetch(`/approvals/${approvalId}/recalc`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
