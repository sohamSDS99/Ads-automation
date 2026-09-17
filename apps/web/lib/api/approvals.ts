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
  status: ApprovalStatus;
  required_role: "admin" | "approver";
  assignee_id: string | null;
  assignee_email: string | null;
  proposal: Record<string, unknown>;
  edited_proposal: Record<string, unknown> | null;
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
