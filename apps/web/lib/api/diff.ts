/** `GET /runs/{id}/diff` — what changed since another run (PRD §13.4 C, §14). */
import { apiFetch } from "@/lib/api";

export type FieldChange = {
  field: string;
  before: unknown;
  after: unknown;
};

export type ItemDiff = {
  key: string;
  label: string;
  status: "added" | "removed" | "changed";
  changes: FieldChange[];
};

export type SectionDiff = {
  path: string;
  title: string;
  added: number;
  removed: number;
  changed: number;
  total: number;
  items: ItemDiff[];
  /**
   * More records changed than `items` lists. The counts are still exact — a cap
   * that did not say so would read as "and nothing else changed".
   */
  truncated: boolean;
};

export type RunDiff = {
  run_id: string;
  against_run_id: string;
  generated_at: string;
  against_generated_at: string;
  against_is_parent: boolean;
  scalars: FieldChange[];
  sections: SectionDiff[];
  unchanged: boolean;
};

export function getRunDiff(runId: string, against?: string): Promise<RunDiff> {
  const query = against ? `?against=${encodeURIComponent(against)}` : "";
  return apiFetch(`/runs/${runId}/diff${query}`);
}
