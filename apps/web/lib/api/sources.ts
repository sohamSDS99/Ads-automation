/**
 * CSV import (PRD §9.5, §13.4 step 2).
 *
 * Two calls, because mapping columns is a conversation: the file is posted once
 * to see what is in it, and again with a confirmed mapping. The preview stores
 * nothing, so uploading the wrong export costs nothing.
 *
 * Both are multipart, so neither can go through `apiFetch`'s JSON body path —
 * setting `Content-Type` by hand would strip the boundary the browser
 * generates. They use `apiFetch` with a `FormData` body, which leaves the
 * header unset on purpose.
 */
import { apiFetch } from "@/lib/api";

export type CsvColumn = {
  header: string;
  values: string[];
  suggested_field: string | null;
};

export type CsvPreview = {
  filename: string;
  row_count: number;
  columns: CsvColumn[];
  canonical_fields: string[];
  required_fields: string[];
};

export type CsvRowError = {
  row: number;
  column: string | null;
  value: string | null;
  problem: string;
};

export type CsvIngest = {
  project_id: string;
  outcome: "won" | "lost";
  rows_accepted: number;
  rows_skipped: number;
  evidence_written: number;
  duplicates: number;
  errors: CsvRowError[];
  evidence_ids: string[];
};

export function previewCsv(projectId: string, file: File): Promise<CsvPreview> {
  const form = new FormData();
  form.append("file", file);
  return apiFetch(`/projects/${projectId}/sources/csv/preview`, { method: "POST", body: form });
}

export function uploadCsv(
  projectId: string,
  file: File,
  outcome: "won" | "lost",
  mapping: Record<string, string>,
): Promise<CsvIngest> {
  const form = new FormData();
  form.append("file", file);
  form.append("outcome", outcome);
  form.append("mapping", JSON.stringify(mapping));
  return apiFetch(`/projects/${projectId}/sources/csv`, { method: "POST", body: form });
}

/** A canonical field name as a label: `employee_count` → `Employee count`. */
export function fieldLabel(field: string): string {
  const spaced = field.replace(/_/g, " ");
  return spaced.charAt(0).toUpperCase() + spaced.slice(1);
}
