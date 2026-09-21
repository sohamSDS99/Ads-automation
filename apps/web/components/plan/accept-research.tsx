"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleCheck, RotateCcw } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { acceptResearch, withdrawAcceptance } from "@/lib/api/plan";
import type { LaunchReadiness } from "@/lib/api/reports";
import { keys, usePlanEligibility } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * "Accept research" — the first of the two manual steps between the stages.
 *
 * Not a fourth gate. The three research gates are decided by the time a report
 * exists; this records that a person read the finished thing and considers it
 * fit to plan from (Stage 02 PRD §4.1), which is why it reuses
 * `approval_decide` rather than inventing a permission.
 *
 * A `no_go` verdict turns the dialog into an override: admin only, and the
 * reason is required because it is printed on the plan's cover page and
 * written to the audit log. The button is not hidden from a non-admin — the
 * server enforces this, and a control that vanishes teaches nobody why.
 */
export function AcceptResearch({
  projectId,
  runId,
  readiness,
}: {
  projectId: string;
  runId: string;
  readiness: LaunchReadiness;
}) {
  const [open, setOpen] = useState(false);
  const [note, setNote] = useState("");
  const router = useRouter();
  const queryClient = useQueryClient();
  const session = useSession();
  const eligibility = usePlanEligibility(projectId);

  const isAdmin = session.user.role === "admin";
  const noGo = readiness === "no_go";
  const source = eligibility.data?.source;
  const acceptedThisRun = source?.research_run_id === runId;

  async function refresh() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: keys.planEligibility(projectId) }),
      queryClient.invalidateQueries({ queryKey: keys.plans(projectId) }),
    ]);
  }

  const accept = useMutation({
    mutationFn: () =>
      acceptResearch(runId, noGo ? { override_reason: note.trim() } : { note: note.trim() || null }),
    onSuccess: async () => {
      setOpen(false);
      setNote("");
      await refresh();
      toast.success("Research accepted", {
        description: "Campaign planning is unlocked for this project.",
        action: {
          label: "Open stage 02",
          onClick: () => router.push(`/projects/${projectId}/plan`),
        },
      });
    },
    onError: (error) =>
      toast.error("The report was not accepted", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  const withdraw = useMutation({
    mutationFn: () => withdrawAcceptance(runId),
    onSuccess: async () => {
      await refresh();
      toast.success("Acceptance withdrawn", {
        description: "Campaign planning is locked again until a report is accepted.",
      });
    },
    onError: (error) =>
      toast.error("The acceptance could not be withdrawn", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      }),
  });

  if (acceptedThisRun) {
    return (
      <Button
        variant="secondary"
        onClick={() => withdraw.mutate()}
        disabled={withdraw.isPending}
        title="Lock campaign planning again"
      >
        {withdraw.isPending ? <Spinner label="Withdrawing" /> : <RotateCcw aria-hidden />}
        Withdraw acceptance
      </Button>
    );
  }

  const blocked = noGo && !isAdmin;
  const reasonMissing = noGo && note.trim().length === 0;

  return (
    <>
      <Button variant="secondary" onClick={() => setOpen(true)}>
        <CircleCheck aria-hidden />
        Accept research
      </Button>

      <Dialog open={open} onOpenChange={accept.isPending ? undefined : setOpen}>
        <DialogContent
          title={noGo ? "Accept research over a no_go verdict" : "Accept this research"}
          description={
            noGo
              ? "This report says the account is not ready to launch."
              : "Records that you read the finished report and consider it fit to plan from."
          }
        >
          <DialogBody className="space-y-4">
            {blocked ? (
              <p className="text-sm text-fg">
                Only an administrator can accept research that says{" "}
                <span className="font-mono text-xs">no_go</span>, and only with a written reason.
                Ask an admin to review it, or fix what the verdict names and run research again.
              </p>
            ) : (
              <>
                {noGo ? (
                  <p className="text-sm text-fg-muted">
                    Your reason is stored on the acceptance, printed on the plan&rsquo;s cover page
                    and written to the audit log. Write it for the person who reads the plan in six
                    months.
                  </p>
                ) : null}
                <label className="flex flex-col gap-1.5">
                  <span className="text-sm font-medium text-fg">
                    {noGo ? "Why you are overriding the verdict" : "Note (optional)"}
                  </span>
                  <Textarea
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    maxLength={2000}
                    invalid={noGo && note.length > 0 && reasonMissing}
                    placeholder={
                      noGo
                        ? "e.g. The CFO signed off on the tracking gap in writing on 12 March; we launch brand-only until it is fixed."
                        : "Anything the person planning from this should know."
                    }
                  />
                </label>
              </>
            )}
          </DialogBody>
          <DialogFooter>
            <Button variant="secondary" onClick={() => setOpen(false)} disabled={accept.isPending}>
              {blocked ? "Close" : "Cancel"}
            </Button>
            {blocked ? null : (
              <Button
                onClick={() => accept.mutate()}
                disabled={accept.isPending || reasonMissing}
                title={reasonMissing ? "A written reason is required" : undefined}
              >
                {accept.isPending ? <Spinner label="Accepting" /> : <CircleCheck aria-hidden />}
                {noGo ? "Override and accept" : "Accept research"}
              </Button>
            )}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
