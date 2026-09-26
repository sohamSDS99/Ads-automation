/**
 * G8 / G8b — the per-asset AI media review (Stage 04 PRD §8.5, §15.4 H).
 *
 * Mirrors `agent.schemas.creative_review`. The card is what 4.4.5 / 4.4.7
 * proposed; once decided, the approval's `edited_proposal` is the same card
 * with every item's `decision` bound to it. A reviewer sends only
 * `edited_proposal.items[]` on the existing `POST /approvals/{id}` — the
 * server revalidates every item (four ticks for approve, a note for
 * regenerate, regenerate at G8 only) and refuses naming the asset.
 */
import { apiFetch } from "@/lib/api";
import type { ApprovalItem } from "@/lib/api/approvals";

export const REVIEW_GATES = ["G8", "G8b"] as const;
export type ReviewGate = (typeof REVIEW_GATES)[number];

export function isReviewGate(gateKey: string): gateKey is ReviewGate {
  return (REVIEW_GATES as readonly string[]).includes(gateKey);
}

export type ReviewDecision = "approve" | "reject" | "regenerate";

/** `ReviewChecklist`: each `false` until a person sets it. Only JSON `true` is a tick. */
export type ReviewChecklist = {
  label_ok: boolean;
  product_match_ok: boolean;
  subjects_ok: boolean;
  rights_ok: boolean;
};

export type ReviewRendition = {
  media_id: string;
  surface: string;
  ratio: string;
  px: string;
  derivation: string;
  bytes: number;
  lint_verdict: string | null;
  duration_ms: number | null;
  brand_first_at_ms: number | null;
  preview_media_id: string | null;
  poster_media_id: string | null;
};

export type ReviewAdvisory = { notes: string[]; flags: string[] };

export type ItemDecision = {
  decision: ReviewDecision;
  checklist: ReviewChecklist;
  note: string | null;
  model_override: string | null;
  params_override: Record<string, unknown> | null;
};

export type ReviewItem = {
  asset_id: string;
  kind: "image" | "video";
  campaign_ref: string;
  concept_id: string;
  regenerated_from: string | null;
  renditions: ReviewRendition[];
  disclosure: Record<string, unknown>;
  product_refs: string[];
  vision_advisory: ReviewAdvisory;
  decision: ItemDecision | null;
};

export type AiAssetReview = {
  status: "review" | "not_required";
  why: string | null;
  round: 1 | 2;
  items: ReviewItem[];
};

/** One item of `edited_proposal.items[]`. */
export type ReviewDecisionItem = {
  asset_id: string;
  decision: ReviewDecision;
  checklist: ReviewChecklist;
  note?: string;
};

/** `Approval.draft_state` — autosaved, never a decision. */
export type ReviewDraftItem = {
  asset_id: string;
  decision: ReviewDecision | null;
  checklist: ReviewChecklist;
  note: string | null;
};

export type ReviewDraft = {
  items: ReviewDraftItem[];
  cursor: string | null;
  saved_at?: string;
  saved_by?: string;
};

/** The card this approval asks about — or, decided, the card it recorded. */
export function reviewCard(approval: ApprovalItem): AiAssetReview {
  return (approval.edited_proposal ?? approval.proposal) as unknown as AiAssetReview;
}

/** `PUT /approvals/{id}/draft` — decider only, pending only; writes no decision. */
export function saveReviewDraft(approvalId: string, draft: ReviewDraft) {
  return apiFetch<{ approval_id: string; draft_state: ReviewDraft }>(
    `/approvals/${approvalId}/draft`,
    { method: "PUT", body: JSON.stringify({ draft_state: draft }) },
  );
}
