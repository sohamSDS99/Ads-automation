"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { RotateCcw, Square } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { Tooltip } from "@/components/ui/tooltip";
import { ApiError } from "@/lib/api";
import { cancelRun, isLive, retryFailed, type RunDetail } from "@/lib/api/runs";
import { usd } from "@/lib/format";
import { keys, useWorkspace } from "@/lib/queries";
import { Can, useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

/**
 * Where the run stands, and the two things you can do about it (PRD §13.4 B).
 *
 * `Pause` is absent on purpose. PRD §13.4 lists it, but §14's API has only
 * cancel and retry-failed, and the wavefront executor has no state between
 * waves to stop in. A button that cannot pause anything is worse than no
 * button; the deviation is argued in the phase's PR rather than mimed here.
 */
export function RunControls({
  run,
  className,
}: {
  run: RunDetail;
  className?: string;
}) {
  const { user } = useSession();
  const queryClient = useQueryClient();
  const workspace = useWorkspace();
  const [confirmingCancel, setConfirmingCancel] = useState(false);

  const live = isLive(run.status);
  const done = run.nodes.filter((node) =>
    ["succeeded", "skipped", "failed"].includes(node.status ?? ""),
  ).length;
  const failed = run.nodes.filter((node) => node.status === "failed").length;
  // The run's own ceiling, as the API resolved it. Not the workspace setting:
  // that one is research's, and a plan run holds a different number.
  const cap =
    run.cost_cap_usd ??
    (workspace.data
      ? (workspace.data.settings.max_run_cost_usd ?? workspace.data.default_max_run_cost_usd)
      : null);
  const spent = Number(run.cost_usd);
  const ratio = cap && Number(cap) > 0 ? Math.min(1, spent / Number(cap)) : 0;

  // PRD §13.4: the controls belong to whoever launched the run — anyone else
  // needs to be an admin. The API enforces the same rule.
  const mine = run.triggered_by === user.id;
  const mayControl = mine || user.role === "admin";
  const refuseReason = mayControl
    ? null
    : `Only ${run.triggered_by_name ?? "whoever launched this run"} or an admin can stop it.`;

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: keys.run(run.id) });
    await queryClient.invalidateQueries({ queryKey: keys.projectRuns(run.project_id) });
  };

  const cancel = useMutation({
    mutationFn: () => cancelRun(run.id),
    onSuccess: async () => {
      await invalidate();
      toast.success("Run cancelled", { description: "Nothing further will be spent on it." });
    },
    onError: (error) => complain(error, "The run could not be cancelled"),
  });

  const retry = useMutation({
    mutationFn: () => retryFailed(run.id),
    onSuccess: async (updated) => {
      await invalidate();
      toast.success("Re-running the failed nodes", {
        description: `${updated.nodes.filter((node) => node.status === "queued").length} node(s) queued.`,
      });
    },
    onError: (error) => complain(error, "Those nodes could not be re-run"),
  });

  return (
    <div
      className={cn(
        "flex flex-wrap items-center justify-between gap-x-6 gap-y-3 border-t bg-surface px-4 py-2.5",
        className,
      )}
    >
      <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
        <Progress done={done} total={run.nodes.length} />
        <Elapsed run={run} />
        <Spend spent={spent} cap={cap} ratio={ratio} />
      </div>

      <Can permission="run_execute">
        <div className="flex items-center gap-2">
          {failed > 0 ? (
            <Tooltip content={refuseReason} wrapDisabled={!mayControl}>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => retry.mutate()}
                disabled={!mayControl || retry.isPending || live}
              >
                {retry.isPending ? <Spinner label="Queueing" /> : <RotateCcw aria-hidden />}
                Re-run {failed} failed
              </Button>
            </Tooltip>
          ) : null}
          {live ? (
            <Tooltip content={refuseReason} wrapDisabled={!mayControl}>
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setConfirmingCancel(true)}
                disabled={!mayControl || cancel.isPending}
              >
                {cancel.isPending ? <Spinner label="Cancelling" /> : <Square aria-hidden />}
                Cancel run
              </Button>
            </Tooltip>
          ) : null}
        </div>
      </Can>

      <ConfirmDialog
        open={confirmingCancel}
        onOpenChange={setConfirmingCancel}
        title="Cancel this run?"
        description="Nodes already finished keep their output. Everything still waiting is skipped."
        body={
          <p className="text-sm text-fg-muted">
            {done} of {run.nodes.length} nodes have finished and {usd(run.cost_usd)} has been spent.
            A cancelled run cannot be resumed — the next one starts over, reusing what it can.
          </p>
        }
        confirmLabel="Cancel run"
        destructive
        onConfirm={() => cancel.mutateAsync().then(() => undefined)}
      />
    </div>
  );
}

function complain(error: unknown, title: string) {
  toast.error(title, {
    description: error instanceof ApiError ? error.detail : "Try again in a moment.",
  });
}

function Progress({ done, total }: { done: number; total: number }) {
  const percent = total === 0 ? 0 : Math.round((done / total) * 100);
  return (
    <div className="flex items-center gap-2.5">
      <div
        role="progressbar"
        aria-valuenow={done}
        aria-valuemin={0}
        aria-valuemax={total}
        aria-label="Nodes finished"
        className="h-1.5 w-28 overflow-hidden rounded-full bg-surface-hover"
      >
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-500 ease-out"
          style={{ width: `${percent}%` }}
        />
      </div>
      <span data-numeric className="text-xs text-fg-muted">
        {done}/{total} nodes
      </span>
    </div>
  );
}

function Elapsed({ run }: { run: RunDetail }) {
  const [, tick] = useState(0);
  const live = isLive(run.status) && run.started_at !== null;

  useEffect(() => {
    if (!live) return;
    const timer = setInterval(() => tick((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, [live]);

  if (!run.started_at) return <span className="text-xs text-fg-subtle">Not started</span>;
  const end = run.finished_at ? Date.parse(run.finished_at) : Date.now();
  const seconds = Math.max(0, Math.round((end - Date.parse(run.started_at)) / 1000));
  const shown = `${Math.floor(seconds / 60)}m ${String(seconds % 60).padStart(2, "0")}s`;
  return (
    <span data-numeric className="text-xs text-fg-muted">
      {shown} elapsed
    </span>
  );
}

function Spend({ spent, cap, ratio }: { spent: number; cap: string | null; ratio: number }) {
  // Amber past four fifths: the cap aborts the run (PRD §7.2), so the warning
  // has to arrive while there is still something a person can do about it.
  const close = ratio >= 0.8;
  return (
    <span className="flex items-center gap-2 text-xs">
      <span data-numeric className={cn(close ? "text-status-gate" : "text-fg-muted")}>
        {usd(spent, spent < 1 ? 4 : 2)}
        {cap ? ` of ${usd(cap)}` : ""}
      </span>
      {cap ? (
        <span aria-hidden className="h-1.5 w-16 overflow-hidden rounded-full bg-surface-hover">
          <span
            className={cn(
              "block h-full rounded-full transition-[width] duration-500 ease-out",
              close ? "bg-status-gate" : "bg-status-success",
            )}
            style={{ width: `${Math.round(ratio * 100)}%` }}
          />
        </span>
      ) : null}
    </span>
  );
}
