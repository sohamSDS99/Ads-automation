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

type FlowData = {
  node: NodeState;
  selected: boolean;
  onSelect: (id: string) => void;
};

type FlowNode = Node<FlowData, "researchNode">;

export function DagCanvas({
  nodes,
  edges,
  selected,
  onSelect,
  className,
}: {
  nodes: NodeState[];
  edges: DagEdge[];
  selected: string | null;
  onSelect: (nodeId: string) => void;
  className?: string;
}) {
  const colorMode = useResolvedColorMode();

  const flowNodes = useMemo<FlowNode[]>(() => {
    const placed = layout(nodes, edges);
    return nodes.map((node) => ({
      id: node.id,
      type: "researchNode" as const,
      position: placed.get(node.id) ?? { x: 0, y: 0 },
      data: { node, selected: node.id === selected, onSelect },
      draggable: false,
      connectable: false,
      selectable: true,
    }));
  }, [nodes, edges, selected, onSelect]);

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
        onNodeClick={(_, node) => onSelect(node.id)}
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

const NODE_TYPES = { researchNode: ResearchNode };

/**
 * Column = longest path from a root; row = order within the column.
 *
 * Longest path rather than shortest: a node runs when its *last* dependency
 * finishes, so that is the column it is actually reached in.
 */
function layout(nodes: NodeState[], edges: DagEdge[]): Map<string, { x: number; y: number }> {
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

  const columns = new Map<number, string[]>();
  for (const node of nodes) {
    const column = depth.get(node.id) ?? 0;
    columns.set(column, [...(columns.get(column) ?? []), node.id]);
  }

  const tallest = Math.max(...[...columns.values()].map((column) => column.length), 1);
  const placed = new Map<string, { x: number; y: number }>();
  for (const [column, ids] of columns) {
    // Columns are centred against the tallest one, so the graph reads as a
    // spine rather than as a staircase hanging off the top edge.
    const offset = ((tallest - ids.length) * ROW) / 2;
    ids.forEach((id, index) => {
      placed.set(id, { x: column * COLUMN, y: offset + index * ROW });
    });
  }
  return placed;
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
