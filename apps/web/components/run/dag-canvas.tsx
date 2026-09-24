"use client";

import {
  Background,
  BackgroundVariant,
  Controls,
  Handle,
  Position,
  ReactFlow,
  type Edge,
  type Node,
  type NodeProps,
} from "@xyflow/react";
import { ShieldCheck } from "lucide-react";
import { useTheme } from "next-themes";
import { useEffect, useMemo, useState } from "react";

import "@xyflow/react/dist/style.css";

import { NodeDot, NodeStatusLabel } from "@/components/run/node-status";
import { nodeLabel, type DagEdge, type NodeState } from "@/lib/api/runs";
import { cn } from "@/lib/utils";

/**
 * The DAG, drawn (PRD §13.4 B, centre).
 *
 * Layout is computed here rather than stored: a node's column is the longest
 * path from any root, which is the order the wavefront executor actually runs
 * them in. Anything else would draw a shape the run does not follow.
 *
 * The canvas is presentation only — selection, pan and zoom. Every piece of
 * state it renders comes from the run query, so a reconnect that corrects the
 * run corrects the picture with it.
 */
const COLUMN = 260;
const ROW = 96;
/** A node card's drawn size — `w-[200px]` and two lines of text. */
const NODE_WIDTH = 200;
const NODE_HEIGHT = 58;
/** Air between two lanes, and a lane band's padding around its nodes. */
const LANE_GAP = 72;
const LANE_PAD = 16;
const LANE_LABEL = 28;

type FlowData = {
  node: NodeState;
  selected: boolean;
  onSelect: (id: string) => void;
};

type FlowNode = Node<FlowData, "researchNode">;

/**
 * A lane a node is drawn in. Nodes of one lane share a band of rows, so a
 * branch that runs beside the main line — Stage 04's media branch — reads as
 * parallel instead of as rows interleaved with the copy nodes it runs beside.
 * `null` is the main line, which gets no band.
 */
export type Lane = { id: string; label: string; hint: string };

type LaneData = { label: string; hint: string };
type LaneFlowNode = Node<LaneData, "lane">;

type Band = { lane: Lane; x: number; y: number; width: number; height: number };

export function DagCanvas({
  nodes,
  edges,
  selected,
  onSelect,
  laneOf,
  className,
}: {
  nodes: NodeState[];
  edges: DagEdge[];
  selected: string | null;
  onSelect: (nodeId: string) => void;
  /** Which lane a node is drawn in; omitted, every node is on the main line. */
  laneOf?: (node: NodeState) => Lane | null;
  className?: string;
}) {
  const colorMode = useResolvedColorMode();

  const flowNodes = useMemo<(FlowNode | LaneFlowNode)[]>(() => {
    const { placed, bands } = layout(nodes, edges, laneOf);
    const cards: FlowNode[] = nodes.map((node) => ({
      id: node.id,
      type: "researchNode" as const,
      position: placed.get(node.id) ?? { x: 0, y: 0 },
      data: { node, selected: node.id === selected, onSelect },
      draggable: false,
      connectable: false,
      selectable: true,
    }));
    // Bands go first and under everything: they are a place, not a node.
    const lanes: LaneFlowNode[] = bands.map((band) => ({
      id: `lane:${band.lane.id}`,
      type: "lane" as const,
      position: { x: band.x, y: band.y },
      data: { label: band.lane.label, hint: band.lane.hint },
      style: { width: band.width, height: band.height },
      draggable: false,
      connectable: false,
      selectable: false,
      focusable: false,
      zIndex: -1,
    }));
    return [...lanes, ...cards];
  }, [nodes, edges, selected, onSelect, laneOf]);

  const flowEdges = useMemo<Edge[]>(() => {
    const status = new Map(nodes.map((node) => [node.id, node.status]));
    return edges.map((edge) => ({
      id: `${edge.source}->${edge.target}`,
      source: edge.source,
      target: edge.target,
      // Only the edges feeding whatever is executing right now move. Animating
      // all of them would say "everything is happening", which is never true.
      animated: status.get(edge.target) === "running",
      style: { stroke: "var(--border-strong)", strokeWidth: 1.5 },
    }));
  }, [nodes, edges]);

  return (
    <div className={cn("h-full w-full", className)}>
      <ReactFlow
        nodes={flowNodes}
        edges={flowEdges}
        nodeTypes={NODE_TYPES}
        colorMode={colorMode}
        fitView
        // `fitView` obeys `minZoom`, which is what keeps a 17-node run from
        // being fitted into a column 470px wide at 30% — legible and pannable
        // beats complete and unreadable.
        fitViewOptions={{ padding: 0.16, maxZoom: 1, minZoom: 0.55 }}
        minZoom={0.3}
        maxZoom={1.6}
        nodesDraggable={false}
        nodesConnectable={false}
        edgesFocusable={false}
        proOptions={{ hideAttribution: false }}
        onInit={(instance) => {
          // `fitView` centres, so a graph wider than the column loses its first
          // column off the left edge — and the first column is where the run
          // starts. When the zoom floor is what clamped the fit, pin the left.
          const viewport = instance.getViewport();
          if (viewport.zoom <= 0.56) instance.setViewport({ ...viewport, x: 24 });
        }}
        onNodeClick={(_, node) => {
          if (node.type !== "lane") onSelect(node.id);
        }}
        aria-label="Run graph"
      >
        <Background variant={BackgroundVariant.Dots} gap={18} size={1} color="var(--border)" />
        <Controls showInteractive={false} position="bottom-left" />
      </ReactFlow>
    </div>
  );
}

function ResearchNode({ data }: NodeProps<FlowNode>) {
  const { node, selected } = data;
  return (
    <button
      type="button"
      onClick={() => data.onSelect(node.id)}
      className={cn(
        "w-[200px] rounded-[var(--radius)] border bg-surface-raised px-3 py-2 text-left transition-colors",
        node.status === "running" && "border-status-running",
        node.status === "failed" && "border-status-failed",
        node.status === "awaiting_approval" && "border-status-gate",
        selected && "ring-2 ring-accent",
        !selected && "hover:bg-surface-hover",
      )}
    >
      <Handle type="target" position={Position.Left} className="!opacity-0" isConnectable={false} />
      <span className="flex items-center gap-1.5">
        <NodeDot status={node.status} />
        <span className="truncate text-sm font-medium text-fg">{nodeLabel(node.name)}</span>
        {node.gate ? (
          <ShieldCheck aria-label="Approval gate" className="size-3.5 shrink-0 text-status-gate" />
        ) : null}
      </span>
      <span className="mt-0.5 flex items-baseline gap-2">
        <span className="font-mono text-[0.6875rem] text-fg-subtle">{node.id}</span>
        <NodeStatusLabel status={node.status} />
      </span>
      <Handle type="source" position={Position.Right} className="!opacity-0" isConnectable={false} />
    </button>
  );
}

/** A lane's band: its name and what it runs beside, and nothing to click. */
function LaneNode({ data }: NodeProps<LaneFlowNode>) {
  return (
    <div
      aria-hidden
      className="pointer-events-none h-full w-full rounded-token border bg-surface"
    >
      <p className="px-3 pt-2 text-xs">
        <span className="font-medium text-fg-muted">{data.label}</span>
        <span className="text-fg-subtle"> · {data.hint}</span>
      </p>
    </div>
  );
}

const NODE_TYPES = { researchNode: ResearchNode, lane: LaneNode };

/**
 * Column = longest path from a root; row = order within the column.
 *
 * Longest path rather than shortest: a node runs when its *last* dependency
 * finishes, so that is the column it is actually reached in. With lanes, each
 * lane is its own band of rows under the one before it, and the columns stay
 * shared — so two branches in the same columns read as running side by side.
 */
function layout(
  nodes: NodeState[],
  edges: DagEdge[],
  laneOf?: (node: NodeState) => Lane | null,
): { placed: Map<string, { x: number; y: number }>; bands: Band[] } {
  const present = new Set(nodes.map((node) => node.id));
  const incoming = new Map<string, string[]>();
  for (const node of nodes) incoming.set(node.id, []);
  for (const edge of edges) {
    if (present.has(edge.source) && present.has(edge.target)) {
      incoming.get(edge.target)?.push(edge.source);
    }
  }

  const depth = new Map<string, number>();
  const resolve = (id: string, seen: Set<string>): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    // A cycle cannot reach here — the API validates the DAG — but a guard costs
    // nothing and a hung layout costs the whole screen.
    if (seen.has(id)) return 0;
    seen.add(id);
    const parents = incoming.get(id) ?? [];
    const value = parents.length === 0 ? 0 : Math.max(...parents.map((p) => resolve(p, seen) + 1));
    depth.set(id, value);
    return value;
  };
  for (const node of nodes) resolve(node.id, new Set());

  // Lanes in order of first appearance; the main line (no lane) first.
  const MAIN = "";
  const order: string[] = [MAIN];
  const meta = new Map<string, Lane>();
  const laneColumns = new Map<string, Map<number, string[]>>([[MAIN, new Map()]]);
  for (const node of nodes) {
    const lane = laneOf?.(node) ?? null;
    const key = lane?.id ?? MAIN;
    if (lane && !meta.has(key)) {
      meta.set(key, lane);
      order.push(key);
      laneColumns.set(key, new Map());
    }
    const columns = laneColumns.get(key)!;
    const column = depth.get(node.id) ?? 0;
    columns.set(column, [...(columns.get(column) ?? []), node.id]);
  }

  const placed = new Map<string, { x: number; y: number }>();
  const bands: Band[] = [];
  let top = 0;
  for (const key of order) {
    const columns = laneColumns.get(key)!;
    if (columns.size === 0) continue;
    const banded = meta.get(key);
    if (banded && top > 0) top += LANE_GAP - (ROW - NODE_HEIGHT);
    const laneTop = top + (banded ? LANE_LABEL : 0);
    const tallest = Math.max(...[...columns.values()].map((column) => column.length), 1);
    for (const [column, ids] of columns) {
      // Columns are centred against the lane's tallest one, so each lane reads
      // as a spine rather than as a staircase hanging off its top edge.
      const offset = ((tallest - ids.length) * ROW) / 2;
      ids.forEach((id, index) => {
        placed.set(id, { x: column * COLUMN, y: laneTop + offset + index * ROW });
      });
    }
    if (banded) {
      const first = Math.min(...columns.keys());
      const last = Math.max(...columns.keys());
      bands.push({
        lane: banded,
        x: first * COLUMN - LANE_PAD,
        y: top - LANE_PAD,
        width: (last - first) * COLUMN + NODE_WIDTH + LANE_PAD * 2,
        height: LANE_LABEL + (tallest - 1) * ROW + NODE_HEIGHT + LANE_PAD * 2,
      });
    }
    top = laneTop + tallest * ROW;
  }
  return { placed, bands };
}

/**
 * The canvas's own light/dark, resolved after mount.
 *
 * `next-themes` cannot know the theme during a server render, and guessing
 * would paint the whole canvas the wrong colour for one frame.
 */
function useResolvedColorMode(): "light" | "dark" {
  const { resolvedTheme } = useTheme();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted && resolvedTheme === "dark" ? "dark" : "light";
}
