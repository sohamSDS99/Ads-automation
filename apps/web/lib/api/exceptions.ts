/**
 * H3 — legal exceptions, non-delegable (Stage 04 PRD §8.6, §15.4 I, §16 H3).
 *
 * Mirrors `agent.api.schemas_creative_exceptions`. Who may clear is the
 * server's answer in two places: the H3 person-task's `can_submit` (the named
 * assignee) and `POST …/exceptions/clear`, which narrows CLAIM_SIGN to the
 * project's named legal owner — no role fallback, no administrator. Nothing
 * here re-derives it.
 */
import { apiFetch } from "@/lib/api";

export type ExceptionKind = "new_claim" | "disclaimer" | "image_right";
export type ExceptionStatus = "open" | "cleared" | "rejected" | "withdrawn";
export type ExceptionDecision = "cleared" | "rejected";

export type Swapped = { out: string; into: string | null };

export type CreativeExceptionItem = {
  exception_id: string;
  kind: ExceptionKind;
  status: ExceptionStatus;
  subject: string | null;
  asset_ids: string[];
  occurrences: number;
  evidence_ids: string[];
  proposed: Record<string, unknown>;
  fallback_asset_ids: string[];
  decided_by: string | null;
  decided_at: string | null;
  decision_note: string | null;
  claim_record_id: string | null;
  signature_id: string | null;
  /** Open only: what rejecting this one exception does to its assets. */
  if_rejected: Swapped[] | null;
};

export type ExceptionSet = {
  run_id: string;
  /** The register hash of H3's set — echoed on clear, never computed here. */
  set_hash: string | null;
  exceptions: CreativeExceptionItem[];
  task: { task_id: string; status: string; assignee_id: string } | null;
};

export type ClearDecisionIn = { exception_id: string; decision: ExceptionDecision; note?: string };

export type ClearReceipt = {
  run_id: string;
  /** The set with these decisions (`decided_hash`). */
  set_hash: string;
  /** The set as it was read. */
  register_hash: string;
  decided_by: string;
  decided_at: string;
  statement: string;
  signature_id: string | null;
  ruleset_version: string | null;
  cleared: string[];
  rejected: string[];
  swapped: Swapped[];
  resumed: boolean;
};

export type WithdrawPreview = {
  run_id: string;
  exception_ids: string[];
  swapped: Swapped[];
  swaps: number;
  drops: number;
  h3_ends: boolean;
};

export type WithdrawResult = {
  run_id: string;
  withdrawn: string[];
  swapped: Swapped[];
  h3_status: "required" | "not_required";
  set_hash: string | null;
  resumed: boolean;
};

/** What the legal owner attests to when clearing H3. */
export const H3_STATEMENT =
  "I have reviewed each exception, its substantiation and what ships if it is rejected, " +
  "and I am authorised to decide it. Each cleared claim is licensed for advertising from " +
  "this signature until it expires or is revoked.";

export const KIND_LABEL: Record<ExceptionKind, string> = {
  new_claim: "New claim",
  disclaimer: "Disclaimer",
  image_right: "Image right",
};

export function listCreativeExceptions(runId: string): Promise<ExceptionSet> {
  return apiFetch(`/creative-runs/${runId}/exceptions`);
}

/**
 * `POST …/exceptions/clear`. `reauthToken` is an argument, never read from
 * anywhere, so there is no place it could be cached (Stage 03's rule).
 */
export function clearCreativeExceptions(
  runId: string,
  body: { decisions: ClearDecisionIn[]; setHash: string; reauthToken: string },
): Promise<ClearReceipt> {
  return apiFetch(`/creative-runs/${runId}/exceptions/clear`, {
    method: "POST",
    body: JSON.stringify({
      decisions: body.decisions,
      statement: H3_STATEMENT,
      set_hash: body.setHash,
      reauth_token: body.reauthToken,
    }),
  });
}

export function previewWithdrawExceptions(runId: string, exceptionIds: string[]): Promise<WithdrawPreview> {
  return apiFetch(`/creative-runs/${runId}/exceptions/withdraw-preview`, {
    method: "POST",
    body: JSON.stringify({ exception_ids: exceptionIds }),
  });
}

export function withdrawCreativeExceptions(runId: string, exceptionIds: string[]): Promise<WithdrawResult> {
  return apiFetch(`/creative-runs/${runId}/exceptions/withdraw`, {
    method: "POST",
    body: JSON.stringify({ exception_ids: exceptionIds }),
  });
}

/** H3's set: open exceptions the legal owner is asked about. */
export function openExceptions(set: ExceptionSet | undefined): CreativeExceptionItem[] {
  return (set?.exceptions ?? []).filter((item) => item.status === "open");
}

/** "swaps 7 assets to fallbacks and drops 1" — the consequence, in numbers. */
export function withdrawConsequence(count: number, preview: Pick<WithdrawPreview, "swaps" | "drops">): string {
  const plural = (n: number, one: string, many: string) => `${n} ${n === 1 ? one : many}`;
  return (
    `Withdrawing ${plural(count, "exception", "exceptions")} swaps ${plural(preview.swaps, "asset", "assets")} ` +
    `to fallbacks and drops ${preview.drops}`
  );
}
