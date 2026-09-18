/**
 * The report, and the five ways to take it away.
 *
 * The types below mirror `agent.export.contract` (PRD §11). Every record is
 * optional-heavy on purpose: the contract itself is, because a run whose
 * Transparency pull degraded still produces a report, and a viewer that assumes
 * a field exists renders a blank page instead of a partial one.
 */
import { API_BASE, apiFetch } from "@/lib/api";

export type Confidence = "high" | "medium" | "low";
export type LaunchReadiness = "go" | "go_with_fixes" | "no_go";
/** Both mirror `agent.export.contract` exactly — a near-miss here is a silent
 *  rendering bug: an unknown value falls through every lookup to a grey dot. */
export type MappingVerdict = "good_fit" | "weak_fit" | "gap";
export type Intent =
  | "transactional"
  | "commercial_investigation"
  | "informational"
  | "navigational"
  | "irrelevant";

export const INTENT_LABEL: Record<Intent, string> = {
  transactional: "Ready to buy",
  commercial_investigation: "Comparing options",
  informational: "Learning",
  navigational: "Looking for someone",
  irrelevant: "Not relevant",
};

export const VERDICT_LABEL: Record<MappingVerdict, string> = {
  good_fit: "Good fit",
  weak_fit: "Weak fit",
  gap: "No page yet",
};

/** Every claim carries the evidence it stands on, or it fails validation. */
export type Claim = {
  statement: string;
  evidence_ids: string[];
  confidence: Confidence;
};

export type Product = {
  name: string;
  price_model?: string | null;
  acv?: number | null;
  gross_margin_pct?: number | null;
  delivery_cost_notes?: string | null;
  evidence_ids?: string[];
};

export type IcpSegment = {
  label: string;
  /**
   * A sentence in some runs, a map of traits in others. The contract declares
   * the union deliberately: 1.1.2 asks the model for prose, a CRM rollup
   * produces a map, and both are legitimate. Render whichever arrived.
   */
  firmographics?: string | Record<string, unknown> | null;
  triggers?: string[];
  jobs_to_be_done?: string[];
  share_of_revenue_pct?: number | null;
  evidence_ids?: string[];
};

export type IcpExclusion = {
  persona: string;
  disqualifier: string;
  observable_signal?: string | null;
  suggested_negative_terms?: string[];
  evidence_ids?: string[];
};

export type MarketCoverage = {
  country: string;
  language?: string | null;
  currency?: string | null;
  demand_months?: number[];
  dead_months?: number[];
  evidence_ids?: string[];
};

export type ComplianceGuardrails = {
  prohibited_claims?: string[];
  required_disclaimers?: string[];
  regulated_terms?: { term: string; rule: string }[];
  confidence?: Confidence | null;
};

export type BusinessContext = {
  products?: Product[];
  ltv_estimate?: number | null;
  target_cac?: number | null;
  payback_months?: number | null;
  segments?: IcpSegment[];
  exclusions?: IcpExclusion[];
  markets?: MarketCoverage[];
  compliance?: ComplianceGuardrails | null;
};

export type PerformanceFinding = {
  campaign: string;
  metric_delta?: string | null;
  period?: string | null;
  evidence_ids?: string[];
};

export type ProfitableTerm = {
  term: string;
  cost?: number | null;
  conv?: number | null;
  cpa?: number | null;
  roas?: number | null;
  evidence_ids?: string[];
};

export type WastefulTerm = {
  term: string;
  cost?: number | null;
  conv?: number;
  recommended_action?: string | null;
  evidence_ids?: string[];
};

export type FailedExperiment = {
  what: string;
  when?: string | null;
  outcome?: string | null;
  do_not_repeat_reason?: string | null;
  evidence_ids?: string[];
};

export type AccountLearnings = {
  winners?: PerformanceFinding[];
  losers?: PerformanceFinding[];
  structural_findings?: string[];
  profitable_terms?: ProfitableTerm[];
  wasteful_terms?: WastefulTerm[];
  tried_and_failed?: FailedExperiment[];
};

export type Competitor = {
  domain: string;
  name?: string | null;
  overlap_score?: number | null;
  overlap_basis?: string[];
  /** 1.3.1's judgement: direct | adjacent | aggregator | irrelevant. */
  threat?: string | null;
  positioning?: string | null;
  evidence_ids?: string[];
};

export const THREAT_LABEL: Record<string, string> = {
  direct: "Direct",
  adjacent: "Adjacent",
  aggregator: "Aggregator",
  irrelevant: "Not a rival",
};

export type CompetitorAd = {
  advertiser: string;
  headline?: string | null;
  description?: string | null;
  offer?: string | null;
  angle?: string | null;
  proof_type?: string | null;
  cta?: string | null;
  landing_url?: string | null;
  first_seen?: string | null;
  last_seen?: string | null;
  screenshot_path?: string | null;
  evidence_ids?: string[];
};

export type MessageCluster = { theme: string; frequency?: number | null; advertisers?: string[] };

export type SpendEstimate = {
  competitor: string;
  est_monthly_spend_range: string;
  method: string;
  confidence?: Confidence | null;
  peak_months?: number[];
  evidence_ids?: string[];
};

export type Whitespace = {
  claim: string;
  why_unsaid?: string | null;
  our_proof?: string | null;
  risk?: string | null;
  evidence_ids?: string[];
};

export type CompetitiveLandscape = {
  competitors?: Competitor[];
  ads?: CompetitorAd[];
  message_clusters?: MessageCluster[];
  spend_estimates?: SpendEstimate[];
  whitespace?: Whitespace[];
  recommended_claim?: string | null;
  substantiation_required?: string[];
};

export type NegativeKeyword = {
  term: string;
  match_type?: "broad" | "phrase" | "exact";
  reason?: string | null;
  source?: string | null;
  evidence_ids?: string[];
};

export type KeywordPageMapping = {
  term_cluster: string;
  best_url?: string | null;
  relevance_score?: number | null;
  verdict: MappingVerdict;
  evidence_ids?: string[];
};

export type DemandMap = {
  total_keywords?: number;
  negatives?: NegativeKeyword[];
  mapping?: KeywordPageMapping[];
  content_gaps?: { cluster: string; required_page_type: string }[];
};

export type PricedKeyword = {
  term: string;
  market?: string | null;
  intent?: Intent | null;
  funnel_stage?: string | null;
  volume?: number | null;
  cpc_low?: number | null;
  cpc_high?: number | null;
  competition?: number | null;
  /** Twelve monthly indices, or nothing. Never some other length. */
  seasonality_index?: number[];
  trend_yoy?: number | null;
  best_url?: string | null;
  verdict?: MappingVerdict | null;
  match_type?: "broad" | "phrase" | "exact";
  evidence_ids?: string[];
};

export type PageAudit = {
  url: string;
  lcp_ms?: number | null;
  cls?: number | null;
  mobile_ok?: boolean | null;
  form_fields_count?: number | null;
  trust_signals?: string[];
  issues?: string[];
  severity?: "critical" | "major" | "minor" | "none" | null;
  evidence_ids?: string[];
};

export type ConversionAction = {
  name: string;
  status?: string | null;
  last_conversion_at?: string | null;
  staleness_days?: number | null;
  evidence_ids?: string[];
};

export type SyntheticCheck = {
  fired_at?: string | null;
  observed_in_ads_api?: boolean | null;
  latency_min?: number | null;
  verdict?: "pass" | "fail" | "inconclusive" | null;
};

export type AudienceList = {
  name: string;
  size?: number | null;
  consent_basis?: string | null;
  markets_allowed?: string[];
  usable?: boolean;
  blocker?: string | null;
  evidence_ids?: string[];
};

export type Scenario = {
  budget_usd_month: number;
  est_clicks?: number | null;
  est_conv?: number | null;
  est_cpa?: number | null;
  est_revenue?: number | null;
  assumptions?: string[];
  confidence_interval?: string | null;
  evidence_ids?: string[];
};

export type Readiness = {
  pages?: PageAudit[];
  conversion_actions?: ConversionAction[];
  synthetic_check?: SyntheticCheck | null;
  alerts?: string[];
  lists?: AudienceList[];
  scenarios?: Scenario[];
};

export type ResearchReport = {
  schema_version: string;
  project_id: string;
  run_id: string;
  generated_at: string;
  executive_summary: string;
  launch_readiness: LaunchReadiness;
  launch_blockers?: Claim[];
  business_context?: BusinessContext;
  account_learnings?: AccountLearnings;
  competitive_landscape?: CompetitiveLandscape;
  demand_map?: DemandMap;
  readiness?: Readiness;
  priced_keyword_list?: PricedKeyword[];
  recommended_next_actions?: Claim[];
  open_questions?: string[];
  /** Connectors that partially failed. Named in the report, not hidden. */
  degraded_sources?: string[];
  cost_usd?: number;
};

export type ExportFormat = "pdf" | "docx" | "md" | "json" | "csv";
export type ExportStatus = "queued" | "running" | "ready" | "failed";

export type ExportJob = {
  id: string;
  report_id: string;
  run_id: string;
  format: ExportFormat;
  status: ExportStatus;
  bytes: number | null;
  filename: string | null;
  error: string | null;
  created_at: string;
  ready_at: string | null;
};

export type ReportResponse = {
  id: string;
  run_id: string;
  project_id: string;
  schema_version: string;
  created_at: string;
  payload: ResearchReport;
  markdown: string;
  exports: ExportJob[];
};

export const FORMAT_LABEL: Record<ExportFormat, string> = {
  pdf: "PDF",
  docx: "Word",
  md: "Markdown",
  json: "JSON",
  csv: "Keywords CSV",
};

export const READINESS_LABEL: Record<LaunchReadiness, string> = {
  go: "Go",
  go_with_fixes: "Go with fixes",
  no_go: "No-go",
};

export function getReport(runId: string): Promise<ReportResponse> {
  return apiFetch(`/reports/${runId}`);
}

export function requestExport(
  runId: string,
  format: ExportFormat,
): Promise<{ job_id: string; export: ExportJob }> {
  return apiFetch(`/reports/${runId}/export?format=${format}`, { method: "POST" });
}

export function getExport(jobId: string): Promise<ExportJob> {
  return apiFetch(`/exports/${jobId}`);
}

/**
 * Where a finished export downloads from.
 *
 * A plain link, not a fetch: the response carries `Content-Disposition`, and
 * letting the browser handle it is what makes the file land in Downloads with
 * the right name instead of in memory.
 */
export function downloadUrl(jobId: string): string {
  return `${API_BASE}/exports/${jobId}/download`;
}

/** Every evidence id cited anywhere in the report, deduped, in reading order. */
export function citedEvidenceIds(report: ResearchReport): string[] {
  const found = new Set<string>();
  const walk = (node: unknown): void => {
    if (Array.isArray(node)) {
      for (const item of node) walk(item);
      return;
    }
    if (node === null || typeof node !== "object") return;
    for (const [key, value] of Object.entries(node)) {
      if (key === "evidence_ids" && Array.isArray(value)) {
        for (const id of value) if (typeof id === "string") found.add(id);
      } else {
        walk(value);
      }
    }
  };
  walk(report);
  return [...found];
}
