import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * A quiet label. Neutral by default because the palette's colours mean run
 * state, and a coloured badge elsewhere would read as one.
 */
export function Badge({
  children,
  tone = "neutral",
  className,
}: {
  children: ReactNode;
  tone?: "neutral" | "accent" | "warning" | "danger";
  className?: string;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-xs font-medium",
        tone === "neutral" && "text-fg-muted",
        tone === "accent" && "border-accent/40 bg-accent-soft text-accent-soft-fg",
        tone === "warning" && "border-status-gate/50 text-fg",
        tone === "danger" && "border-status-failed/50 text-fg",
        className,
      )}
    >
      {children}
    </span>
  );
}
