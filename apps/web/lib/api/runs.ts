/**
 * One run: its DAG, its nodes, the controls, and who is watching.
 *
 * The console reads this once on open and then again on every SSE reconnect —
 * PRD §13.5 #2 makes the full state the reconciliation point, because a stream
 * that dropped events is invisible from the events themselves.
 */
import { apiFetch } from "@/lib/api";
import type { RunStatus } from "@/lib/api/projects";

export type NodeStatus =
  | "queued"
  | "running"
  | "awaiting_approval"
  // Halted for one named person (Stage 03 PRD §8.4; Stage 04's H3 at 4.6.3),
  // not for any holder of a role — the api's `NodeStatus.AWAITING_HUMAN_TASK`.
  | "awaiting_human_task"
  | "succeeded"
  | "failed"
  | "skipped";

export type TaskClass =
  | "extract"
  | "classify"
  | "synthesize"
  | "critique"
  // Stage 04 (S4-P4): copy is written by `copywrite`, briefs are read by
  // `vision`, and media nodes declare the two generation classes.
  | "copywrite"
  | "vision"
  | "image_gen"
  | "video_gen";

export type NodeState = {
  id: string;
  name: string;
  stage: string;
  task_class: TaskClass;
  depends_on: string[];
  gate: boolean;
  /** Null until the run reaches this node — "not started" is not a status. */
  status: NodeStatus | null;
  attempt: number | null;
  model: string | null;
  token_in: number | null;
  token_out: number | null;
  cost_usd: string | null;
  latency_ms: number | null;
  started_at: string | null;
  finished_at: string | null;
  error: Record<string, unknown> | null;
};

export type DagEdge = { source: string; target: string };

/**
 * A source that did not fully answer during a run (PRD §15 NF4).
 *
 * `degraded` means a connector broke mid-run and `detail` carries its own words
 * — for the Transparency Center, the selector that stopped matching.
 * `unavailable` means nothing of that kind is connected, which is a setup state
 * and not a fault.
 */
export type DegradedSource = {
  kind: string;
  nodes: string[];
  detail: string | null;
  severity: "degraded" | "unavailable";
};

export type RunStage = "research" | "plan" | "guideline" | "creative";

export type RunDetail = {
  id: string;
  project_id: string;
  /**
   * Which DAG this run executes. The plan console reads it to refuse a run
   * opened under the wrong route rather than drawing a plan-shaped shell
   * around research nodes.
   *
   * Optional on purpose. `web` and `api` are separate Railway services and
   * deploy independently, so a browser running this build can be talking to an
   * api that predates the field. Absent means "unknown", which must not read as
   * "mismatched".
   */
  stage?: RunStage;
  status: RunStatus;
  mode: "full" | "partial";
  trigger: "manual" | "schedule";
  triggered_by: string | null;
  triggered_by_name: string | null;
  /**
   * The run this one follows. Null for a project's first run, which is what the
   * Report Viewer checks before offering to compare.
   */
  parent_run_id: string | null;
  selected_node_ids: string[];
  cost_usd: string;
  /**
   * The ceiling this run is actually held to, resolved server-side and keyed
   * by stage. Read this rather than the workspace setting: §17 PF4 gives a
   * plan run its own cap, and deriving one here from `max_run_cost_usd` drew
   * the meter against $15 while the executor killed the run at $8.
   */
  cost_cap_usd: string;
  token_in: number;
  token_out: number;
  started_at: string | null;
  finished_at: string | null;
  error: Record<string, unknown> | null;
  nodes: NodeState[];
  edges: DagEdge[];
  /**
   * Derived from the nodes' own `coverage` output, so it is durable: the banner
   * is still there after a reload, unlike anything only announced over SSE.
   */
  degraded_sources: DegradedSource[];
  /**
   * A creative run's two meters (Stage 04 PRD §15.4 C, law 43): total and
   * media, each spent + reserved against its own cap — the numbers the
   * reserve script compares before it grants a media job. Null on every other
   * stage, and when the api cannot say what is reserved; optional because an
   * api older than this build does not send it.
   */
  creative_spend?: CreativeSpend | null;
};

export type SpendMeter = { spent_usd: string; reserved_usd: string; cap_usd: string };
export type CreativeSpend = { total: SpendMeter; media: SpendMeter };

export type NodeRunDetail = {
  run_id: string;
  node_id: string;
  name: string;
  stage: string;
  status: NodeStatus;
  attempt: number;
  input_hash: string | null;
  output: Record<string, unknown> | null;
  evidence_ids: string[];
  prompt: string | null;
  model: string | null;
  token_in: number;
  token_out: number;
  cost_usd: string;
  latency_ms: number | null;
  started_at: string | null;
  finished_at: string | null;
  error: Record<string, unknown> | null;
};

export type RunViewer = { id: string; name: string };
export type Presence = { viewers: RunViewer[]; total: number };

export function getRun(runId: string): Promise<RunDetail> {
  return apiFetch(`/runs/${runId}`);
}

export function getNodeRun(runId: string, nodeId: string): Promise<NodeRunDetail> {
  return apiFetch(`/runs/${runId}/nodes/${encodeURIComponent(nodeId)}`);
}

export function cancelRun(runId: string): Promise<RunDetail> {
  return apiFetch(`/runs/${runId}/cancel`, { method: "POST" });
}

export function retryFailed(runId: string): Promise<RunDetail> {
  return apiFetch(`/runs/${runId}/retry-failed`, { method: "POST" });
}

/** Say "I have this console open", and learn who else does (PRD §13.4 B). */
export function checkIn(runId: string): Promise<Presence> {
  return apiFetch(`/runs/${runId}/presence`, { method: "POST" });
}

const TERMINAL: ReadonlySet<RunStatus> = new Set(["succeeded", "failed", "cancelled"]);

/** Whether this run is still capable of producing an event. */
export function isLive(status: RunStatus): boolean {
  return !TERMINAL.has(status);
}

/**
 * `offer_economics` → `Offer economics`.
 *
 * Node names are identifiers in the registry, and the registry is right to keep
 * them that way. This is the only place they become a label.
 */
export function nodeLabel(name: string): string {
  const words = name.replace(/_/g, " ").trim();
  return words.charAt(0).toUpperCase() + words.slice(1);
}

/** Every stage present in this run, in DAG order, with its nodes. */
export function byStage(nodes: NodeState[]): { stage: string; nodes: NodeState[] }[] {
  const stages: { stage: string; nodes: NodeState[] }[] = [];
  for (const node of nodes) {
    const current = stages.at(-1);
    if (current?.stage === node.stage) current.nodes.push(node);
    else stages.push({ stage: node.stage, nodes: [node] });
  }
  return stages;
}
