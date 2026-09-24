"use client";

import {
  Ban,
  CircleCheck,
  CircleHelp,
  CircleX,
  Clapperboard,
  Clock,
  Image as ImageIcon,
  Loader2,
  RotateCw,
  TimerOff,
  Wallet,
  type LucideIcon,
} from "lucide-react";
import { useEffect, useId, useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  isJobInFlight,
  type GenerationJobItem,
  type GenerationStatus,
} from "@/lib/api/creative-runs";
import { absoluteTime, duration, usd } from "@/lib/format";
import { errorMessage, useCheckGenerationJob, useGenerationJobs } from "@/lib/queries";
import { cn } from "@/lib/utils";

const STATUS: Record<
  GenerationStatus,
  { label: string; icon: LucideIcon; tone: "neutral" | "warning" | "danger"; ink: string; spin?: boolean }
> = {
  queued: { label: "Queued", icon: Clock, tone: "neutral", ink: "text-fg-subtle" },
  submitting: { label: "Submitting", icon: Loader2, tone: "neutral", ink: "text-status-running", spin: true },
  submitted: { label: "Submitted", icon: Loader2, tone: "neutral", ink: "text-status-running", spin: true },
  in_progress: { label: "Rendering", icon: Loader2, tone: "neutral", ink: "text-status-running", spin: true },
  completed: { label: "Completed", icon: CircleCheck, tone: "neutral", ink: "text-status-success" },
  failed: { label: "Failed", icon: CircleX, tone: "danger", ink: "text-status-failed" },
  cancelled: { label: "Cancelled", icon: Ban, tone: "neutral", ink: "text-fg-subtle" },
  expired: { label: "Expired", icon: TimerOff, tone: "warning", ink: "text-status-gate" },
  timed_out: { label: "Timed out", icon: TimerOff, tone: "warning", ink: "text-status-gate" },
  blocked_by_budget: { label: "Blocked by budget", icon: Wallet, tone: "warning", ink: "text-status-gate" },
  unknown_submit_state: {
    label: "Submit state unknown",
    icon: CircleHelp,
    tone: "warning",
    ink: "text-status-gate",
  },
};

/**
 * The Jobs tab (Stage 04 PRD §15.4 C): every `GenerationJob` of the run —
 * model, status, elapsed, polls, estimate beside actual — and `Check again`
 * wherever the server says it would do anything for this caller.
 *
 * `can_check` is the whole rule for showing the control: the route re-checks
 * it and refuses the rest with a 409, so a button here is never one the api
 * would turn down (§15.5 item 4: absent, not disabled). After a check the row
 * says so and the list keeps asking until the worker moves the job on.
 */
export function JobsTab({ runId, selectedNodeId }: { runId: string; selectedNodeId: string }) {
  /** Jobs a check was queued for, with the status they had when it was. */
  const [checking, setChecking] = useState<Record<string, GenerationStatus>>({});
  const jobs = useGenerationJobs(runId, { watching: Object.keys(checking).length > 0 });
  const check = useCheckGenerationJob(runId);

  // A check is over once the worker has moved the job off the status it had.
  const items = jobs.data?.items;
  useEffect(() => {
    if (!items) return;
    setChecking((current) => {
      const next = { ...current };
      for (const job of items) {
        if (next[job.id] !== undefined && next[job.id] !== job.status) delete next[job.id];
      }
      return Object.keys(next).length === Object.keys(current).length ? current : next;
    });
  }, [items]);

  if (jobs.isPending) {
    return (
      <div className="flex flex-col gap-2" aria-busy>
        <Skeleton className="h-12 w-full" />
        <Skeleton className="h-12 w-full" />
      </div>
    );
  }
  if (jobs.isError) {
    return (
      <Alert tone="error" title="The generation jobs could not be loaded">
        {errorMessage(jobs) ?? "Try the refresh button in the console header."}
      </Alert>
    );
  }
  if (jobs.data.items.length === 0) {
    return (
      <EmptyState
        icon={Clapperboard}
        title="No media jobs yet"
        description="Images and video are submitted only after the brief is approved at G7. Each job appears here the moment it is committed, with its estimate, before anything is spent."
      />
    );
  }

  const runCheck = (job: GenerationJobItem) =>
    check.mutate(job.id, {
      onSuccess: (accepted) => {
        setChecking((current) => ({ ...current, [job.id]: job.status }));
        toast.success(
          job.modality === "video" ? "Polling this video again" : "Submitting this image again",
          {
            description: accepted.queued
              ? "The worker has it. This row updates when OpenRouter answers."
              : "A check for this job is already waiting for the worker.",
          },
        );
      },
      onError: (error) => {
        toast.error("Check again did not go through", {
          description: error instanceof ApiError ? error.detail : "Try again in a moment.",
        });
      },
    });

  return (
    // Three columns, not four: the node panel is 420px at its widest, so the
    // action rides under the job it acts on and the model id truncates.
    // `min-w-0` overrides `Table`'s `min-w-max`: at max-content the model id
    // would push Time and Estimate / actual off the panel's right edge.
    <Table label="Generation jobs of this run" className="min-w-0">
      <thead>
        <tr>
          <Th>Job</Th>
          {/* Below `sm` the time rides the job's own line, so the model id
              keeps the width; a phone's panel is ~300px. */}
          <Th className="hidden sm:table-cell">Time</Th>
          <Th className="text-right">Estimate / actual</Th>
        </tr>
      </thead>
      <tbody>
        {jobs.data.items.map((job) => (
          <JobRow
            key={job.id}
            job={job}
            highlighted={job.node_id === selectedNodeId}
            checking={checking[job.id] !== undefined}
            pending={check.isPending && check.variables === job.id}
            onCheck={() => runCheck(job)}
          />
        ))}
      </tbody>
    </Table>
  );
}

function JobRow({
  job,
  highlighted,
  checking,
  pending,
  onCheck,
}: {
  job: GenerationJobItem;
  highlighted: boolean;
  checking: boolean;
  pending: boolean;
  onCheck: () => void;
}) {
  const hintId = useId();
  const state = STATUS[job.status];
  const Modality = job.modality === "video" ? Clapperboard : ImageIcon;
  const start = job.submitted_at ?? job.created_at;
  const end = job.completed_at ?? (isJobInFlight(job.status) ? null : job.updated_at);
  const hint =
    job.modality === "video"
      ? "Resumes polling OpenRouter for this video. Spends nothing new."
      : `Submits this image again under the same key. At most ${usd(job.estimate_usd)}.`;

  return (
    <Tr className={cn(highlighted && "bg-accent-soft hover:bg-accent-soft")}>
      <Td className="w-full max-w-0 align-top">
        <p className="flex min-w-0 items-center gap-1.5">
          <Modality className="size-3.5 shrink-0 text-fg-muted" aria-label={job.modality} />
          <span className="truncate font-mono text-xs text-fg" title={job.model_id}>
            {job.model_id}
          </span>
        </p>
        <p className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-fg-muted">
          <Badge tone={state.tone}>
            <state.icon
              className={cn("size-3", state.ink, state.spin && "motion-safe:animate-spin")}
              aria-hidden
            />
            {state.label}
          </Badge>
          <span className="font-mono">{job.node_id}</span>
          {job.round > 1 ? <span>round {job.round}</span> : null}
          <span data-elapsed className="tabular-nums sm:hidden">
            {duration(start, end)} · {job.polls} {job.polls === 1 ? "poll" : "polls"}
          </span>
        </p>
        {checking ? (
          <p className="mt-2 flex items-center gap-1.5 text-xs text-fg-muted" role="status">
            <Loader2 className="size-3.5 motion-safe:animate-spin" aria-hidden />
            {job.modality === "video" ? "Polling again" : "Submitting again"}
          </p>
        ) : job.can_check ? (
          <div className="mt-2">
            <Button
              variant="secondary"
              size="sm"
              onClick={onCheck}
              disabled={pending}
              title={hint}
              aria-describedby={hintId}
            >
              <RotateCw aria-hidden className={cn(pending && "motion-safe:animate-spin")} />
              Check again
            </Button>
            <span id={hintId} className="sr-only">
              {hint}
            </span>
          </div>
        ) : null}
      </Td>
      <Td className="hidden align-top whitespace-nowrap text-xs text-fg-muted sm:table-cell">
        <p data-elapsed className="tabular-nums" title={`Started ${absoluteTime(start)}`}>
          {duration(start, end)}
        </p>
        <p className="mt-1 tabular-nums">
          {job.polls} {job.polls === 1 ? "poll" : "polls"}
        </p>
      </Td>
      <Td className="align-top whitespace-nowrap text-right text-xs">
        <p className="tabular-nums text-fg-muted">{usd(job.estimate_usd)}</p>
        <p className={cn("mt-1 tabular-nums", job.cost_usd === null ? "text-fg-muted" : "text-fg")}>
          {job.cost_usd === null ? "not billed yet" : usd(job.cost_usd)}
        </p>
      </Td>
    </Tr>
  );
}
