import { cn } from "@/lib/utils";

/** §15.2 rule 6: amber from 90% of the limit. */
const NEAR = 0.9;

/**
 * `27/30` — how long a piece of copy is against its limit (Stage 04 PRD §15.2
 * rule 6): zinc under 90%, amber from 90%, rose over the limit.
 *
 * **Display, never a verdict.** `count` is `charCount()` — the number the
 * server's linter will measure (held to it by the parity test) — and `limit`
 * is the pinned spec sheet's. Whether the text passes is the `LintChip`
 * beside it, which reads the linter. Over the limit the counter also says so
 * in words, so the state is never carried by colour alone (rule 14).
 */
export function CharCounter({
  count,
  limit,
  className,
}: {
  count: number;
  /** The spec sheet's `max_chars` at the run's pin; null when it names none. */
  limit: number | null;
  className?: string;
}) {
  if (limit === null) {
    return (
      <span className={cn("text-xs tabular-nums text-fg-muted", className)} aria-label={`${count} characters`}>
        {count}
      </span>
    );
  }
  const over = count - limit;
  const tone = over > 0 ? "over" : count >= NEAR * limit ? "near" : "under";
  return (
    <span
      data-tone={tone}
      aria-label={`${count} of ${limit} characters${over > 0 ? `, ${over} over the limit` : ""}`}
      className={cn(
        "whitespace-nowrap text-xs tabular-nums",
        tone === "under" && "text-fg-muted",
        tone === "near" && "font-medium text-status-gate-ink",
        tone === "over" && "font-medium text-status-failed-ink",
        className,
      )}
    >
      {count}/{limit}
      {over > 0 ? <span> · {over} over</span> : null}
    </span>
  );
}
