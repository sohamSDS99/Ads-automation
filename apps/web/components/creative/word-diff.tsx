import { Fragment } from "react";

import { wordDiff, type DiffToken } from "@/lib/creative/word-diff";
import { cn } from "@/lib/utils";

/**
 * `WordDiff` — the ad headline against the page's H1, word by word, with the
 * score 4.5.1 gave the pair: `match 0.41 < 0.55` (Stage 04 PRD §15.4 J).
 *
 * The score, the threshold and the verdict are the node's
 * (`match.token_trigram_v1`); the diff only shows which words the two lines
 * share, so a reader can see *why* the number is low. Words only one line
 * has are underlined and listed for screen readers, so the difference is
 * never carried by colour.
 */
export function WordDiff({
  headline,
  h1,
  score,
  threshold,
  verdict,
  caption,
}: {
  headline: string;
  h1: string | null;
  score: number;
  threshold: number;
  verdict: "pass" | "fail";
  /** Whose pair this is: "sds software on mobile — the weakest of 2 ad groups". */
  caption: string;
}) {
  const diff = wordDiff(headline, h1 ?? "");
  const only = (tokens: DiffToken[]) => tokens.filter((t) => t.kind === "only").map((t) => t.text);
  const digits = numberWidth(score, threshold, verdict);
  return (
    <div data-testid="word-diff" className="flex min-w-0 flex-col gap-2">
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <p className="text-xs text-fg-muted">{caption}</p>
        <p
          data-testid="match-score"
          className={cn(
            "font-mono text-sm tabular-nums",
            verdict === "fail" ? "text-status-failed-ink" : "text-fg",
          )}
        >
          match {score.toFixed(digits)} {verdict === "pass" ? "≥" : "<"} {threshold.toFixed(digits)}
        </p>
      </div>
      <dl className="grid grid-cols-1 gap-1.5 text-sm sm:grid-cols-6">
        <dt className="text-xs text-fg-muted sm:pt-0.5">Ad headline</dt>
        <dd className="sm:col-span-5">
          <Tokens tokens={diff.a} />
        </dd>
        <dt className="text-xs text-fg-muted sm:pt-0.5">Page H1</dt>
        <dd className="sm:col-span-5">
          {h1 ? <Tokens tokens={diff.b} /> : <span className="text-fg-muted">The page has no H1.</span>}
        </dd>
      </dl>
      <p className="text-xs text-fg-muted">Underlined words appear in only one of the two lines.</p>
      <p className="sr-only">
        Only in the ad headline: {only(diff.a).join(", ") || "none"}. Only in the page H1: {only(diff.b).join(", ") || "none"}.
      </p>
    </div>
  );
}

function Tokens({ tokens }: { tokens: DiffToken[] }) {
  return (
    <span>
      {tokens.map((token, index) => (
        <Fragment key={`${index}-${token.text}`}>
          {index > 0 ? " " : null}
          <span
            data-kind={token.kind}
            className={cn(
              token.kind === "only" &&
                "font-medium text-fg underline decoration-status-failed decoration-2 underline-offset-4",
              token.kind !== "only" && "text-fg-muted",
            )}
          >
            {token.text}
          </span>
        </Fragment>
      ))}
    </span>
  );
}

/** Two decimals, or three when two would print a failing score equal to its threshold. */
function numberWidth(score: number, threshold: number, verdict: "pass" | "fail"): number {
  return verdict === "fail" && score.toFixed(2) === threshold.toFixed(2) ? 3 : 2;
}
