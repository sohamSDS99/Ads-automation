/**
 * The sign-off matrix: who owns brand, legal and performance sign-off.
 *
 * Changing the legal owner voids every signature the outgoing owner made, and
 * the number is counted by the server before anybody confirms. That count is
 * the whole reason this screen exists (PRD §15.3 C.6).
 */
import { apiFetch } from "@/lib/api";

export type OwnerSlot = {
  user_id: string;
  name: string | null;
  email: string | null;
  role: string | null;
};

export type SignOffMatrix = {
  id: string;
  project_id: string;
  brand_owner: OwnerSlot;
  legal_owner: OwnerSlot;
  performance_owner: OwnerSlot;
  version: number;
  set_by: string;
  set_by_name: string | null;
  set_at: string;
  can_edit: boolean;
};

export type SignOffMatrixState = {
  /** Null is a normal early state — G6 has not been decided — never an error. */
  matrix: SignOffMatrix | null;
  eligible_owners: OwnerSlot[];
  /** Only `approver`. Law 23: an admin can never hold the legal signature. */
  eligible_legal_owners: OwnerSlot[];
  can_edit: boolean;
};

export type MatrixChangePreview = {
  legal_owner_changes: boolean;
  from_legal_owner_id: string | null;
  from_legal_owner_name: string | null;
  to_legal_owner_id: string | null;
  to_legal_owner_name: string | null;
  voided_signature_ids: string[];
  requeued_claim_ids: string[];
  voided_count: number;
  requeued_count: number;
  reason_required: boolean;
};

export function getSignOffMatrix(projectId: string) {
  return apiFetch<SignOffMatrixState>(`/projects/${projectId}/signoff-matrix`);
}

export function previewMatrixChange(projectId: string, legalOwnerId: string) {
  return apiFetch<MatrixChangePreview>(
    `/projects/${projectId}/signoff-matrix/preview?legal_owner_id=${encodeURIComponent(legalOwnerId)}`,
  );
}

export function putSignOffMatrix(
  projectId: string,
  body: {
    brand_owner_id: string;
    legal_owner_id: string;
    performance_owner_id: string;
    reason?: string;
  },
) {
  return apiFetch<SignOffMatrixState>(`/projects/${projectId}/signoff-matrix`, {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export function patchGuidelineApprovers(
  projectId: string,
  body: { G5?: string | null; G6?: string | null },
) {
  return apiFetch<Record<string, string | null>>(
    `/projects/${projectId}/guideline-approvers`,
    { method: "PATCH", body: JSON.stringify(body) },
  );
}

export function ownerLabel(slot: OwnerSlot | null | undefined): string {
  if (!slot) return "Not named";
  return slot.name || slot.email || slot.user_id;
}
