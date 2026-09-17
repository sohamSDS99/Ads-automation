/**
 * Business-context documents (PRD §14, wizard step 1).
 *
 * The file the brand already has — a pricing sheet, a positioning one-pager —
 * uploaded once and read by the research nodes as citable evidence.
 *
 * Multipart, so the body is a `FormData` and `Content-Type` is deliberately
 * left unset: naming it by hand strips the boundary the browser generated.
 */
import { apiFetch } from "@/lib/api";

export type ProjectDocument = {
  id: string;
  project_id: string;
  filename: string;
  media_type: string;
  bytes: number;
  char_count: number;
  passage_count: number;
  /** What `unit_count` counts: "page", "row", "paragraph". */
  unit: string;
  unit_count: number;
  /** What the extractor could not read. Empty is the normal case. */
  warnings: string[];
  preview: string;
  created_at: string;
  uploaded_by: string | null;
  uploaded_by_name: string | null;
};

export type DocumentList = {
  documents: ProjectDocument[];
  total_chars: number;
  max_documents: number;
  max_bytes: number;
  /** `[".pdf", ".docx", …]` — the server's list, not the browser's guess. */
  accepted_extensions: string[];
};

/** One member of an archive that was not stored, and why. */
export type SkippedEntry = {
  filename: string;
  reason: string;
};

export type DocumentUpload = {
  /** Everything this upload stored. One entry for a plain file, many for a zip. */
  documents: ProjectDocument[];
  passages_written: number;
  duplicates: number;
  /** Archive members that were left out. Named, never silently dropped. */
  skipped: SkippedEntry[];
};

export function listDocuments(projectId: string): Promise<DocumentList> {
  return apiFetch(`/projects/${projectId}/documents`);
}

export function uploadDocument(projectId: string, file: File): Promise<DocumentUpload> {
  const form = new FormData();
  form.append("file", file);
  return apiFetch(`/projects/${projectId}/documents`, { method: "POST", body: form });
}

export function deleteDocument(projectId: string, documentId: string): Promise<void> {
  return apiFetch(`/projects/${projectId}/documents/${documentId}`, { method: "DELETE" });
}

/** `1.4 MB`, `812 KB` — sizes as a person reads them, not as bytes. */
export function fileSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/**
 * "8 pages · 14 passages" — what was read, in the unit the format counts in.
 *
 * Passages are named because they are the unit a node cites: a document with
 * text but no passages reached the project and will never be quoted in it.
 */
export function documentScale(document: ProjectDocument): string {
  const parts: string[] = [];
  if (document.unit && document.unit_count) {
    parts.push(`${document.unit_count.toLocaleString()} ${document.unit}${document.unit_count === 1 ? "" : "s"}`);
  }
  parts.push(`${document.char_count.toLocaleString()} characters`);
  parts.push(`${document.passage_count} passage${document.passage_count === 1 ? "" : "s"}`);
  return parts.join(" · ");
}
