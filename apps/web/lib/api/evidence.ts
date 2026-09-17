/**
 * Evidence: browse, search, resolve a citation, and fetch a creative.
 *
 * `ids` is the Report Viewer's entry point — one request for every citation in
 * a report, rather than one per superscript.
 */
import { API_BASE, apiFetch } from "@/lib/api";

export type EvidenceSource =
  | "google_ads"
  | "dataforseo"
  | "transparency"
  | "serp"
  | "web"
  | "csv"
  | "upload"
  | "derived";

export type EvidenceItem = {
  id: string;
  project_id: string;
  run_id: string | null;
  source: EvidenceSource;
  kind: string;
  source_url: string | null;
  content_text: string | null;
  payload: Record<string, unknown>;
  fetched_at: string;
  has_screenshot: boolean;
  score: number;
  matched_by: "vector" | "text" | "both" | "filter";
  text_rank: number | null;
  vector_rank: number | null;
};

export type EvidencePage = {
  items: EvidenceItem[];
  next_cursor: string | null;
  /** True when `q` was supplied, which is what makes `score` mean anything. */
  ranked: boolean;
};

export type EvidenceQuery = {
  project_id?: string;
  source?: EvidenceSource | "";
  kind?: string;
  run_id?: string;
  ids?: string[];
  /** ISO instants. The explorer's date pickers resolve to a whole day. */
  fetched_from?: string;
  fetched_to?: string;
  q?: string;
  cursor?: string | null;
  limit?: number;
};

/** How many ids `GET /evidence` will accept at once — its `PAGE_SIZE_MAX`. */
export const MAX_IDS = 200;

export const SOURCE_LABEL: Record<EvidenceSource, string> = {
  google_ads: "Google Ads",
  dataforseo: "Keyword data",
  transparency: "Transparency Center",
  serp: "Search results",
  web: "Site crawl",
  csv: "CRM upload",
  upload: "Business context",
  derived: "Computed",
};

export function listEvidence(query: EvidenceQuery = {}): Promise<EvidencePage> {
  const params = new URLSearchParams();
  if (query.project_id) params.set("project_id", query.project_id);
  if (query.source) params.set("source", query.source);
  if (query.kind) params.set("kind", query.kind);
  if (query.run_id) params.set("run_id", query.run_id);
  if (query.fetched_from) params.set("fetched_from", query.fetched_from);
  if (query.fetched_to) params.set("fetched_to", query.fetched_to);
  if (query.q) params.set("q", query.q);
  if (query.cursor) params.set("cursor", query.cursor);
  if (query.limit) params.set("limit", String(query.limit));
  for (const id of query.ids ?? []) params.append("ids", id);
  const suffix = params.size > 0 ? `?${params}` : "";
  return apiFetch(`/evidence${suffix}`);
}

/**
 * The creative capture stored for a row.
 *
 * A URL rather than a fetch: it goes in `src`, and the browser's own image
 * cache is better at this than anything a query client would do with a blob.
 */
export function screenshotUrl(evidenceId: string): string {
  return `${API_BASE}/evidence/${evidenceId}/screenshot`;
}

/** The one line that stands for a row in a table cell. */
export function evidenceSummary(item: EvidenceItem): string {
  const text = item.content_text?.replace(/\s+/g, " ").trim();
  if (text) {
    // `render_content_text` puts the kind on the first line, and the whitespace
    // collapse above turns `search_term_pnl` into `search term pnl` — so the
    // prefix has to be matched in the shape it actually arrives in.
    const spelled = item.kind.replace(/_/g, " ");
    for (const prefix of [item.kind, spelled]) {
      if (text.toLowerCase().startsWith(prefix.toLowerCase())) {
        return text.slice(prefix.length).trim() || text;
      }
    }
    return text;
  }
  const values = Object.values(item.payload).filter(
    (value) => typeof value === "string" || typeof value === "number",
  );
  return values.slice(0, 4).join(" · ") || item.kind;
}
