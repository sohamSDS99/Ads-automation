"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { startPlanRun, type AcceptedSource } from "@/lib/api/plan";
import { absoluteTime, relativeTime } from "@/lib/format";
import { keys } from "@/lib/queries";

/**
 * Start campaign planning, after confirming what it will be planned from.
 *
 * A dialog rather than a bare button, for one reason: the plan is built from
 * *a particular research run*, and a project can hold several. Showing which
 * one — and who accepted it, and how old it is — is the difference between a
 * deliberate act and a click. That is also why the confirm button repeats the
 * run it is about to consume rather than saying "Confirm".
 *
 * The 409 is not preventable in advance. Two people can press this in the same
 * second; the loser is offered the run that beat them (Stage 02 PRD §4.2 E4).
 */
export function StartPlanDialog({
  projectId,
  source,
  disabled,
}: {
  projectId: string;
  source: AcceptedSource;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const router = useRouter();
  const queryClient = useQueryClient();

  const start = useMutation({
    mutationFn: () => startPlanRun(projectId),
    onSuccess: async (run) => {
      setOpen(false);
      await queryClient.invalidateQueries({ queryKey: keys.planEligibility(projectId) });
      toast.success("Campaign planning started", {
        description: "The plan console shows it as it runs.",
      });
      router.push(`/projects/${projectId}/plan/runs/${run.run_id}`);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        const holder = (error.problem as { holder?: { run_id?: string } } | null)?.holder;
        void queryClient.invalidateQueries({ queryKey: keys.planEligibility(projectId) });
        toast.error("A plan run is already in flight", {
          description: error.detail,
          action: holder?.run_id
            ? {
                label: "Open it",
                onClick: () =>
                  router.push(`/projects/${projectId}/plan/runs/${holder.run_id}`),
              }
            : undefined,
        });
        setOpen(false);
        return;
      }
      toast.error("Campaign planning could not start", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      });
    },
  });

  return (
    <>
      <Button onClick={() => setOpen(true)} disabled={disabled}>
        <Play aria-hidden />
        Start campaign planning
      </Button>

      <Dialog open={open} onOpenChange={start.isPending ? undefined : setOpen}>
        <DialogContent
          title="Start campaign planning"
          description="The plan is built from one accepted research report and nothing else."
        >
          <DialogBody className="space-y-4">
            <dl className="grid gap-x-6 gap-y-3 text-sm sm:grid-cols-[auto_1fr]">
              <dt className="text-fg-subtle">Research run</dt>
              <dd className="font-mono text-xs text-fg">{source.research_run_id}</dd>
              <dt className="text-fg-subtle">Accepted by</dt>
              <dd className="text-fg">
                {source.accepted_by_name}
                {" · "}
                <time dateTime={source.accepted_at} title={absoluteTime(source.accepted_at)}>
                  {relativeTime(source.accepted_at)}
                </time>
              </dd>
              <dt className="text-fg-subtle">Verdict</dt>
              <dd className="text-fg">{READINESS_LABEL[source.launch_readiness]}</dd>
            </dl>

            {source.degraded_sources.length > 0 ? (
              <p className="text-sm text-fg-muted">
                {source.degraded_sources.join(", ")} degraded during that run. The forecast this
                plan produces will be no more confident than the demand data behind it.
              </p>
            ) : null}
            {source.override_reason ? (
              <p className="text-sm text-fg-muted">
                Accepted over a <span className="text-fg">no_go</span> verdict:{" "}
                {source.override_reason}
              </p>
            ) : null}

            <p className="text-sm text-fg-muted">
              Nothing is written to the Google Ads account. Four decisions will stop for a human
              yes: the campaign targets, the lead definition, the budget split and the channel
              slate.
            </p>
          </DialogBody>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setOpen(false)} disabled={start.isPending}>
              Cancel
            </Button>
            <Button onClick={() => start.mutate()} disabled={start.isPending}>
              {start.isPending ? <Spinner label="Starting" /> : <Play aria-hidden />}
              Plan from this research
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

const READINESS_LABEL: Record<AcceptedSource["launch_readiness"], string> = {
  go: "Go",
  go_with_fixes: "Go, with fixes",
  no_go: "No go",
};
