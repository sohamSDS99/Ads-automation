import { CircleAlert, CircleCheck, CircleSlash } from "lucide-react";

import { READINESS_LABEL, type LaunchReadiness } from "@/lib/api/reports";
import { cn } from "@/lib/utils";

/**
 * The verdict, in the stage diagram's colours (PRD §13.4 C).
 *
 * Computed in Python from the blockers, never written by a model — so this is
 * reporting a decision, not making one. Icon and word both carry it; colour
 * alone would leave the whole verdict to a hue.
 */
const LOOK: Record<LaunchReadiness, { icon: typeof CircleCheck; className: string }> = {
  go: { icon: CircleCheck, className: "border-status-success/50 text-status-success" },
  go_with_fixes: { icon: CircleAlert, className: "border-status-gate/60 text-status-gate" },
  no_go: { icon: CircleSlash, className: "border-status-failed/60 text-status-failed" },
};

export function ReadinessBadge({
  readiness,
  className,
}: {
  readiness: LaunchReadiness;
  className?: string;
}) {
  const { icon: Icon, className: tone } = LOOK[readiness];
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-sm font-medium",
        tone,
        className,
      )}
    >
      <Icon className="size-4" aria-hidden />
      {READINESS_LABEL[readiness]}
    </span>
  );
}
