import { cn } from "@/lib/utils";
import type { NodeStatus } from "@/lib/api/runs";

/**
 * One node's state, in the stage diagram's colours (PRD §13.2).
 *
 * `null` is its own state and not an absence: a node the run has not reached
 * yet is waiting, which is different from one that was deliberately skipped.
 * The dot carries the colour and the label carries the meaning, so nothing here
 * depends on telling amber from green.
 */
export const NODE_STATE: Record<NodeStatus | "pending", { label: string; dot: string; text: string }> = {
  pending: { label: "Waiting", dot: "bg-border-strong", text: "text-fg-subtle" },
  queued: { label: "Queued", dot: "bg-border-strong", text: "text-fg-muted" },
  running: { label: "Running", dot: "bg-status-running", text: "text-fg" },
  awaiting_approval: { label: "Needs approval", dot: "bg-status-gate", text: "text-fg" },
  // A creative run halts here at H3 (4.6.3); without it the console crashed on
  // the missing entry the moment such a run was opened.
  awaiting_human_task: { label: "Needs a person", dot: "bg-status-gate", text: "text-fg" },
  succeeded: { label: "Done", dot: "bg-status-success", text: "text-fg-muted" },
  failed: { label: "Failed", dot: "bg-status-failed", text: "text-fg" },
  skipped: { label: "Skipped", dot: "bg-status-skipped", text: "text-fg-subtle" },
};

export function stateOf(status: NodeStatus | null) {
  return NODE_STATE[status ?? "pending"];
}

export function NodeDot({ status, className }: { status: NodeStatus | null; className?: string }) {
  const state = stateOf(status);
  return (
    <span
      aria-hidden
      className={cn(
        "size-2 shrink-0 rounded-full",
        state.dot,
        status === "running" && "animate-pulse motion-reduce:animate-none",
        className,
      )}
    />
  );
}

/**
 * `onTint`: the label sits on the selected row's `accent-soft` tint, where
 * `fg-subtle` is under 4.5:1 in both themes — it is raised to `fg-muted` there,
 * as the row's node id already is.
 */
export function NodeStatusLabel({ status, onTint = false }: { status: NodeStatus | null; onTint?: boolean }) {
  const state = stateOf(status);
  const ink = onTint && state.text === "text-fg-subtle" ? "text-fg-muted" : state.text;
  return <span className={cn("text-xs", ink)}>{state.label}</span>;
}
