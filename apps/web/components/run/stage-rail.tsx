"use client";

import { ShieldCheck } from "lucide-react";

import { NodeDot, NodeStatusLabel } from "@/components/run/node-status";
import { stageTitle } from "@/components/run/stages";
import { byStage, nodeLabel, type NodeState } from "@/lib/api/runs";
import { cn } from "@/lib/utils";

/**
 * Every node of the run, grouped by stage (PRD §13.4 B, left rail).
 *
 * Not an accordion. The PRD asks for one and a run has five stages of four
 * nodes: collapsing them hides the two lines of the screen that answer "where
 * is it now", and the whole list fits without it. Stages stay as sticky
 * headings so the grouping survives a scroll.
 */
export function StageRail({
  nodes,
  selected,
  onSelect,
  className,
}: {
  nodes: NodeState[];
  selected: string | null;
  onSelect: (nodeId: string) => void;
  className?: string;
}) {
  return (
    <nav aria-label="Run nodes" className={cn("overflow-y-auto", className)}>
      {byStage(nodes).map(({ stage, nodes: group }) => (
        <section key={stage}>
          <h3 className="sticky top-0 z-10 flex items-baseline gap-2 border-b bg-surface px-3 py-2">
            <span className="font-mono text-xs text-fg-subtle">{stage}</span>
            <span className="truncate text-xs font-medium text-fg-muted">{stageTitle(stage)}</span>
          </h3>
          <ul className="p-1.5">
            {group.map((node) => (
              <li key={node.id}>
                <button
                  type="button"
                  onClick={() => onSelect(node.id)}
                  aria-current={node.id === selected ? "true" : undefined}
                  className={cn(
                    "flex w-full items-center gap-2 rounded-[calc(var(--radius)-4px)] px-2 py-1.5 text-left transition-colors",
                    node.id === selected ? "bg-accent-soft" : "hover:bg-surface-hover",
                  )}
                >
                  <NodeDot status={node.status} />
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-1.5">
                      <span
                        className={cn(
                          "truncate text-sm",
                          node.id === selected ? "font-medium text-fg" : "text-fg",
                        )}
                      >
                        {nodeLabel(node.name)}
                      </span>
                      {node.gate ? (
                        <ShieldCheck
                          aria-label="Approval gate"
                          className="size-3.5 shrink-0 text-status-gate"
                        />
                      ) : null}
                    </span>
                    <span className="flex items-baseline gap-2">
                      {/* Muted, not subtle, on the selected row: fg-subtle on
                          the accent tint is under 4.5:1 in both themes. */}
                      <span
                        className={cn(
                          "font-mono text-[0.6875rem]",
                          node.id === selected ? "text-fg-muted" : "text-fg-subtle",
                        )}
                      >
                        {node.id}
                      </span>
                      <NodeStatusLabel status={node.status} onTint={node.id === selected} />
                    </span>
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </nav>
  );
}
