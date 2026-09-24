"use client";

import { useQueryClient } from "@tanstack/react-query";
import { CalendarClock, FileText, LayoutGrid, List, Loader2, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useMemo, useState } from "react";

import { SpendMeters } from "@/components/creative/spend-meters";
import { DagCanvas, type Lane } from "@/components/run/dag-canvas";
import { LogDrawer } from "@/components/run/log-drawer";
import { NodeDot, NodeStatusLabel } from "@/components/run/node-status";
import { NodePanel } from "@/components/run/node-panel";
import { DegradedBanner } from "@/components/run/degraded-banner";
import { PresenceRow } from "@/components/run/presence-row";
import { RunControls } from "@/components/run/run-controls";
import { StageRail } from "@/components/run/stage-rail";
import { MEDIA_STAGE } from "@/components/run/stages";
import { Alert } from "@/components/ui/alert";
import { Avatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { StatusPill } from "@/components/ui/status-pill";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { isLive, nodeLabel, type NodeState, type RunDetail, type RunStage } from "@/lib/api/runs";
import { absoluteTime, relativeTime, usd } from "@/lib/format";
import { CREATIVE_POLL_MS, errorMessage, keys, useApprovals, useRun } from "@/lib/queries";
import { describe, useRunStream, type RunEvent } from "@/lib/run-events";
import { applyRunEvent } from "@/lib/run-reduce";
import { useRunConsole } from "@/lib/stores/run-console";
import { cn } from "@/lib/utils";

/** Where each stage's console lives, and what a person calls that stage. */
const CONSOLE: Record<RunStage, { name: string; path: string }> = {
  research: { name: "research", path: "runs" },
  plan: { name: "campaign planning", path: "plan/runs" },
  guideline: { name: "content guidelines", path: "guidelines/runs" },
  creative: { name: "copy & creative", path: "creative/runs" },
};

const MEDIA_LANE: Lane = {
  id: "media",
  label: "Media branch",
  hint: "runs beside the copy, from the brief to the pre-flight checks",
};

/** Stage 04's media branch in its own lane (§15.4 C); everything else on the main line. */
function creativeLane(node: NodeState): Lane | null {
  return node.stage === MEDIA_STAGE ? MEDIA_LANE : null;
}

/**
 * The run console (PRD §13.4 B).
 *
 * Three sources feed one picture and they are deliberately ranked. `GET
 * /runs/{id}` is the truth; the SSE feed is speed, folded into that truth as it
 * arrives; the log is a record of the feed and nothing reads state from it.
 * Where speed and truth can disagree — a gate decided by someone else while the
 * stream replays its history — the console asks the API again rather than
 * guessing.
 */
export function RunConsole({
  runId,
  projectId,
  stage = "research",
}: {
  runId: string;
  projectId: string;
  /**
   * Which pipeline this console is showing (Stage 02 PRD §15.3 B). The DAG,
   * the rail and the node panel are all driven by what the API returns, so
   * this changes exactly two things: the node panel gains its Calc tab, and
   * the console refuses a run that belongs to the other stage.
   */
  stage?: RunStage;
}) {
  const queryClient = useQueryClient();
  // A creative run's meters move with media jobs, which the stream does not
  // announce, so its console re-reads the run while it is live.
  const run = useRun(runId, { pollMs: stage === "creative" ? CREATIVE_POLL_MS : undefined });
  const approvals = useApprovals({ run_id: runId });
  const [selected, setSelected] = useState<string | null>(null);
  const [view, setView] = useState<"canvas" | "list">("canvas");
  const { attach, append } = useRunConsole();

  useEffect(() => attach(runId), [attach, runId]);

  const live = run.data ? isLive(run.data.status) : false;

  const onEvent = useCallback(
    (event: RunEvent) => {
      append({
        id: event.id || `${Date.now()}-${Math.random()}`,
        at: Date.now(),
        type: event.type,
        nodeId: typeof event.data.node_id === "string" ? event.data.node_id : null,
        text: describe(event),
      });

      // A finished run replays its whole history when the console opens. Those
      // events are a record, not news: folding them back in would re-stage a
      // run that is already over.
      if (live) {
        queryClient.setQueryData<RunDetail>(keys.run(runId), (current) =>
          current ? applyRunEvent(current, event) : current,
        );
      }

      // Two events change something this console cannot derive: a gate opening
      // and the run ending. Both are rare, so both are worth one authoritative
      // read rather than a guess.
      if (event.type === "approval.required") {
        void queryClient.invalidateQueries({ queryKey: ["approvals"] });
        void queryClient.invalidateQueries({ queryKey: keys.run(runId) });
        const node = event.data.node_id;
        if (typeof node === "string") setSelected(node);
      }
      if (event.type === "run.completed") {
        void queryClient.invalidateQueries({ queryKey: keys.run(runId) });
        void queryClient.invalidateQueries({ queryKey: keys.projectRuns(projectId) });
      }
      if (event.type === "node.completed" || event.type === "node.failed") {
        const node = event.data.node_id;
        if (typeof node === "string") {
          void queryClient.invalidateQueries({ queryKey: keys.nodeRun(runId, node) });
        }
      }
    },
    [append, live, projectId, queryClient, runId],
  );

  const onReconnect = useCallback(() => {
    // Whatever the gap contained, the run itself still knows. This is PRD
    // §13.5 #2's reconciliation, and it is why a dropped connection costs a
    // request rather than an accurate screen.
    void queryClient.invalidateQueries({ queryKey: keys.run(runId) });
    void queryClient.invalidateQueries({ queryKey: ["approvals"] });
  }, [queryClient, runId]);

  const stream = useRunStream({ runId, enabled: run.data !== undefined, onEvent, onReconnect });

  const nodes = useMemo(() => run.data?.nodes ?? [], [run.data]);
  const selectedNode = nodes.find((node) => node.id === selected) ?? null;
  const pendingApproval =
    approvals.data?.items.find(
      (item) => item.status === "pending" && item.node_id === selectedNode?.id,
    ) ?? null;
  const openGates = approvals.data?.items.filter((item) => item.status === "pending") ?? [];

  // The first thing worth looking at, chosen once: whatever is running, or
  // whatever stopped the run. Re-selecting on every poll would fight the click.
  useEffect(() => {
    if (selected !== null || nodes.length === 0) return;
    const interesting =
      nodes.find((node) => node.status === "awaiting_approval") ??
      nodes.find((node) => node.status === "running") ??
      nodes.find((node) => node.status === "failed");
    if (interesting) setSelected(interesting.id);
  }, [nodes, selected]);

  if (run.isPending) {
    return (
      <div className="flex h-full flex-col gap-3">
        <Skeleton className="h-16 w-full" />
        <Skeleton className="min-h-0 flex-1 w-full" />
      </div>
    );
  }

  if (!run.data) {
    return (
      <Alert tone="error" title="This run could not be loaded">
        {errorMessage(run) ?? "It may have been removed."}
      </Alert>
    );
  }

  const detail = run.data;

  // A run id from the other stage renders a console that looks right and
  // describes the wrong pipeline — plan stage headings over research nodes, a
  // Calc tab with nothing behind it. Cheaper to say so and offer the way
  // across than to let someone read it as the run they asked for.
  //
  // Fails open on an absent `stage`: `web` and `api` deploy separately, and a
  // guard that treated "the api is older than this build" as "wrong stage"
  // would blank every console in the gap. Unknown is not mismatched.
  if (detail.stage !== undefined && detail.stage !== stage) {
    const href = `/projects/${projectId}/${CONSOLE[detail.stage].path}/${runId}`;
    return (
      <Alert tone="warning" title="This run belongs to another stage">
        <p>
          This is a {CONSOLE[detail.stage].name} run, opened under the {CONSOLE[stage].name}{" "}
          console.
        </p>
        <Link href={href} className="mt-2 inline-block text-accent hover:underline">
          Open it where it belongs
        </Link>
      </Alert>
    );
  }

  return (
    <div className="flex h-full min-h-[34rem] flex-col overflow-hidden rounded-[var(--radius)] border bg-surface-raised">
      <header className="flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-b px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <StatusPill status={detail.status} />
          <Attribution run={detail} />
        </div>
        {stage === "creative" ? <SpendMeters spend={detail.creative_spend} /> : null}
        <div className="flex items-center gap-3">
          <PresenceRow runId={runId} />
          <StreamState state={stream} live={live} />
          <Button
            variant="ghost"
            size="icon"
            onClick={() => void run.refetch()}
            title="Refresh from the API"
          >
            <RefreshCw aria-hidden className={cn(run.isFetching && "animate-spin")} />
            <span className="sr-only">Refresh</span>
          </Button>
          {/* Research only for now. The plan run's equivalent is the Plan
              Viewer at `/plan/runs/{id}/plan`, which is the next slice — a
              link to a route that does not exist yet is worse than no link. */}
          {stage === "research" ? (
            <Link
              href={`/projects/${projectId}/runs/${runId}/report`}
              className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline"
            >
              <FileText className="size-4" aria-hidden />
              Report
            </Link>
          ) : null}
          {stage === "creative" ? (
            <Link
              href={`/projects/${projectId}/creative/runs/${runId}/brief`}
              className="inline-flex items-center gap-1.5 text-sm text-accent hover:underline"
            >
              <FileText className="size-4" aria-hidden />
              Brief
            </Link>
          ) : null}
        </div>
      </header>

      {detail.degraded_sources.length > 0 ? (
        // Above the gate strip and below the header: it is a fact about the
        // whole run, and it has to be seen before anyone reads a node's output
        // and concludes the competitor section is simply short.
        <div className="border-b px-4 py-3">
          <DegradedBanner sources={detail.degraded_sources} />
        </div>
      ) : null}

      {openGates.length > 0 && !openGates.some((gate) => gate.node_id === selectedNode?.id) ? (
        <div className="border-b px-4 py-2">
          <p className="text-sm text-fg-muted">
            {openGates.length === 1 ? "A gate is" : `${openGates.length} gates are`} waiting on a
            decision.{" "}
            <button
              type="button"
              className="text-accent hover:underline"
              onClick={() => setSelected(openGates[0]!.node_id)}
            >
              Open {openGates.length === 1 ? "it" : "the first"}
            </button>
          </p>
        </div>
      ) : null}

      <div className="flex min-h-0 flex-1 flex-col xl:flex-row">
        <StageRail
          nodes={nodes}
          selected={selected}
          onSelect={setSelected}
          className="max-h-72 shrink-0 border-b xl:max-h-none xl:w-[17.5rem] xl:border-r xl:border-b-0"
        />

        {/* `min-w-0`: without it a flex child is sized by its content, and the
            list view's table is wider than the column — it pushed the node
            panel off the right edge. */}
        <div className="relative hidden min-h-0 min-w-0 flex-1 xl:block">
          {view === "canvas" ? (
            <DagCanvas
              nodes={nodes}
              edges={detail.edges}
              selected={selected}
              onSelect={setSelected}
              laneOf={stage === "creative" ? creativeLane : undefined}
            />
          ) : (
            <NodeTable nodes={nodes} selected={selected} onSelect={setSelected} />
          )}
          <div className="absolute top-3 right-3 z-10 flex rounded-[var(--radius)] border bg-surface-raised p-0.5">
            <ViewButton
              active={view === "canvas"}
              onClick={() => setView("canvas")}
              icon={LayoutGrid}
              label="Graph"
            />
            <ViewButton
              active={view === "list"}
              onClick={() => setView("list")}
              icon={List}
              label="List"
            />
          </div>
        </div>

        <NodePanel
          runId={runId}
          projectId={projectId}
          node={selectedNode}
          approval={pendingApproval}
          onDecided={onReconnect}
          stage={stage}
          className="min-h-0 flex-1 border-t xl:w-[26.25rem] xl:flex-none xl:border-t-0 xl:border-l"
        />
      </div>

      <LogDrawer />
      {/* A creative run's spend is the header's two meters; a third, single
          one here would be drawn against a cap the run is not held to. */}
      <RunControls run={detail} showSpend={stage !== "creative"} />
    </div>
  );
}

function Attribution({ run }: { run: RunDetail }) {
  if (run.trigger === "schedule") {
    return (
      <p className="flex items-center gap-2 text-sm text-fg-muted">
        <CalendarClock className="size-4 text-fg-subtle" aria-hidden />
        Scheduled run
        {run.started_at ? (
          <time dateTime={run.started_at} title={absoluteTime(run.started_at)}>
            · {relativeTime(run.started_at)}
          </time>
        ) : null}
      </p>
    );
  }
  const name = run.triggered_by_name ?? "Someone no longer in this workspace";
  return (
    <p className="flex min-w-0 items-center gap-2 text-sm text-fg-muted">
      <Avatar name={name} size="sm" />
      <span className="truncate">
        Triggered by <span className="text-fg">{name}</span>
        {run.started_at ? (
          <>
            {" · "}
            <time dateTime={run.started_at} title={absoluteTime(run.started_at)}>
              {relativeTime(run.started_at)}
            </time>
          </>
        ) : null}
      </span>
    </p>
  );
}

/**
 * Whether this screen is still hearing from the run.
 *
 * Only ever shown when it matters: a finished run has nothing to stream, so
 * "closed" there is the normal state and saying so would read as a fault.
 */
function StreamState({ state, live }: { state: string; live: boolean }) {
  if (!live) return null;
  if (state === "live") {
    return (
      <span className="flex items-center gap-1.5 text-xs text-fg-muted">
        <span
          aria-hidden
          className="size-1.5 rounded-full bg-status-success motion-safe:animate-pulse"
        />
        Live
      </span>
    );
  }
  return (
    <span className="flex items-center gap-1.5 text-xs text-status-gate" role="status">
      <Loader2 aria-hidden className="size-3 animate-spin" />
      Reconnecting
    </span>
  );
}

function ViewButton({
  active,
  onClick,
  icon: Icon,
  label,
}: {
  active: boolean;
  onClick: () => void;
  icon: typeof LayoutGrid;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      className={cn(
        "inline-flex items-center gap-1.5 rounded-[calc(var(--radius)-4px)] px-2 py-1 text-xs transition-colors",
        active ? "bg-accent-soft text-accent-soft-fg" : "text-fg-muted hover:text-fg",
      )}
    >
      <Icon className="size-3.5" aria-hidden />
      {label}
    </button>
  );
}

/**
 * The canvas as a table (PRD §13.4 B, "toggle to list view").
 *
 * Not a fallback — it answers a different question. The graph shows what
 * depends on what; this shows what each node cost and how long it took, which
 * is what anyone tuning a run is actually reading.
 */
function NodeTable({
  nodes,
  selected,
  onSelect,
}: {
  nodes: RunDetail["nodes"];
  selected: string | null;
  onSelect: (id: string) => void;
}) {
  return (
    <div className="h-full overflow-y-auto">
      <Table label="Run nodes">
        <thead>
          <tr>
            <Th>Node</Th>
            <Th>Status</Th>
            <Th>Model</Th>
            <Th className="text-right">Tokens</Th>
            <Th className="text-right">Cost</Th>
            <Th className="text-right">Took</Th>
          </tr>
        </thead>
        <tbody>
          {nodes.map((node) => (
            <Tr
              key={node.id}
              className={cn(
                "cursor-pointer transition-colors",
                node.id === selected && "bg-accent-soft hover:bg-accent-soft",
              )}
              onClick={() => onSelect(node.id)}
            >
              <Td>
                <button
                  type="button"
                  onClick={() => onSelect(node.id)}
                  className="flex items-center gap-2 text-left"
                >
                  <NodeDot status={node.status} />
                  <span className="truncate text-fg">{nodeLabel(node.name)}</span>
                  <span className="font-mono text-[0.6875rem] text-fg-subtle">{node.id}</span>
                </button>
              </Td>
              <Td className="whitespace-nowrap">
                <NodeStatusLabel status={node.status} />
              </Td>
              <Td className="max-w-48 truncate font-mono text-xs text-fg-muted">
                {node.model ?? "—"}
              </Td>
              <Td data-numeric className="text-right text-fg-muted">
                {node.token_in === null && node.token_out === null
                  ? "—"
                  : ((node.token_in ?? 0) + (node.token_out ?? 0)).toLocaleString()}
              </Td>
              <Td data-numeric className="text-right text-fg-muted">
                {node.cost_usd === null ? "—" : usd(node.cost_usd, 4)}
              </Td>
              <Td data-numeric className="text-right text-fg-muted">
                {node.latency_ms === null ? "—" : `${(node.latency_ms / 1000).toFixed(1)}s`}
              </Td>
            </Tr>
          ))}
        </tbody>
      </Table>
    </div>
  );
}
