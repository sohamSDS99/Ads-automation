/**
 * A creative run's own reads (Stage 04 PRD §16): the brief, what the run
 * wrote, its generation jobs, and "Check again" — and the Ad Studio's lint
 * preview, edit and reserve swap.
 *
 * Mirrors `agent.api.schemas_creative_runs` and `agent.schemas.creative_brief`.
 * Nothing here decides anything. `authorises` is G7's own reading of the brief
 * it hashes, `can_check` is the server's "would it do anything, and may you
 * ask", and whether someone may decide G7 is `ApprovalItem.can_decide` on the
 * approvals surface — the brief page reads it from there, never from here.
 */
import { apiFetch } from "@/lib/api";
import type { LintResult } from "@/lib/api/lint";

export type SourceStage = "S1" | "S2" | "S3";

/** Where a brief line comes from: a stage, a node, and what in it. */
export type SourceRef = {
  stage: SourceStage;
  node_id: string;
  evidence_ids: string[];
  rule_id: string | null;
  field: string | null;
};

/** Law 1 applied to the brief: every line carries at least one source. */
export type BriefLine = { text: string; sources: SourceRef[] };

export type AdGroupBrief = {
  campaign_ref: string;
  ad_group_ref: string;
  theme: string;
  primary_message: BriefLine;
  top_keywords: string[];
  landing_url: string;
  kpi: string;
  /** The variant-B angle, decided up front (4.2.4 writes B from it). */
  angle_b: BriefLine;
};

/** §12.2. References only — no number or date in an offer is model-written. */
export type OfferBinding = {
  offer_record_id: string;
  sku_or_set: string;
  fields: Record<string, string>;
  resolved: Record<string, string>;
};

export type NonNegotiables = {
  voice_words: string[];
  never_terms: string[];
  required_terms: string[];
  disclosures: string[];
};

export type ProductDepiction = "reference_guided" | "composited_real" | "none";

export type VisualConstraints = {
  permitted_subjects: string[];
  forbidden_subjects: string[];
  palette_tokens: string[];
  product_depiction: ProductDepiction;
};

/** What the approver authorises spend on, copied from `calc/`'s estimate. */
export type MediaPlanSummary = {
  images: boolean;
  video: boolean;
  jobs: Record<string, number>;
  ratios: Record<string, Record<string, string>>;
  media_usd: string;
  total_usd: string;
  confidence: string;
  fits: boolean;
  calc_evidence_ids: string[];
};

/** A claim licensed at the pin (law 34). */
export type ClaimRef = {
  claim_id: string;
  normalized_text: string;
  surface_forms: string[];
  status: "draft" | "approved" | "rejected" | "expired";
  market_scope: string[];
  languages: string[];
  expires_at: string | null;
};

export type CreativeBrief = {
  schema_version: "1.0";
  creative_run_id: string;
  plan_ref: { plan_id: string; version: number; schema_version: string; plan_run_id: string };
  ruleset_ref: { guideline_id: string; ruleset_version: string; hash: string };
  objective: BriefLine;
  audience: BriefLine[];
  exclusions: BriefLine[];
  angle: BriefLine;
  proof_points: ClaimRef[];
  offer: OfferBinding | null;
  non_negotiables: NonNegotiables;
  ad_groups: AdGroupBrief[];
  visual_constraints: VisualConstraints;
  media_plan: MediaPlanSummary;
  rendered_word_count: number;
  brief_hash: string;
};

/** "Authorises 20 RSAs, 36 images, 4 videos and up to $38.40 of media spend". */
export type BriefAuthorisation = { rsas: number; images: number; videos: number; media_usd: string };

export type CreativeBriefView = {
  run_id: string;
  brief: CreativeBrief;
  brief_hash: string;
  /** Set once G7 approves — then equal to `brief_hash`, which is frozen. */
  approved_hash: string | null;
  /** Counted by the server over the rendered brief; never re-counted here. */
  word_count: number;
  max_words: number;
  authorises: BriefAuthorisation;
  /** The G7 gate deciding this brief; its state is on the approvals surface. */
  approval_id: string | null;
};

export type LintVerdict = "pass" | "pass_with_warnings" | "fail";

export type CreativeAssetKind =
  | "headline"
  | "long_headline"
  | "description"
  | "path"
  | "sitelink"
  | "callout"
  | "structured_snippet"
  | "promotion"
  | "price"
  | "lead_form"
  | "business_name"
  | "video_script"
  | "image"
  | "video"
  | "logo";

export type CreativeAssetStatus =
  | "draft"
  | "linted"
  | "reserve"
  | "awaiting_review"
  | "approved"
  | "rejected"
  | "dropped"
  | "awaiting_exception"
  | "released";

export type CreativeAssetItem = {
  id: string;
  node_id: string;
  campaign_ref: string;
  ad_group_ref: string | null;
  ad_ref: string | null;
  kind: CreativeAssetKind;
  surface: string;
  variant: "A" | "B" | null;
  category: string | null;
  text: string | null;
  fields: Record<string, unknown>;
  claim_ids: string[];
  pin_position: string | null;
  generated_by_ai: boolean;
  status: CreativeAssetStatus;
  /** As the linter returned it when the asset was created (law 33). */
  lint_verdict: LintVerdict | null;
  ruleset_version: string | null;
  lineage: Record<string, unknown>;
  content_hash: string;
  frozen_at: string | null;
  created_at: string;
};

export type GenerationStatus =
  | "queued"
  | "submitting"
  | "submitted"
  | "in_progress"
  | "completed"
  | "failed"
  | "cancelled"
  | "expired"
  | "timed_out"
  | "blocked_by_budget"
  | "unknown_submit_state";

export type GenerationJobItem = {
  id: string;
  node_id: string;
  asset_id: string | null;
  round: number;
  modality: "image" | "video";
  model_id: string;
  provider_tag: string | null;
  status: GenerationStatus;
  estimate_usd: string;
  /** What OpenRouter billed; null until it answers. */
  cost_usd: string | null;
  attempts: number;
  polls: number;
  error: Record<string, unknown> | null;
  submitted_at: string | null;
  completed_at: string | null;
  created_at: string;
  updated_at: string;
  /** Would "Check again" do anything here, and may this caller ask. */
  can_check: boolean;
};

export type GenerationCheckAccepted = {
  job_id: string;
  status: GenerationStatus;
  /** False when an identical check is already waiting for the worker. */
  queued: boolean;
};

export function getCreativeBrief(runId: string): Promise<CreativeBriefView> {
  return apiFetch(`/creative-runs/${runId}/brief`);
}

export function listCreativeAssets(runId: string): Promise<{ items: CreativeAssetItem[] }> {
  return apiFetch(`/creative-runs/${runId}/assets`);
}

export function listGenerationJobs(runId: string): Promise<{ items: GenerationJobItem[] }> {
  return apiFetch(`/creative-runs/${runId}/generation-jobs`);
}

export function checkGenerationJob(jobId: string): Promise<GenerationCheckAccepted> {
  return apiFetch(`/generation-jobs/${jobId}/check`, { method: "POST" });
}

/** A job that has not reached an end state — what the Jobs tab polls for. */
export function isJobInFlight(status: GenerationStatus): boolean {
  return (
    status === "queued" ||
    status === "submitting" ||
    status === "submitted" ||
    status === "in_progress"
  );
}

// ---------------------------------------------------------------------------
// editing the brief at G7
// ---------------------------------------------------------------------------

/**
 * An approver's edits, keyed by where the text sits in the brief. Only the
 * words of a line move: `revalidate_edit` refuses any change to what code
 * wrote (plan facts, sources, the media plan), so those are never offered.
 */
export type BriefEdits = Record<string, string>;

/** Every place an approver may rewrite, with the text on record there. */
export function editableLines(brief: CreativeBrief): [path: string, text: string][] {
  return [
    ["objective", brief.objective.text],
    ...brief.audience.map((line, i): [string, string] => [`audience.${i}`, line.text]),
    ...brief.exclusions.map((line, i): [string, string] => [`exclusions.${i}`, line.text]),
    ["angle", brief.angle.text],
    ...brief.ad_groups.flatMap((group, i): [string, string][] => [
      [`ad_groups.${i}.theme`, group.theme],
      [`ad_groups.${i}.primary_message`, group.primary_message.text],
      [`ad_groups.${i}.angle_b`, group.angle_b.text],
    ]),
  ];
}

/**
 * The proposal with the edited words put back where they came from — the
 * `edited_proposal` G7's decision carries. The server revalidates and
 * re-hashes it; nothing here decides whether the edit is acceptable.
 */
export function applyBriefEdits(
  proposal: Record<string, unknown>,
  edits: BriefEdits,
): Record<string, unknown> {
  const next = structuredClone(proposal) as Record<string, unknown>;
  for (const [path, text] of Object.entries(edits)) {
    const parts = path.split(".");
    let target: Record<string, unknown> = next;
    for (const part of parts.slice(0, -1)) {
      target = (Array.isArray(target) ? target[Number(part)] : target[part]) as Record<string, unknown>;
    }
    const last = parts.at(-1)!;
    const slot = Array.isArray(target) ? target[Number(last)] : target[last];
    if (slot !== null && typeof slot === "object" && "text" in slot) {
      (slot as { text: string }).text = text;
    } else {
      target[last] = text;
    }
  }
  return next;
}

// ---------------------------------------------------------------------------
// the Ad Studio (§15.4 E): what 4.2.1–4.2.4 judged, and the three writes
// ---------------------------------------------------------------------------

/** Mirrors `agent.schemas.search_ads`. Read from the nodes' stored outputs. */
export type HeadlineCategory = "keyword" | "benefit" | "offer" | "proof" | "objection" | "cta";
export type PairKind = "HH" | "HD" | "DD";
export type PairFlag =
  | "duplicate"
  | "near_duplicate"
  | "offer_conflict"
  | "cta_collision"
  | "claim_conflict"
  | "keyword_stuffing";
export type PairLabel = "reads_well" | "redundant" | "contradictory" | "order_dependent";
export type PinPosition = "H1" | "H2" | "H3" | "D1" | "D2";

export type LintRef = { verdict: LintVerdict; ruleset_version: string; rule_ids: string[] };

export type HeadlineCandidate = {
  asset_id: string;
  text: string;
  default_text: string;
  category: HeadlineCategory;
  keyword_ref: string | null;
  claim_ids: string[];
  dki: boolean;
  lint: LintRef;
  outcome: "selected" | "reserve" | "dropped" | "failed_lint";
  reason: string | null;
};

export type QuotaLine = { category: string; required: number; selected: number; available: number };

export type HeadlineGroup = {
  campaign_ref: string;
  ad_group_ref: string;
  campaign_type: string;
  market: string;
  language: string;
  variant: "A" | "B";
  candidates: HeadlineCandidate[];
  selected: string[];
  reserve: string[];
  quota_report: { lines: QuotaLine[]; limit: number; selected: number; met: boolean };
  near_duplicate_trigram: number;
};

export type DescriptionItem = {
  asset_id: string;
  text: string;
  claim_ids: string[];
  claim_span: [number, number];
  lint: LintRef;
};

export type DescriptionGroup = {
  campaign_ref: string;
  ad_group_ref: string;
  variant: "A" | "B";
  descriptions: DescriptionItem[];
  paths: [string | null, string | null];
  reserve: DescriptionItem[];
};

export type Pair = { a: string; b: string; kind: PairKind; flags: PairFlag[]; label: PairLabel };

export type PairReport = {
  campaign_ref: string;
  ad_group_ref: string;
  variant: "A" | "B";
  headlines: string[];
  descriptions: string[];
  pairs: Pair[];
  swaps: { out: string; in_from_reserve: string; why: string }[];
  pins: { asset_id: string; position: PinPosition; why: string }[];
  repair_rounds: number;
  unresolved: [string, string][];
};

export type ResponsiveSearchAd = {
  ad_ref: string;
  campaign_ref: string;
  ad_group_ref: string;
  variant: "A" | "B";
  angle: string;
  hypothesis: string | null;
  headlines: string[];
  descriptions: string[];
  paths: [string | null, string | null];
  final_url: string;
  pair_report: PairReport;
  distinctness_vs_a: number | null;
};

export type VariantBGroup = {
  campaign_ref: string;
  ad_group_ref: string;
  headlines: HeadlineGroup;
  descriptions: DescriptionGroup;
  ad_b: ResponsiveSearchAd;
  distinctness_vs_a: number;
  variant_min_distance: number;
  distinctness_metric: string;
  hypothesis: string;
  primary_metric: string;
};

export type HeadlineSpreadOutput = { ad_groups: HeadlineGroup[] };
export type ClaimBoundDescriptionsOutput = { ad_groups: DescriptionGroup[] };
export type CombinationCoherenceOutput = { ads: PairReport[] };
export type VariantBOutput = { ad_groups: VariantBGroup[] };

/** `agent.schemas.guardrails.LintTarget` — what the lint preview is asked about. */
export type CreativeLintTarget = {
  ref: string;
  surface: string;
  campaign_type: string;
  market: string;
  language: string;
  text: string;
  generated_by_ai: boolean;
};

/**
 * The pinned linter's verdict on text nobody has saved (§16). No side effects,
 * so it runs on a debounce; `signal` aborts the one a newer keystroke replaced,
 * or a stale answer would overwrite a fresher one.
 */
export function lintPreview(runId: string, targets: CreativeLintTarget[], signal?: AbortSignal) {
  return apiFetch<LintResult>(`/creative-runs/${runId}/lint-preview`, {
    method: "POST",
    body: JSON.stringify({ targets }),
    signal,
  });
}

/** A person's rewrite (§16): stored only if its node's checks and the pin pass. */
export function editCreativeAsset(assetId: string, text: string): Promise<CreativeAssetItem> {
  return apiFetch(`/creative-assets/${assetId}`, { method: "PATCH", body: JSON.stringify({ text }) });
}

export type ReserveSwapResult = { out: CreativeAssetItem; into: CreativeAssetItem };

/** A reserve into the ad in place of `assetId`, re-linted at the pin first (§16). */
export function swapCreativeAsset(assetId: string, reserveId: string): Promise<ReserveSwapResult> {
  return apiFetch(`/creative-assets/${assetId}/swap`, {
    method: "POST",
    body: JSON.stringify({ with_reserve_id: reserveId }),
  });
}
