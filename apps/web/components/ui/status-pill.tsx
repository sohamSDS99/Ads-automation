import { cn } from "@/lib/utils";
import type { RunStatus } from "@/lib/api/projects";

/**
 * A run's state, in the exact colours of the stage diagram (PRD §13.2).
 *
 * The dot carries the colour and the word carries the meaning, so the pill
 * survives being read by someone who cannot separate amber from green.
 */
const STATES: Record<RunStatus, { label: string; dot: string }> = {
  queued: { label: "Queued", dot: "bg-status-skipped" },
  running: { label: "Running", dot: "bg-status-running" },
  awaiting_approval: { label: "Awaiting approval", dot: "bg-status-gate" },
  succeeded: { label: "Succeeded", dot: "bg-status-success" },
  failed: { label: "Failed", dot: "bg-status-failed" },
  cancelled: { label: "Cancelled", dot: "bg-status-skipped" },
};

export function StatusPill({ status, className }: { status: RunStatus; className?: string }) {
  const state = STATES[status] ?? { label: status, dot: "bg-status-skipped" };
  return (
    <span
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-xs font-medium text-fg",
        className,
      )}
    >
      <span
        aria-hidden
        className={cn(
          "size-1.5 rounded-full",
          state.dot,
          status === "running" && "animate-pulse motion-reduce:animate-none",
        )}
      />
      {state.label}
    </span>
  );
}
