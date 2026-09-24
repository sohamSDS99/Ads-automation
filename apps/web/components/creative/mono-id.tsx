"use client";

import { Check } from "lucide-react";
import { useEffect, useState } from "react";

/**
 * An id or a hash, the way §15.2 rule 5 asks for it: the mono stack,
 * truncated in the middle (`a41f…9c2e`) because both ends are what a person
 * matches by eye, and copied whole on click.
 *
 * A `<button>` rather than text with a click handler, so it is reachable by
 * keyboard and announces what it does. The full value rides the tooltip and
 * the accessible name; "copied" is announced in a live region beside it,
 * because a name that changes under the focus is not reliably re-read.
 */
export function MonoId({ value, label }: { value: string; label: string }) {
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (!copied) return;
    const timer = window.setTimeout(() => setCopied(false), 2000);
    return () => window.clearTimeout(timer);
  }, [copied]);

  const short = value.length > 12 ? `${value.slice(0, 4)}…${value.slice(-4)}` : value;

  return (
    <span className="inline-flex items-center gap-1">
      <button
        type="button"
        title={value}
        aria-label={`Copy ${label} ${value}`}
        onClick={async () => {
          await navigator.clipboard.writeText(value);
          setCopied(true);
        }}
        className="rounded-sm font-mono text-xs text-fg underline decoration-border-strong decoration-dotted underline-offset-4 transition-colors hover:text-accent hover:decoration-accent"
      >
        {short}
      </button>
      {copied ? <Check className="size-3.5 text-status-success" aria-hidden /> : null}
      <span aria-live="polite" className="sr-only">
        {copied ? `${label} copied` : ""}
      </span>
    </span>
  );
}
