"use client";

import { RotateCw } from "lucide-react";
import { useState } from "react";

import { STATUS } from "@/components/creative/jobs-tab";
import { MonoId } from "@/components/creative/mono-id";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { isJobInFlight, type GenerationJobItem, type GenerationStatus } from "@/lib/api/creative-runs";
import { absoluteTime, duration, usd } from "@/lib/format";
import { useCheckGenerationJob } from "@/lib/queries";
import { cn } from "@/lib/utils";

/**
 * "Generating 6 of 12 images · $1.84 so far" (§15.2 rule 10) — counted from
 * the job rows, the money from what OpenRouter billed.
 */
export function jobSummary(jobs: GenerationJobItem[]): string {
  if (jobs.length === 0) return "No media job has been submitted for this run.";
  const billed = jobs.reduce((sum, job) => sum + (job.cost_usd ? Number(job.cost_usd) : 0), 0);
  const estimated = jobs.reduce((sum, job) => sum + Number(job.estimate_usd), 0);
  const live = jobs.filter((job) => isJobInFlight(job.status));
  const noun = (modality: "image" | "video", n: number) =>
    modality === "image" ? (n === 1 ? "image" : "images") : n === 1 ? "video clip" : "video clips";
  if (live.length > 0) {
    const modality = live.every((job) => job.modality === "video") ? "video" : "image";
    const scope = jobs.filter((job) => job.modality === modality);
    const done = scope.filter((job) => !isJobInFlight(job.status)).length;
    return `Generating ${done + 1} of ${scope.length} ${noun(modality, scope.length)} · ${usd(billed)} so far`;
  }
  return `${jobs.length} ${jobs.length === 1 ? "job" : "jobs"} · ${usd(billed)} billed of ${usd(estimated)} estimated`;
}

/**
 * `JobStatusList` (Stage 04 PRD §15.4 G): every generation job of the run —
 * which asset it paints, its model, status, round, elapsed time, polls and
 * the estimate beside what was billed — with `Check again` wherever the
 * server says it would do anything for this caller (`can_check`: absent,
 * never disabled, for anyone else; §15.5 item 4).
 */
export function JobStatusList({
  runId,
  jobs,
  assetLabel,
  onOpenAsset,
}: {
  runId: string;
  jobs: GenerationJobItem[];
  /** What the library calls an asset: its concept or campaign, and kind. */
  assetLabel: (assetId: string) => string | null;
  onOpenAsset?: (assetId: string) => void;
}) {
  const check = useCheckGenerationJob(runId);
  const [checking, setChecking] = useState<Record<string, GenerationStatus>>({});

  const onCheck = (job: GenerationJobItem) => {
    check.mutate(job.id, {
      onSuccess: (accepted) => {
        setChecking((now) => ({ ...now, [job.id]: job.status }));
        toast.success(
          accepted.queued ? "Checking this job again." : "A check of this job is already waiting for the worker.",
        );
      },
      onError: (error) => {
        toast.error(error instanceof ApiError ? (error.problem?.detail ?? error.message) : "The check could not be queued.");
      },
    });
  };

  return (
    <section className="flex min-w-0 flex-col gap-3" aria-labelledby="job-list-title" data-testid="job-status-list">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="job-list-title" className="text-md font-semibold text-fg">
          Generation jobs
        </h2>
        <p className="text-sm tabular-nums text-fg-muted" role="status">
          {jobSummary(jobs)}
        </p>
      </div>
      {jobs.length === 0 ? null : (
        <Table label="Generation jobs of this run">
          <thead>
            <Tr>
              <Th>Job</Th>
              <Th>Asset</Th>
              <Th>Model</Th>
              <Th>Status</Th>
              <Th className="text-right">Round</Th>
              <Th className="text-right">Elapsed</Th>
              <Th className="text-right">Polls</Th>
              <Th className="text-right">Estimate · billed</Th>
              <Th>
                <span className="sr-only">Actions</span>
              </Th>
            </Tr>
          </thead>
          <tbody>
            {jobs.map((job) => {
              const state = STATUS[job.status];
              const Icon = state.icon;
              const waiting = checking[job.id] === job.status;
              const label = job.asset_id ? assetLabel(job.asset_id) : null;
              return (
                <Tr key={job.id} data-job={job.id} data-status={job.status}>
                  <Td>
                    <MonoId value={job.id} label="Job id" />
                  </Td>
                  <Td>
                    {/* A fixed width that truncates: the shared Table is
                        `min-w-max`, so a cell's max-width alone lets the text
                        run into the next column. */}
                    {job.asset_id && onOpenAsset ? (
                      <button
                        type="button"
                        onClick={() => onOpenAsset(job.asset_id as string)}
                        title={label ?? undefined}
                        className="block w-48 truncate text-left text-accent underline-offset-2 hover:underline"
                      >
                        {label ?? `${job.modality} asset`}
                      </button>
                    ) : (
                      <span className="block w-48 truncate text-fg-muted">{label ?? "—"}</span>
                    )}
                    <span className="block font-mono text-xs text-fg-subtle">{job.node_id}</span>
                  </Td>
                  <Td>
                    <span
                      className="block w-56 truncate font-mono text-xs text-fg"
                      title={`${job.model_id} · ${job.provider_tag ?? "any provider"}`}
                    >
                      {job.model_id}
                    </span>
                  </Td>
                  <Td>
                    <Badge tone={state.tone} data-status={job.status} className="whitespace-nowrap">
                      <Icon
                        className={cn("size-3", state.ink, state.spin && "motion-safe:animate-spin")}
                        aria-hidden
                      />
                      {waiting ? "Check queued" : state.label}
                    </Badge>
                    {job.error && typeof job.error["detail"] === "string" ? (
                      <span className="mt-1 block max-w-64 text-xs text-fg-muted">{job.error["detail"]}</span>
                    ) : null}
                  </Td>
                  <Td className="text-right tabular-nums">{job.round}</Td>
                  <Td className="text-right tabular-nums" title={absoluteTime(job.submitted_at ?? job.created_at)}>
                    {duration(job.submitted_at ?? job.created_at, job.completed_at ?? null)}
                  </Td>
                  <Td className="text-right tabular-nums">{job.polls}</Td>
                  <Td className="whitespace-nowrap text-right tabular-nums">
                    {usd(job.estimate_usd)} · {job.cost_usd ? usd(job.cost_usd) : "not billed yet"}
                  </Td>
                  <Td>
                    {job.can_check ? (
                      <Button
                        size="sm"
                        variant="secondary"
                        onClick={() => onCheck(job)}
                        disabled={waiting || (check.isPending && check.variables === job.id)}
                      >
                        <RotateCw className="size-3.5" aria-hidden />
                        Check again
                      </Button>
                    ) : null}
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      )}
    </section>
  );
}
