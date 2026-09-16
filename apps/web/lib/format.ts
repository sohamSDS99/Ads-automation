/**
 * How this app writes times, durations and money.
 *
 * One module so a duration reads the same on the project list as it does in the
 * run history. Everything renders from the browser's locale except currency,
 * which is USD because that is what OpenRouter bills in.
 */

const RELATIVE = new Intl.RelativeTimeFormat(undefined, { numeric: "auto" });

const UNITS: [Intl.RelativeTimeFormatUnit, number][] = [
  ["year", 31_536_000],
  ["month", 2_592_000],
  ["week", 604_800],
  ["day", 86_400],
  ["hour", 3600],
  ["minute", 60],
];

/** "3 hours ago" — or "just now", because "0 minutes ago" reads as a bug. */
export function relativeTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const seconds = (Date.parse(iso) - Date.now()) / 1000;
  const magnitude = Math.abs(seconds);
  if (magnitude < 45) return "just now";
  for (const [unit, size] of UNITS) {
    if (magnitude >= size) return RELATIVE.format(Math.round(seconds / size), unit);
  }
  return RELATIVE.format(Math.round(seconds), "second");
}

/** An absolute timestamp, for the title attribute behind a relative one. */
export function absoluteTime(iso: string | null | undefined): string {
  if (!iso) return "";
  return new Date(iso).toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" });
}

export function shortDate(iso: string | null | undefined): string {
  if (!iso) return "—";
  return new Date(iso).toLocaleDateString(undefined, { dateStyle: "medium" });
}

/** How long a run took, or how long it has been going. */
export function duration(startIso: string | null, endIso: string | null): string {
  if (!startIso) return "—";
  const end = endIso ? Date.parse(endIso) : Date.now();
  const seconds = Math.max(0, Math.round((end - Date.parse(startIso)) / 1000));
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ${seconds % 60}s`;
  return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
}

export function usd(value: string | number | null | undefined, fractionDigits = 2): string {
  if (value === null || value === undefined) return "—";
  const amount = typeof value === "number" ? value : Number(value);
  if (Number.isNaN(amount)) return "—";
  return `$${amount.toFixed(fractionDigits)}`;
}

export function compactNumber(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  return new Intl.NumberFormat(undefined, { notation: "compact" }).format(value);
}
