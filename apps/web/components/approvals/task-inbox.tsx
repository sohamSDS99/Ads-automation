"use client";

import { ClipboardCheck } from "lucide-react";

import { H3TaskCard } from "@/components/creative/h3-exceptions";
import { TaskCard } from "@/components/tasks/task-card";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import type { HumanTask } from "@/lib/api/tasks";
import { errorMessage, useHumanTasks, APPROVAL_POLL_MS } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * The `Signatures & attestations` tab (PRD §15.2).
 *
 * Shows the caller's own person-tasks. Open ones first, then the settled ones
 * beneath, because a completed attestation is the receipt somebody comes back
 * looking for and hiding it would send them to the audit log.
 *
 * Stage 04's H3 (§15.4 I) lives here too — no new inbox. The legal owner gets
 * it among their own tasks, decided row by row. Everyone else sees the open
 * H3s waiting on somebody else, reading `Awaiting {legal owner}` with every
 * decide control absent; a `creative_execute` holder can withdraw them.
 */
export function TaskInbox() {
  const { user } = useSession();
  const tasks = useHumanTasks({ mine: true }, { pollMs: APPROVAL_POLL_MS });
  const all = useHumanTasks({ status: "open" }, { pollMs: APPROVAL_POLL_MS });
  const refetch = () => {
    void tasks.refetch();
    void all.refetch();
  };
  const waiting = (all.data?.items ?? []).filter((task) => task.task_key === "H3" && task.assignee_id !== user.id);

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

  if (items.length === 0 && waiting.length === 0) {
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
            <Card key={task.id} task={task} onDone={refetch} />
          ))}
        </section>
      ) : (
        <p className="text-sm text-fg-muted">
          {settled.length > 0
            ? "Nothing open. Your completed attestations are below."
            : "Nothing is waiting on you personally."}
        </p>
      )}

      {settled.length > 0 ? (
        <section className="space-y-3">
          <h3 className="text-xs font-medium text-fg-subtle">Settled</h3>
          {settled.map((task) => (
            <Card key={task.id} task={task} onDone={refetch} />
          ))}
        </section>
      ) : null}

      {waiting.length > 0 ? (
        <section className="space-y-3" aria-labelledby="h3-waiting-title">
          <h3 id="h3-waiting-title" className="text-xs font-medium text-fg-subtle">
            Legal exceptions waiting on someone else
          </h3>
          {waiting.map((task) => (
            <H3TaskCard key={task.id} task={task} onDone={refetch} />
          ))}
        </section>
      ) : null}
    </div>
  );
}

/** H3 is decided as a set of exceptions, not attested as a checklist. */
function Card({ task, onDone }: { task: HumanTask; onDone: () => void }) {
  return task.task_key === "H3" ? <H3TaskCard task={task} onDone={onDone} /> : <TaskCard task={task} onDone={onDone} />;
}
