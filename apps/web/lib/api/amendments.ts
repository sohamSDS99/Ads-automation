/**
 * Reasons the published rulebook should change.
 *
 * `change_kind` is the axis the whole screen turns on, and the four values are
 * not a severity scale — `signature_affecting` is not "worse substantive", it
 * is a different event with a different consequence (PRD §8.6).
 */
import { apiFetch } from "@/lib/api";

export type AmendmentChangeKind =
  | "mechanical"
  | "substantive"
  | "signature_affecting"
  | "unclassified";

export type AmendmentStatus =
  | "open"
  | "needs_review"
  | "applied"
  | "dismissed"
  | "auto_applied";

export type AmendmentOrigin = "policy_watch" | "claim_expiry" | "disapproval" | "manual";

export type VoidedSignature = {
  signature_id: string;
  signer_id: string;
  signer_name: string | null;
  signed_at: string;
  voided_at: string | null;
  void_reason: string | null;
  claim_count: number;
};

export type Amendment = {
  id: string;
  project_id: string;
  project_name: string | null;
  origin: AmendmentOrigin | string;
  detected_at: string;
  change_kind: AmendmentChangeKind | string;
  status: AmendmentStatus | string;
  source_id: string | null;
  source_label: string | null;
  source_url: string | null;
  diff: Record<string, unknown> | null;
  proposed_rule_changes: Record<string, unknown> | null;
  rationale: string | null;
  applied_ruleset_version: string | null;
  reviewed_by: string | null;
  reviewed_by_name: string | null;
  reviewed_at: string | null;
  review_note: string | null;
  /** Named, not counted — §15.3 G. A count cannot say whose signature went. */
  voided_signatures: VoidedSignature[];
  requeued_claim_ids: string[];
  can_decide: boolean;
};

export type AmendmentList = { items: Amendment[]; open_count: number };

export function listAmendments(filters: { status?: string; project_id?: string } = {}) {
  const query = new URLSearchParams();
  if (filters.status) query.set("status", filters.status);
  if (filters.project_id) query.set("project_id", filters.project_id);
  const suffix = query.size > 0 ? `?${query}` : "";
  return apiFetch<AmendmentList>(`/policy-amendments${suffix}`);
}

export function applyAmendment(amendmentId: string) {
  return apiFetch<{ amendment: Amendment; ruleset_version: string | null }>(
    `/policy-amendments/${amendmentId}/apply`,
    { method: "POST" },
  );
}

export function dismissAmendment(amendmentId: string, reason: string) {
  return apiFetch<{ amendment: Amendment; ruleset_version: string | null }>(
    `/policy-amendments/${amendmentId}/dismiss`,
    { method: "POST", body: JSON.stringify({ reason }) },
  );
}

/** What each class means, in the words the inbox uses. */
export const KIND_COPY: Record<string, { label: string; detail: string }> = {
  mechanical: {
    label: "Mechanical",
    detail: "A value inside an existing rule changed. Applied automatically.",
  },
  substantive: {
    label: "Substantive",
    detail: "A rule appeared or disappeared. Never applies itself.",
  },
  signature_affecting: {
    label: "Signature affecting",
    detail: "Touches a rule a named person personally signed for.",
  },
  unclassified: {
    label: "Needs triage",
    detail: "The classifier was not confident enough. Handled as substantive.",
  },
};

export const ORIGIN_LABEL: Record<string, string> = {
  policy_watch: "Policy page changed",
  claim_expiry: "A claim expired",
  disapproval: "Google disapproved an ad",
  manual: "Raised by hand",
};
