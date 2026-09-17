/**
 * Recurring runs (PRD §13.4 E, §14).
 *
 * The preview is a server call, not a client-side cron parser. Two parsers
 * would be two opinions about when `0 2 * * *` fires across a summer-time
 * change, and the one that matters is the scheduler's — so the editor asks the
 * scheduler.
 */
import { apiFetch } from "@/lib/api";

export type Schedule = {
  id: string;
  project_id: string;
  project_name: string;
  cron: string;
  timezone: string;
  enabled: boolean;
  /** One sentence, written by the same parser the poller fires on. */
  description: string;
  /** The next few firings, in UTC. */
  upcoming: string[];
  next_at: string | null;
  last_run_id: string | null;
  last_run_at: string | null;
  last_run_status: string | null;
  created_by: string;
  created_by_name: string;
};

export type SchedulePreview = {
  cron: string;
  timezone: string;
  description: string;
  upcoming: string[];
};

export function listSchedules(projectId?: string): Promise<{ items: Schedule[] }> {
  const query = projectId ? `?project_id=${encodeURIComponent(projectId)}` : "";
  return apiFetch(`/schedules${query}`);
}

export function previewSchedule(body: { cron: string; timezone: string }): Promise<SchedulePreview> {
  return apiFetch("/schedules/preview", { method: "POST", body: JSON.stringify(body) });
}

export function createSchedule(body: {
  project_id: string;
  cron: string;
  timezone: string;
  enabled: boolean;
}): Promise<Schedule> {
  return apiFetch("/schedules", { method: "POST", body: JSON.stringify(body) });
}

export function updateSchedule(
  id: string,
  body: { cron?: string; timezone?: string; enabled?: boolean },
): Promise<Schedule> {
  return apiFetch(`/schedules/${id}`, { method: "PATCH", body: JSON.stringify(body) });
}

export function deleteSchedule(id: string): Promise<void> {
  return apiFetch(`/schedules/${id}`, { method: "DELETE" });
}

/** The viewer's own zone, so the editor opens on the one they think in. */
export function localTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/**
 * Timezones offered in the picker.
 *
 * `Intl.supportedValuesOf` is the browser's own list, drawn from the same IANA
 * database the server validates against. The fallback is for the handful of
 * engines that do not implement it.
 */
export function timezoneOptions(): string[] {
  const supported =
    typeof Intl.supportedValuesOf === "function" ? Intl.supportedValuesOf("timeZone") : [];
  const fallback = ["Europe/Copenhagen", "Europe/London", "America/New_York", "Asia/Kolkata"];
  // `Intl.supportedValuesOf` returns canonical zone names and omits "UTC",
  // which is every `Schedule` row's default. A `<select>` whose value is not
  // among its options silently displays the first one instead — the editor
  // opened on "Africa/Abidjan" while its state still said "UTC", so the zone
  // shown and the zone about to be saved disagreed with nobody told.
  const zones = new Set<string>(["UTC", localTimezone()]);
  for (const zone of supported.length ? supported : fallback) zones.add(zone);
  return [...zones].sort((a, b) => (a === "UTC" ? -1 : b === "UTC" ? 1 : a.localeCompare(b)));
}

/** A few starting points, so nobody has to remember field order to get going. */
export const CRON_PRESETS: ReadonlyArray<{ label: string; cron: string }> = [
  { label: "Every weekday, 07:00", cron: "0 7 * * 1-5" },
  { label: "Every Monday, 07:00", cron: "0 7 * * 1" },
  { label: "1st of the month, 06:00", cron: "0 6 1 * *" },
  { label: "Every night, 02:00", cron: "0 2 * * *" },
];
