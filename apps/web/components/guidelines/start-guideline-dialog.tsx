"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Play } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { BindingPicker } from "@/components/guidelines/binding-picker";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  startGuidelineRun,
  type AvailableBindings,
  type GuidelineBindings,
} from "@/lib/api/guidelines";
import { keys } from "@/lib/queries";
import { Can } from "@/lib/session";

/**
 * Start a content guidelines run (Stage 03 PRD §15.3 A, §23 deliverable 12).
 *
 * A dialog rather than a bare button, but for the opposite reason to Stage
 * 02's. There the dialog exists to confirm *which* research the plan consumes,
 * because a plan cannot exist without one. Here it exists to offer bindings a
 * person may decline — so the primary action is enabled from the moment the
 * dialog opens, with every box clear.
 *
 * A started run goes to the Guideline Console at
 * `/projects/{id}/guidelines/runs/{runId}`. S3-P0 routed to the stage-agnostic
 * `/projects/{id}/runs/{runId}` because the Guideline Console did not exist
 * yet and routing to it would have landed a person who pressed Start on a 404.
 * S3-P7 built it, so that reason has expired — and the guideline route is the
 * one with the Rules tab and the link to the draft rulebook.
 *
 * The button is wrapped in `GUIDELINE_EXECUTE`, so a `viewer` and an
 * `approver` see the landing's four blocks without it (§15.3 A). Absent, not
 * disabled: a greyed button invites a person to work out what would enable it,
 * and on this stage the answer is "a different role", which is not something
 * they can fix.
 *
 * `mode` is never sent. What the caller asks to bind and what actually
 * resolves are different things, and the server derives the second (§4.5 rule
 * 3). When they differ the 202 says so, and the toast repeats it rather than
 * letting a person discover on the rulebook header that their binding was
 * dropped.
 */
export function StartGuidelineDialog({
  projectId,
  available,
  disabled,
}: {
  projectId: string;
  available: AvailableBindings;
  disabled?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [bindings, setBindings] = useState<GuidelineBindings>({});
  const router = useRouter();
  const queryClient = useQueryClient();

  const start = useMutation({
    mutationFn: () => startGuidelineRun(projectId, bindings),
    onSuccess: async (run) => {
      setOpen(false);
      await queryClient.invalidateQueries({ queryKey: keys.guidelineEligibility(projectId) });
      await queryClient.invalidateQueries({ queryKey: keys.guidelines(projectId) });

      // A binding that was asked for and did not resolve is the one outcome a
      // person would otherwise only notice much later, on the rulebook header.
      const requested = Object.values(bindings).filter(Boolean).length > 0;
      const dropped = requested && run.mode === "standalone";
      toast.success("Content guidelines started", {
        description: dropped
          ? "The bindings could not be resolved, so the run is unscoped and covers every campaign type."
          : "The guideline console shows it as it runs.",
      });
      router.push(`/projects/${projectId}/guidelines/runs/${run.run_id}`);
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        const holder = (error.problem as { holder?: { run_id?: string } } | null)?.holder;
        void queryClient.invalidateQueries({ queryKey: keys.guidelineEligibility(projectId) });
        toast.error("A guideline run is already in flight", {
          description: error.detail,
          action: holder?.run_id
            ? {
                label: "Open it",
                onClick: () =>
                  router.push(`/projects/${projectId}/guidelines/runs/${holder.run_id}`),
              }
            : undefined,
        });
        setOpen(false);
        return;
      }
      toast.error("Content guidelines could not start", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      });
    },
  });

  return (
    <>
      <Can permission="guideline_execute">
        <Button onClick={() => setOpen(true)} disabled={disabled}>
          <Play className="size-4" aria-hidden />
          Start content guidelines
        </Button>
      </Can>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent title="Start content guidelines">
          <DialogBody className="flex flex-col gap-4">
            <p className="text-sm text-fg-muted">
              Profiles the brand voice, harvests every claim the site and the ad account already
              make, maps the Google policies that apply, and compiles the lot into a rulebook.
              Nothing is written to the Google Ads account.
            </p>
            <BindingPicker
              available={available}
              value={bindings}
              onChange={setBindings}
              disabled={start.isPending}
            />
          </DialogBody>
          <DialogFooter>
            <Button variant="ghost" onClick={() => setOpen(false)} disabled={start.isPending}>
              Cancel
            </Button>
            <Button onClick={() => start.mutate()} disabled={start.isPending}>
              {start.isPending ? <Spinner className="size-4" /> : <Play className="size-4" aria-hidden />}
              Start
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
