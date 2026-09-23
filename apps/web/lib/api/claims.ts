/**
 * The claims register and the one signature an administrator cannot forge.
 *
 * Two things in this file are load-bearing and easy to "simplify" into bugs:
 *
 * 1. **`set_hash` comes from the server and is echoed back untouched.** It is
 *    the hash of the set the signer actually read. A client that computed its
 *    own could not detect a register that moved underneath them, which is the
 *    only thing the hash is for (PRD §16 rule 2).
 * 2. **The re-auth token is never stored.** `reauth()` returns it, the caller
 *    passes it straight into `signClaims()`, and it goes out of scope. It is
 *    never React state, never a query cache entry, never a log line.
 */
import { apiFetch } from "@/lib/api";

export type ClaimStatus =
  | "unsupported"
  | "pending_signoff"
  | "approved"
  | "rejected"
  | "expired";

export type ClaimType =
  | "superlative"
  | "comparative"
  | "quantified"
  | "certification"
  | "guarantee"
  | "pricing"
  | "other";

export type ClaimRiskTier = "low" | "medium" | "high";

export type ClaimSummary = {
  id: string;
  claim_text: string;
  /** What the hash is computed over. Shown in the step-up dialog's detail view. */
  normalized_text: string;
  /** What the signature actually licenses. The most important column in the drawer. */
  surface_forms: string[];
  claim_type: ClaimType | string;
  market_scope: string[];
  languages: string[];
  risk_tier: ClaimRiskTier | string;
  status: ClaimStatus | string;
  expires_at: string | null;
  evidence_ids: string[];
  current_signature_id: string | null;
};

export type ClaimList = {
  claims: ClaimSummary[];
  /** Null when there is nothing signable. Never recomputed here. */
  set_hash: string | null;
  /** Lets the table say "awaiting signature from …" to everyone who is not them. */
  legal_owner_id: string | null;
};

export type ClaimDecision = {
  claim_id: string;
  normalized_text: string;
  decision: "approved" | "rejected";
  note?: string | null;
  expires_at?: string | null;
};

export type SignatureReceipt = {
  signature_id: string;
  signer_id: string;
  set_hash: string;
  statement: string;
  method: string;
  signed_at: string;
  expires_at: string | null;
  approved_count: number;
  rejected_count: number;
  voided_at: string | null;
  void_reason: string | null;
};

export function listClaims(guidelineId: string) {
  return apiFetch<ClaimList>(`/guidelines/${guidelineId}/claims`);
}

export function editClaim(
  guidelineId: string,
  claimId: string,
  patch: {
    claim_text?: string;
    surface_forms?: string[];
    market_scope?: string[];
    languages?: string[];
  },
) {
  return apiFetch<ClaimSummary>(`/guidelines/${guidelineId}/claims/${claimId}`, {
    method: "PATCH",
    body: JSON.stringify(patch),
  });
}

/**
 * Mint a single-use step-up proof.
 *
 * Deliberately returns the token rather than stashing it: the only correct
 * lifetime for this value is the few milliseconds between the password field
 * and the sign request, and a module-level variable would outlive that.
 */
export function reauth(password: string) {
  return apiFetch<{ token: string; expires_in: number }>("/auth/reauth", {
    method: "POST",
    body: JSON.stringify({ password }),
    redirectOn401: false,
  });
}

export function signClaims(
  guidelineId: string,
  body: {
    decisions: ClaimDecision[];
    statement: string;
    set_hash: string;
    reauth_token: string;
  },
) {
  return apiFetch<SignatureReceipt>(`/guidelines/${guidelineId}/claims/sign`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function getSignature(claimId: string) {
  return apiFetch<SignatureReceipt>(`/claims/${claimId}/signature`);
}

export function revokeSignature(claimId: string, reason: string) {
  return apiFetch<SignatureReceipt>(`/claims/${claimId}/revoke`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

/** The attestation the signer confirms, stored verbatim on the signature. */
export const ATTESTATION =
  "I confirm that I have read each claim in this set, that the approved claims are " +
  "substantiated by the evidence attached to them, and that I accept responsibility " +
  "for their use in advertising until they expire or are revoked.";

/** Claims a signature can still be routed to. Mirrors `routes_claims.SIGNABLE`. */
export const SIGNABLE: ReadonlySet<string> = new Set([
  "unsupported",
  "pending_signoff",
  "expired",
]);

export function isSignable(claim: ClaimSummary): boolean {
  return SIGNABLE.has(claim.status);
}

/**
 * How close a claim is to expiring, or null when it never does.
 *
 * Returns days rather than a formatted string so the caller can decide between
 * "in 24 days" and a date; the 30-day threshold is §15.3 A's, not a guess.
 */
export function expiryState(
  claim: ClaimSummary,
  now = Date.now(),
): { days: number; soon: boolean; expired: boolean } | null {
  if (!claim.expires_at) return null;
  const days = Math.floor((Date.parse(claim.expires_at) - now) / 86_400_000);
  return { days, soon: days <= 30, expired: days < 0 };
}
