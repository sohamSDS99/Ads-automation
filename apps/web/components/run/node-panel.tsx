"use client";

import { Hourglass } from "lucide-react";
import { useState } from "react";

import { ApprovalCard } from "@/components/approvals/approval-card";
import { PlanNodeFigure } from "@/components/plan/node-figure";
import { CalcPanel } from "@/components/run/calc-panel";
import { NodeEvidence } from "@/components/run/node-evidence";
import { NodeDot, stateOf } from "@/components/run/node-status";
import { stageTitle } from "@/components/run/stages";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { JsonTree } from "@/components/ui/json-tree";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs } from "@/components/ui/tabs";
import { ApiError } from "@/lib/api";
import type { ApprovalItem } from "@/lib/api/approvals";
import { nodeLabel, type NodeState, type RunStage } from "@/lib/api/runs";
import { absoluteTime, usd } from "@/lib/format";
import { useNodeRun } from "@/lib/queries";
import { cn } from "@/lib/utils";

type TabId = "output" | "evidence" | "prompt" | "metrics" | "calc";

/**
 * Everything one node did (PRD §13.4 B, right panel).
 *
 * A node that has not run yet is the common case for most of a run's life, and
 * the API answers 404 for it. That is an empty state here, not an error: the
 * person clicked a node to find out where it stands, and "not yet" is the
 * answer.
 */
export function NodePanel({
  runId,
  projectId,
  node,
  approval,
  onDecided,
  stage = "research",
  className,
}: {
  runId: string;
  projectId: string;
  node: NodeState | null;
  approval: ApprovalItem | null;
  onDecided: () => void;
  /** A plan run gains the Calc tab; a research run has no arithmetic to show. */
  stage?: RunStage;
  className?: string;
}) {
  const [tab, setTab] = useState<TabId>("output");
  const detail = useNodeRun(runId, node?.id ?? null);

  if (node === null) {
    return (
      <aside className={cn("grid place-items-center p-6", className)}>
        <p className="max-w-56 text-center text-sm text-fg-subtle">
          Pick a node to see what it produced, what it read, and what it cost.
        </p>
      </aside>
    );
  }

  const notRunYet = detail.error instanceof ApiError && detail.error.status === 404;
  const data = detail.data ?? null;

  return (
    <aside className={cn("flex min-h-0 flex-col", className)} aria-label={`Node ${node.id}`}>
      <header className="border-b px-4 py-3">
        <div className="flex items-center gap-2">
          <NodeDot status={node.status} />
          <h2 className="truncate text-[length:var(--text-md)] font-medium tracking-tight">
            {nodeLabel(node.name)}
          </h2>
        </div>
        <p className="mt-1 flex flex-wrap items-baseline gap-x-2 gap-y-0.5 text-xs text-fg-subtle">
          <span className="font-mono">{node.id}</span>
          <span>·</span>
          <span>{stageTitle(node.stage)}</span>
          <span>·</span>
          <span className={stateOf(node.status).text}>{stateOf(node.status).label}</span>
        </p>
      </header>

      {approval ? (
        <div className="border-b p-3">
          <ApprovalCard approval={approval} onDecided={onDecided} />
        </div>
      ) : null}

      <Tabs
        label="Node detail"
        value={tab}
        onChange={(id) => setTab(id as TabId)}
        items={[
          { id: "output", label: "Output" },
          {
            id: "evidence",
            label: "Evidence",
            badge: data && data.evidence_ids.length > 0 ? data.evidence_ids.length : undefined,
          },
          { id: "prompt", label: "Prompt" },
          { id: "metrics", label: "Metrics" },
          // Stage 02 PRD §15.3 B: the fifth tab, and only on a plan run. A
          // research node has no `plan_calc` rows, so offering it there would
          // be a tab that is always empty.
          ...(stage === "plan" ? [{ id: "calc", label: "Calc" }] : []),
        ]}
      />

      <div className="min-h-0 flex-1 overflow-y-auto p-3">
        {detail.isPending && !notRunYet ? <Skeleton className="h-40 w-full" /> : null}

        {notRunYet ? (
          <EmptyState
            icon={Hourglass}
            title="Not run yet"
            description="This node is waiting for the nodes it depends on. Its output, evidence and cost appear here the moment it starts."
          />
        ) : null}

        {data ? (
          <>
            {data.error ? (
              <Alert tone="error" title="This node failed" className="mb-3">
                <pre className="mt-1 break-words whitespace-pre-wrap font-mono text-xs">
                  {JSON.stringify(data.error, null, 2)}
                </pre>
              </Alert>
            ) : null}

            {tab === "output" ? (
              data.output ? (
                <>
                  {/* Only a plan run has nodes with a shape worth drawing; a
                      research node id never matches, so the call is free. */}
                  {stage === "plan" ? (
                    <PlanNodeFigure nodeId={node.id} output={data.output} />
                  ) : null}
                  <JsonTree value={data.output} />
                </>
              ) : (
                <p className="text-sm text-fg-subtle">No output recorded for this attempt.</p>
              )
            ) : null}

            {tab === "evidence" ? (
              <NodeEvidence projectId={projectId} ids={data.evidence_ids} />
            ) : null}

            {tab === "prompt" ? <Prompt text={data.prompt} /> : null}

            {tab === "metrics" ? <Metrics detail={data} /> : null}

            {/* `runId` is the plan run: `plan_calc` rows are keyed by it and
                the node, which is exactly this panel's subject. */}
            {tab === "calc" ? (
              <CalcPanel planRunId={runId} nodeId={node.id} projectId={projectId} />
            ) : null}
          </>
        ) : null}
      </div>
    </aside>
  );
}

function Prompt({ text }: { text: string | null }) {
  if (!text) {
    return (
      <p className="text-sm text-fg-subtle">
        This node called no model — its answer is computed in Python (PRD §18 law 3).
      </p>
    );
  }
  return (
    <pre className="break-words whitespace-pre-wrap rounded-[var(--radius)] border bg-surface p-3 font-mono text-xs leading-relaxed text-fg-muted">
      {text}
    </pre>
  );
}

function Metrics({ detail }: { detail: NonNullable<ReturnType<typeof useNodeRun>["data"]> }) {
  const rows: [string, string][] = [
    ["Model", detail.model ?? "—"],
    ["Attempt", String(detail.attempt)],
    ["Tokens in", detail.token_in.toLocaleString()],
    ["Tokens out", detail.token_out.toLocaleString()],
    ["Cost", usd(detail.cost_usd, 4)],
    ["Latency", detail.latency_ms === null ? "—" : `${(detail.latency_ms / 1000).toFixed(1)}s`],
    ["Started", absoluteTime(detail.started_at) || "—"],
    ["Finished", absoluteTime(detail.finished_at) || "—"],
    ["Input hash", detail.input_hash ?? "—"],
  ];
  return (
    <dl className="divide-y">
      {rows.map(([label, value]) => (
        <div key={label} className="flex items-baseline justify-between gap-4 py-2">
          <dt className="text-sm text-fg-muted">{label}</dt>
          <dd data-numeric className="truncate font-mono text-xs text-fg">
            {value}
          </dd>
        </div>
      ))}
    </dl>
  );
}
