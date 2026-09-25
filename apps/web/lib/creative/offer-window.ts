/**
 * How an offer-bound asset reads on the Extras screen (Stage 04 PRD §15.4 F):
 * `20% off · ends 12 Oct 2026 · 18 days`.
 *
 * Every figure is a value the `OfferBinding` already rendered from the
 * `OfferRecord` in code (law 35) — printed as it is, never recomputed or
 * rounded here. The window is the record's own: its end (`ends_at`, else
 * `effective_to`) as a calendar date in the reader's time zone, and how many
 * calendar days away that is today. That count is display, like a relative
 * time; whether an offer is still live at release is the server's 409.
 */
import type { OfferBinding, OfferSource } from "@/lib/api/creative-runs";

const DATE = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short", year: "numeric" });
const DAY_MS = 86_400_000;

/** "23% off", "USD 10.00 off", "USD 99.00" — the binding's own strings. */
export function boundFigure(binding: OfferBinding): string | null {
  const values = binding.resolved;
  const currency = values["currency"];
  if (values["percent_off"]) return `${values["percent_off"]}% off`;
  if (values["money_off"]) return `${currency ? `${currency} ` : ""}${values["money_off"]} off`;
  if (values["price"]) return `${currency ? `${currency} ` : ""}${values["price"]}`;
  return null;
}

/** The record's end: `ends_at`, else the end of its effective window. */
export function offerEnd(offer: OfferSource | null, binding: OfferBinding): string | null {
  return offer?.ends_at ?? offer?.effective_to ?? binding.resolved["end"] ?? null;
}

export function formatDate(iso: string): string {
  return DATE.format(new Date(iso));
}

function calendarDay(date: Date): number {
  return Date.UTC(date.getFullYear(), date.getMonth(), date.getDate());
}

/** Calendar days from `now` to `iso` in the reader's zone: 0 today, negative once past. */
export function daysUntil(iso: string, now: Date): number {
  return Math.round((calendarDay(new Date(iso)) - calendarDay(now)) / DAY_MS);
}

/** `18 days`, `1 day`, `ends today`, `ended 3 days ago`. */
export function remaining(iso: string, now: Date): string {
  const days = daysUntil(iso, now);
  if (days > 1) return `${days} days`;
  if (days === 1) return "1 day";
  if (days === 0) return "ends today";
  return days === -1 ? "ended yesterday" : `ended ${-days} days ago`;
}
