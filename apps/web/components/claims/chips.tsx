import { cn } from "@/lib/utils";
import type { ClaimSummary } from "@/lib/api/claims";
import { expiryState } from "@/lib/api/claims";

/**
 * Stage 03's status vocabulary, in the palette Stages 01 and 02 established.
 *
 * Every chip pairs its colour with a word. A register read by somebody who
 * cannot separate amber from green still says which claims are waiting
 * (WCAG 1.4.1), and it is the word that survives a screenshot in a compliance
 * pack a year from now.
 */
const CLAIM_STATES: Record<string, { label: string; dot: string }> = {
  unsupported: { label: "Unsupported", dot: "bg-status-failed" },
  pending_signoff: { label: "Awaiting signature", dot: "bg-status-gate" },
  approved: { label: "Approved", dot: "bg-status-success" },
  rejected: { label: "Rejected", dot: "bg-status-skipped" },
  expired: { label: "Expired", dot: "bg-status-skipped" },
};

export function ClaimStatusChip({ status, className }: { status: string; className?: string }) {
  const state = CLAIM_STATES[status] ?? { label: status, dot: "bg-status-skipped" };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium text-fg",
        className,
      )}
    >
      <span aria-hidden className={cn("size-1.5 rounded-full", state.dot)} />
      {state.label}
    </span>
  );
}

const RISK: Record<string, string> = {
  low: "text-fg-muted",
  medium: "text-fg",
  high: "text-fg font-medium",
};

export function RiskChip({ tier }: { tier: string }) {
  return (
    <span className={cn("text-xs capitalize", RISK[tier] ?? "text-fg-muted")}>
      {tier} risk
    </span>
  );
}

/**
 * How long a claim has left.
 *
 * A relative phrase with the absolute date on the element itself — not only in
 * a `title`, which a keyboard or touch user never reaches. An expired claim
 * reads `Expired`, not `-24 days`: the countdown stops being the useful frame
 * the moment it goes negative.
 */
export function ExpiryChip({ claim, now }: { claim: ClaimSummary; now?: number }) {
  const state = expiryState(claim, now);
  if (!state) return <span className="text-xs text-fg-subtle">No expiry</span>;
  const absolute = new Date(claim.expires_at as string).toISOString().slice(0, 10);
  if (state.expired) {
    return (
      <span className="text-xs text-fg-muted">
        Expired <span className="text-fg-subtle">{absolute}</span>
      </span>
    );
  }
  return (
    <span className={cn("text-xs", state.soon ? "text-fg" : "text-fg-muted")}>
      {state.soon ? (
        <span aria-hidden className="mr-1.5 inline-block size-1.5 rounded-full bg-status-gate align-middle" />
      ) : null}
      in {state.days} {state.days === 1 ? "day" : "days"}{" "}
      <span className="text-fg-subtle">{absolute}</span>
    </span>
  );
}

const TYPE_LABEL: Record<string, string> = {
  superlative: "Superlative",
  comparative: "Comparative",
  quantified: "Quantified",
  certification: "Certification",
  guarantee: "Guarantee",
  pricing: "Pricing",
  other: "Other",
};

export function claimTypeLabel(type: string): string {
  return TYPE_LABEL[type] ?? type;
}
