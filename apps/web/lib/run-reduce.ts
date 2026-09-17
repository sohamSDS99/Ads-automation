/**
 * Folding a stream event into the run the console is showing.
 *
 * The alternative — refetch `GET /runs/{id}` on every event — is a request per
 * node per state change, and it makes the picture lag the feed. This applies
 * what the event says and nothing more; anything it gets wrong is corrected by
 * the reconciliation fetch every reconnect performs (PRD §13.5 #2).
 */
import type { NodeState, RunDetail } from "@/lib/api/runs";
import type { RunEvent } from "@/lib/run-events";
import type { RunStatus } from "@/lib/api/projects";

export function applyRunEvent(run: RunDetail, event: RunEvent): RunDetail {
  const data = event.data;
  const nodeId = typeof data.node_id === "string" ? data.node_id : null;

  switch (event.type) {
    case "run.status":
      return { ...run, status: asRunStatus(data.status) ?? run.status };

    case "run.completed": {
      const finished = { ...run, status: asRunStatus(data.status) ?? run.status };
      finished.finished_at = finished.finished_at ?? new Date().toISOString();
      if (typeof data.cost_usd === "string") finished.cost_usd = data.cost_usd;
      if (data.error !== undefined) {
        finished.error = (data.error as Record<string, unknown> | null) ?? null;
      }
      return finished;
    }

    case "node.started":
      return patchNode(run, nodeId, (node) => ({
        ...node,
        status: "running",
        attempt: numberOr(data.attempt, node.attempt),
        started_at: node.started_at ?? new Date().toISOString(),
        error: null,
      }));

    case "node.tokens":
      return withRunCost(
        patchNode(run, nodeId, (node) => ({
          ...node,
          token_in: numberOr(data.token_in, node.token_in),
          token_out: numberOr(data.token_out, node.token_out),
          cost_usd: typeof data.cost_usd === "string" ? data.cost_usd : node.cost_usd,
        })),
      );

    case "node.completed":
      return withRunCost(
        patchNode(run, nodeId, (node) => ({
          ...node,
          status: "succeeded",
          model: typeof data.model === "string" ? data.model : node.model,
          cost_usd: typeof data.cost_usd === "string" ? data.cost_usd : node.cost_usd,
          latency_ms: numberOr(data.latency_ms, node.latency_ms),
          finished_at: new Date().toISOString(),
        })),
      );

    case "node.failed":
      return patchNode(run, nodeId, (node) => ({
        ...node,
        // A failure that will be retried leaves the node running: the attempt
        // failed, the node has not.
        status: data.will_retry === true ? "running" : "failed",
        attempt: numberOr(data.attempt, node.attempt),
        error: (data.error as Record<string, unknown> | null) ?? node.error,
      }));

    case "approval.required":
      return {
        ...patchNode(run, nodeId, (node) => ({ ...node, status: "awaiting_approval" })),
        status: "awaiting_approval",
      };

    default:
      return run;
  }
}

function patchNode(
  run: RunDetail,
  nodeId: string | null,
  patch: (node: NodeState) => NodeState,
): RunDetail {
  if (nodeId === null) return run;
  let touched = false;
  const nodes = run.nodes.map((node) => {
    if (node.id !== nodeId) return node;
    touched = true;
    return patch(node);
  });
  return touched ? { ...run, nodes } : run;
}

/** The run's spend is its nodes' spend — recomputed so the two cannot drift. */
function withRunCost(run: RunDetail): RunDetail {
  const total = run.nodes.reduce((sum, node) => sum + Number(node.cost_usd ?? 0), 0);
  return { ...run, cost_usd: total.toFixed(6) };
}

function numberOr(value: unknown, fallback: number | null): number | null {
  return typeof value === "number" ? value : fallback;
}

const RUN_STATUSES = new Set<RunStatus>([
  "queued",
  "running",
  "awaiting_approval",
  "succeeded",
  "failed",
  "cancelled",
]);

function asRunStatus(value: unknown): RunStatus | null {
  return typeof value === "string" && RUN_STATUSES.has(value as RunStatus)
    ? (value as RunStatus)
    : null;
}
