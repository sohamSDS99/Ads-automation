/**
 * The Media Library's reads and its one write (Stage 04 PRD §15.4 G, §16).
 *
 * The library renders what 4.4.1–4.4.4 stored (`GET /runs/{id}/nodes/{node}`,
 * the Stage 01 surface), the run's generation jobs and assets, and files
 * through `GET /media/{id}/content` — a 302 to the file server, which a
 * `<video>` seeks with `Range`. Mirrors `agent.schemas.creative_media`,
 * `agent.schemas.creative_video` and `agent.api.schemas_media_library`.
 *
 * Nothing here decides anything: lint verdicts, gaps and their reasons, byte
 * limits, verification scores and the price of a regeneration are the
 * server's, and are only displayed.
 */
import { API_BASE, apiFetch } from "@/lib/api";
import type { LintVerdict, ProductDepiction } from "@/lib/api/creative-runs";
import type { SpendMeter } from "@/lib/api/runs";

export type MediaVariant = "preview" | "poster" | "master";

/**
 * Where an `<img>` or a `<video>` loads one stored file from. The grid and the
 * board only ever ask for `preview` (§15.5 item 2); `master` is the detail
 * drawer's alone.
 */
export function mediaContentUrl(mediaId: string, variant: MediaVariant): string {
  return `${API_BASE}/media/${mediaId}/content?variant=${variant}`;
}

/** `MediaReferenceOut` — a product or style reference and its rights attestation. */
export type MediaReference = {
  id: string;
  project_id: string;
  kind: "product_reference" | "style_reference";
  origin: "own" | "licensed" | "third_party";
  product_ref: string | null;
  rights_statement: string;
  attested_by: string;
  attested_at: string;
  retired_at: string | null;
  media_type: string;
  width: number;
  height: number;
  bytes: number;
  sha256: string;
  created_at: string;
};

/** Every reference of the project, retired ones included (§16). */
export function listMediaReferences(projectId: string): Promise<MediaReference[]> {
  return apiFetch(`/projects/${projectId}/media-references`);
}

/** Where an `<img>` loads a reference from: a 302 to the file server (S4-P22). */
export function mediaReferenceContentUrl(referenceId: string): string {
  return `${API_BASE}/media-references/${referenceId}/content`;
}

// ---------------------------------------------------------------------------
// 4.4.1 creative_concepts
// ---------------------------------------------------------------------------

export type ImageSurface = "search_image" | "pmax_image" | "display_image" | "demand_gen_image";

export type Concept = {
  id: string;
  campaign_ref: string;
  name: string;
  /** A key into the brief: `angle`, `{ad_group_ref}:primary`, `{ad_group_ref}:angle_b`. */
  angle: string;
  /** The brief's own words for that angle. */
  angle_text: string;
  rationale: string;
  subject: string;
  setting: string;
  composition_by_ratio: Record<string, string>;
  palette_tokens: string[];
  product_depiction: ProductDepiction;
  prompt: string;
  negative_constraints: string[];
  surfaces: ImageSurface[];
};

export type CreativeConceptsOutput = {
  product_depiction: ProductDepiction;
  reference_refusals: Record<string, string | null>;
  campaigns: { campaign_ref: string; campaign_type: string; concepts: Concept[] }[];
};

// ---------------------------------------------------------------------------
// 4.4.2 image_masters
// ---------------------------------------------------------------------------

/** `image_verdict()` adds `indeterminate` to the text verdicts. */
export type ImageLintVerdict = LintVerdict | "indeterminate";

export type CandidateLint = {
  verdict: ImageLintVerdict;
  unchecked: boolean;
  ruleset_version: string;
  rule_ids: string[];
};

export type Candidate = {
  job_id: string;
  media_id: string;
  seed: number | null;
  attempt: 1 | 2;
  lint: CandidateLint;
  vision_advisory: { note: string; flags: string[] } | null;
};

export type ConceptMasters = {
  concept_id: string;
  campaign_ref: string;
  asset_id: string;
  aspect_ratio: string | null;
  reference_sha256s: string[];
  candidates: Candidate[];
  master: { media_id: string; why: string } | null;
  gap: {
    reason: "all_candidates_failed_lint" | "generation_failed" | "blocked_by_budget";
    detail: string;
  } | null;
};

export type ImageMastersOutput = { concepts: ConceptMasters[] };

// ---------------------------------------------------------------------------
// 4.4.3 image_renditions
// ---------------------------------------------------------------------------

export type Rendition = {
  concept_id: string;
  campaign_ref: string;
  asset_id: string;
  media_id: string;
  job_id: string | null;
  surface: ImageSurface;
  ratio: string;
  /** `1200x628`. */
  px: string;
  derivation: "native" | "relaid" | "crop";
  scale: { sx: number; sy: number };
  retained_saliency: number;
  logo_composited: boolean;
  logo_note: string | null;
  bytes: number;
  /** The slot's byte limit; null when none is set, absent on runs before S4-P21. */
  max_bytes?: number | null;
  lint: CandidateLint;
  /** `{xmp_digital_source_type, visible_labels[]}`, read back from the file. */
  disclosure: { xmp_digital_source_type?: string; visible_labels?: string[] } & Record<string, unknown>;
};

export type FittedLogo = {
  campaign_ref: string;
  asset_type: string;
  ratio: string;
  px: string;
  registered_logo_id: string;
  asset_id: string;
  media_id: string;
  scale: { sx: number; sy: number };
  bytes: number;
  surface?: string | null;
  max_bytes?: number | null;
  lint: CandidateLint;
};

export type RenditionGap = {
  campaign_ref: string;
  concept_id: string | null;
  surface: string;
  ratio: string;
  why: string;
};

export type ImageRenditionsOutput = {
  renditions: Rendition[];
  logos: FittedLogo[];
  gaps: RenditionGap[];
};

// ---------------------------------------------------------------------------
// 4.4.4 video_production
// ---------------------------------------------------------------------------

export type LogoFrame = {
  t_ms: number;
  detected: boolean;
  score: number;
  asset_id: string | null;
  status: string;
};

export type CaptionFrame = {
  index: number;
  t_ms: number;
  expected: string;
  read: string;
  similarity: number;
  passed: boolean;
};

export type VideoVerification = {
  passed: boolean;
  failures: string[];
  faststart: boolean;
  brand_first_at_ms: number | null;
  caption_ocr_min_similarity: number | null;
  /** The 1 fps samples of the first five seconds (`verify.SAMPLE_WINDOW_S`). */
  logo_frames: LogoFrame[];
  caption_frames: CaptionFrame[];
};

export type VideoRendition = {
  ratio: string;
  px: string;
  duration_ms: number;
  derivation: "native" | "crop";
  source_ratio: string;
  brand_first_at_ms: number;
  /** `video.brand_within_ms` and the end card's length; absent before S4-P21. */
  brand_window_ms?: number | null;
  end_card_ms?: number | null;
  captions_burned: boolean;
  caption_ocr_min_similarity: number | null;
  has_audio: boolean;
  bytes: number;
  disclosure: { xmp_digital_source_type?: string; mp4_comment?: string } & Record<string, unknown>;
  media_id: string;
  preview_media_id: string;
  poster_media_id: string;
  verification: VideoVerification;
  failed_attempts: { args: string; exit_code: number | null; stderr_tail: string }[];
};

export type VideoClip = {
  ratio: string;
  index: number;
  duration_s: number;
  job_id: string;
  status: string;
  media_id: string | null;
};

export type ScriptCaption = { t0: number; t1: number; text: string };

export type CampaignVideo = {
  campaign_ref: string;
  campaign_type: string;
  concept_id: string;
  asset_id: string;
  script_asset_id: string;
  duration_s: number;
  script: { duration_s: number; captions: ScriptCaption[]; cta: string };
  clips: VideoClip[];
  degraded: string[];
  renditions: VideoRendition[];
};

export type VideoGap = {
  campaign_ref: string;
  reason: string;
  detail: string;
  ratio: string | null;
  clip_index: number | null;
  job_id: string | null;
  verification: VideoVerification | null;
};

export type VideoProductionOutput = {
  status: "produced" | "not_required";
  why: string | null;
  videos: CampaignVideo[];
  gaps: VideoGap[];
};

// ---------------------------------------------------------------------------
// regeneration (§15.4 G, §16)
// ---------------------------------------------------------------------------

export type RegenerationRequest = {
  /** An allowlisted model of the asset's modality; omitted keeps the run's. */
  model_override?: string;
  provider_tag?: string | null;
  params_override?: Record<string, string | number | boolean>;
};

export type RegenerationEstimate = {
  asset_id: string;
  modality: "image" | "video";
  model_id: string;
  provider_tag: string | null;
  params: Record<string, unknown>[];
  /** One image; one request per planned clip of a video. */
  requests: number;
  estimate_usd: string;
  confidence: "high" | "medium" | "low";
  media: SpendMeter;
  remaining_usd: string;
  remaining_after_usd: string;
  fits: boolean;
};

/** The price of the regeneration as it would be submitted. No spend. */
export function estimateRegeneration(
  assetId: string,
  body: RegenerationRequest,
  signal?: AbortSignal,
): Promise<RegenerationEstimate> {
  return apiFetch(`/creative-assets/${assetId}/regeneration-estimate`, {
    method: "POST",
    body: JSON.stringify(body),
    signal,
  });
}

export type RegenerationAccepted = { job_id: string };

/**
 * `POST /creative-assets/{id}/regenerate` (§16): media, before G8. The route
 * re-checks capability and budget before it enqueues; this only sends what
 * the person chose.
 */
export function regenerateAsset(
  assetId: string,
  body: RegenerationRequest & { note: string },
): Promise<RegenerationAccepted> {
  return apiFetch(`/creative-assets/${assetId}/regenerate`, {
    method: "POST",
    body: JSON.stringify(body),
  });
}
