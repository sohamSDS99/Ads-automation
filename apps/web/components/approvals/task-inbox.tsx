"use client";

import { ClipboardCheck } from "lucide-react";

import { TaskCard } from "@/components/tasks/task-card";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { errorMessage, useHumanTasks, APPROVAL_POLL_MS } from "@/lib/queries";

/**
 * The `Signatures & attestations` tab (PRD §15.2).
 *
 * Shows the caller's own person-tasks. Open ones first, then the settled ones
 * beneath, because a completed attestation is the receipt somebody comes back
 * looking for and hiding it would send them to the audit log.
 */
export function TaskInbox() {
  const tasks = useHumanTasks({ mine: true }, { pollMs: APPROVAL_POLL_MS });

  if (tasks.isLoading) {
    return (
      <div className="space-y-3" aria-busy>
        <Skeleton className="h-40 w-full" />
        <Skeleton className="h-40 w-full" />
      </div>
    );
  }

  if (tasks.isError) {
    return (
      <Alert tone="error" title="Your tasks could not be read">
        {errorMessage(tasks)}
      </Alert>
    );
  }

  const items = tasks.data?.items ?? [];
  const open = items.filter((task) => !["completed", "not_required"].includes(task.status));
  const settled = items.filter((task) => ["completed", "not_required"].includes(task.status));

  if (items.length === 0) {
    return (
      <EmptyState
        icon={ClipboardCheck}
        title="Nothing is waiting on you personally"
        description="Person-tasks are the acts the agent cannot perform for anybody — signing a claim set, attesting to a verification. They appear here when one is routed to your name."
      />
    );
  }

  return (
    <div className="space-y-6">
      {open.length > 0 ? (
        <section className="space-y-3">
          {open.map((task) => (
            <TaskCard key={task.id} task={task} onDone={() => void tasks.refetch()} />
          ))}
        </section>
      ) : (
        <p className="text-sm text-fg-muted">
          Nothing open. Your completed attestations are below.
        </p>
      )}

      {settled.length > 0 ? (
        <section className="space-y-3">
          <h3 className="text-xs font-medium text-fg-subtle">Settled</h3>
          {settled.map((task) => (
            <TaskCard key={task.id} task={task} onDone={() => void tasks.refetch()} />
          ))}
        </section>
      ) : null}
    </div>
  );
}
