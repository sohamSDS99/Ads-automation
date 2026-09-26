/**
 * Stage 04's QA and package reads (PRD §15.3 `…/qa`, `…/package`, the
 * released package's canonical URL and `/compare`; §16 "Package, release and
 * the Stage 05 contract").
 *
 * Mirrors `agent.schemas.creative_package`, `agent.api.schemas_creative_packages`,
 * `agent.creative.package_diff` and the QA reads of `agent.api.routes_creative_runs`.
 * Every verdict here is the server's: 4.6.4's preview verdict and its
 * truncation, 4.6.1's conformance verdict, 4.7.2's thirteen checks, whether a
 * package is releasable and the version release would mint. The screens render
 * them; nothing in this module computes one.
 */
import { API_BASE, apiFetch } from "@/lib/api";

// ---------------------------------------------------------------------------
// 4.6.4 previews and 4.6.1 conformance
// ---------------------------------------------------------------------------

export type PreviewDevice = "mobile" | "desktop";
export type PreviewVerdict = "pass" | "warning" | "blocking" | "unavailable";

/** A box in the capture, in CSS px at scale 1 (`agent.schemas.landing.Box`). */
export type PreviewBox = { x: number; y: number; width: number; height: number };

export type PreviewElementMetrics = {
  key: string;
  asset_id: string | null;
  scroll_width: number;
  client_width: number;
  overflow_px: number;
  clipped: boolean;
  /** Absent on a preview measured before S4-P23 recorded boxes. */
  box?: PreviewBox | null;
  /** The block that clips it — where a clipped element is cut off. */
  clip_box?: PreviewBox | null;
};

/** `serp.PreviewRender.dom()` — advisory pixel facts (D12). */
export type PreviewDom = {
  truncated?: string[];
  overflow_px?: { element: string; asset_id: string | null; px: number }[];
  elements?: PreviewElementMetrics[];
  frame?: PreviewBox | null;
  requests?: number;
  error?: string;
};

export type SpecMismatch = { element: string; asset_id: string; constraint: "max_chars"; expected: number; measured: number };
export type SpecCount = { asset_type: string; constraint: "min_count" | "max_count"; expected: number; measured: number };
export type SpecUnchecked = { element: string; asset_id: string; surface: string; reason: "spec_missing" };

/** `conformance.SpecDiff` — the rendered combination against the pin's specs; the only source of `blocking`. */
export type PreviewSpecDiff = {
  missing?: SpecCount[];
  extra?: SpecCount[];
  mismatched?: SpecMismatch[];
  unchecked?: SpecUnchecked[];
};

export type PreviewCombination = {
  roles?: string[];
  headlines?: string[];
  descriptions?: string[];
  paths?: (string | null)[];
  likelihood?: number;
  model?: string;
};

export type RenderPreviewItem = {
  id: string;
  creative_run_id: string;
  ad_ref: string;
  device: PreviewDevice;
  combination: PreviewCombination;
  has_screenshot: boolean;
  dom_metrics: PreviewDom;
  spec_diff: PreviewSpecDiff;
  visual_diff: Record<string, unknown> | null;
  template_version: string;
  verdict: PreviewVerdict;
  created_at: string;
};

export function listRenderPreviews(runId: string): Promise<{ items: RenderPreviewItem[] }> {
  return apiFetch(`/creative-runs/${runId}/previews`);
}

/** The PNG 4.6.4 captured, streamed by `api` off the worker's Volume. */
export function previewScreenshotUrl(previewId: string): string {
  return `${API_BASE}/render-previews/${previewId}/screenshot`;
}

export type ConformanceConstraint =
  | "max_chars"
  | "min_px"
  | "ratio"
  | "max_bytes"
  | "format"
  | "min_duration_s"
  | "max_duration_s"
  | "fps"
  | "codec"
  | "audio_codec";
export type ConformanceSource = "lint" | "pillow" | "ffprobe";

export type ConformanceCheck = {
  asset_id: string;
  media_id: string | null;
  constraint: ConformanceConstraint;
  expected: string | number;
  measured: string | number;
  source: ConformanceSource;
  verdict: "pass" | "fail";
};

export type ConformanceUnchecked = {
  asset_id: string;
  media_id: string | null;
  reason: "spec_missing" | "file_missing" | "file_unreadable";
  detail: string;
};

export type ConformanceResponse = {
  ruleset_version: string;
  checks: ConformanceCheck[];
  unchecked: ConformanceUnchecked[];
  failed: number;
};

export type ConformanceFilter = "all" | "fail" | "pass";

export function getConformance(runId: string, verdict: ConformanceFilter): Promise<ConformanceResponse> {
  const query = verdict === "all" ? "" : `?verdict=${verdict}`;
  return apiFetch(`/creative-runs/${runId}/conformance${query}`);
}

// ---------------------------------------------------------------------------
// the package (§12.3)
// ---------------------------------------------------------------------------

export type PackageStatus = "draft" | "blocked" | "ready_to_release" | "released" | "superseded";
export type PackageLintVerdict = "pass" | "pass_with_warnings" | "fail" | "indeterminate" | "unlinted";

export type PackagePins = {
  plan_id: string;
  plan_version: number;
  ruleset_version: string;
  context_hash: string;
  constants_version: string;
  catalogue_hash: string;
};

export type LintResultRef = { verdict: PackageLintVerdict; ruleset_version: string | null; rule_ids: string[] };

export type PackageTextAsset = {
  asset_id: string;
  kind: string;
  surface: string;
  campaign_ref: string;
  ad_group_ref: string | null;
  text: string | null;
  fields: Record<string, unknown>;
  category: string | null;
  claim_ids: string[];
  pin_position: string | null;
  variant: "A" | "B" | null;
  lint: LintResultRef;
  generated_by_ai: boolean;
};

export type PackageRsa = {
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
};

export type PackageMediaRendition = {
  media_id: string;
  path: string;
  surface: string;
  aspect_ratio: string;
  width: number;
  height: number;
  bytes: number;
  media_type: string;
  sha256: string;
  derivation: string;
  lint: LintResultRef;
  duration_ms: number | null;
};

export type PackageMediaAsset = {
  asset_id: string;
  modality: "image" | "video" | "logo";
  campaign_ref: string;
  concept_id: string | null;
  generated_by_ai: boolean;
  renditions: PackageMediaRendition[];
  provenance: { model_id: string | null; provider: string | null; cost_usd: string | null };
};

export type RequiredCount = { asset_type: string; required: number; present: number; met: boolean };

export type MinimumCheck = {
  campaign_type: string;
  required: RequiredCount[];
  met: boolean;
  no_stated_minimum: boolean;
};

export type PackageExtensions = {
  sitelinks: string[];
  callouts: string[];
  snippets: string[];
  promotions: string[];
  prices: string[];
  lead_form: string | null;
};

export type CampaignCreative = {
  campaign_ref: string;
  campaign_type: string;
  text_assets: PackageTextAsset[];
  ads: PackageRsa[];
  extensions: PackageExtensions;
  media: PackageMediaAsset[];
  logos: PackageMediaAsset[];
  launch_minimums: MinimumCheck;
};

export type GateDecision = {
  gate_key: "G7" | "G8" | "G8b";
  approval_id: string;
  node_id: string;
  status: "approved" | "rejected" | "pending" | "expired";
  decided_by: string | null;
  decided_at: string | null;
  note: string;
};

export type PackageDependency = {
  kind: "youtube_upload" | "landing_patch" | "inherited";
  task: string;
  owner: string;
  blocking_for: "launch" | "none";
  campaign_refs: string[];
  source: string;
};

export type ManifestEntry = { path: string; sha256: string; bytes: number; media_type: string };

export type CostSummary = {
  text_usd: string;
  image_usd: string;
  video_usd: string;
  media_estimate_usd: string;
  media_actual_usd: string;
  total_usd: string;
};

export type LintSummary = {
  ruleset_version: string;
  by_verdict: { verdict: PackageLintVerdict; count: number }[];
  by_rule: { rule_id: string; count: number }[];
};

export type CreativePackage = {
  schema_version: "1.0";
  package_id: string;
  project_id: string;
  creative_run_id: string;
  version: number;
  status: PackageStatus;
  pins: PackagePins;
  brief_hash: string;
  campaigns: CampaignCreative[];
  decisions: GateDecision[];
  exceptions: { exception_id: string; kind: string; status: string; subject: string | null }[];
  open_dependencies: PackageDependency[];
  lint_summary: LintSummary;
  manifest: ManifestEntry[];
  cost: CostSummary;
  package_hash: string;
};

export type CritiqueIssue = {
  severity: "blocking" | "warning" | "note";
  section: string;
  finding: string;
  fix: string;
  check: string;
  asset_ids: string[];
};

export type CreativeCritique = {
  package_id: string;
  status: "blocked" | "ready_to_release";
  issues: CritiqueIssue[];
  blocking: number;
  warnings: number;
  notes: number;
  failed_checks: string[];
  checks_run: 13;
};

export type ChecklistItem = { check: string; title: string; passed: boolean; issues: CritiqueIssue[] };

export type ReleaseGate = "G7" | "G8" | "G8b" | "H3";

export type ReleaseStop = {
  gate: ReleaseGate;
  status: string;
  decided_by: string | null;
  decided_by_name: string | null;
  decided_at: string | null;
  detail: string;
};

export type ReleasePreview = {
  releasable: boolean;
  /** What release would mint now — the server's `max(version) + 1`. Null once released. */
  version_to_mint: number | null;
  reason: string | null;
  stops: ReleaseStop[];
};

/** `GET /creative-runs/{id}/package` and `GET /creative-packages/{id}`. */
export type PackageView = {
  package: CreativePackage;
  row_version: number;
  package_hash: string | null;
  released_at: string | null;
  released_by: string | null;
  released_by_name: string | null;
  plan_superseded: boolean;
  ruleset_superseded: boolean;
  created_at: string;
  updated_at: string;
  critique: CreativeCritique | null;
  checklist: ChecklistItem[];
  release: ReleasePreview;
};

export function getRunPackage(runId: string): Promise<PackageView> {
  return apiFetch(`/creative-runs/${runId}/package`);
}

export function getPackage(packageId: string): Promise<PackageView> {
  return apiFetch(`/creative-packages/${packageId}`);
}

/** A released package's manifest file — a signed redirect to the file server. */
export function packageFileUrl(packageId: string, path: string): string {
  return `${API_BASE}/creative-packages/${packageId}/files/${path.split("/").map(encodeURIComponent).join("/")}`;
}

export type ReleaseResult = {
  package_id: string;
  creative_run_id: string;
  version: number;
  status: string;
  package_hash: string;
  released_at: string;
  released_by: string | null;
  superseded: string[];
  files: number;
};

/** `POST /creative-packages/{id}/release` — the version the approver typed. */
export function releasePackage(packageId: string, confirmVersion: number): Promise<ReleaseResult> {
  return apiFetch(`/creative-packages/${packageId}/release`, {
    method: "POST",
    body: JSON.stringify({ confirm_version: confirmVersion }),
  });
}

/** The canonical URL of a package (§15.3): the one a released version is read at. */
export function packageHref(projectId: string, packageId: string): string {
  return `/projects/${projectId}/creative/packages/${packageId}`;
}

export function compareHref(projectId: string, before: string, after: string): string {
  return `/projects/${projectId}/creative/compare?a=${before}&b=${after}`;
}

// ---------------------------------------------------------------------------
// the diff (`package_diff`)
// ---------------------------------------------------------------------------

export type DiffAsset = {
  asset_id: string;
  slot: string;
  kind: string;
  text: string | null;
  fields: Record<string, unknown>;
  renditions: PackageMediaRendition[];
};

export type DiffChange = { slot: string; kind: string; from: DiffAsset; to: DiffAsset };

export type PackageDiff = {
  package_id: string;
  version: number;
  against_id: string;
  against_version: number;
  pins: { field: string; before: string; after: string }[];
  added: DiffAsset[];
  removed: DiffAsset[];
  changed: DiffChange[];
};

/** What `after` added, removed and changed against `before`. */
export function getPackageDiff(before: string, after: string): Promise<PackageDiff> {
  return apiFetch(`/creative-packages/${after}/diff?against=${before}`);
}
