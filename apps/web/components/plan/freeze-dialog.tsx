"use client";

/**
 * Freezing a plan (Stage 02 PRD §15.3 E).
 *
 * This dialog is the last reversible moment in Stage 02. After it, §12.2 and a
 * database trigger agree that the payload, the markdown and the version cannot
 * change — a correction means a new plan run and a new version. So the dialog's
 * job is not to confirm an intent, it is to put the four things somebody would
 * regret not having read in front of them first: who decided each gate, what
 * the critique found, what the envelope commits to, and how big the account
 * this builds actually is.
 *
 * Typing the version is the confirmation, per §15.3 E. It is not friction for
 * its own sake: the number is what the plan will be *called* for the rest of its
 * life, it is the number every later diff and every export footer refers to, and
 * a person who cannot say which version they are freezing is a person who has
 * the wrong plan open. It is also what §16 rule 2 makes the idempotency key, so
 * a double submit answers 200 with the same row instead of minting a second
 * version.
 *
 * The transaction itself belongs to S2-P5b. What arrives back here is a 200 with
 * the frozen plan, or a 409 carrying `blockers[]` — an undecided gate, a
 * blocking critique, a version that moved under us — which is rendered with the
 * same component the eligibility panel uses, because it is the same kind of
 * fact about the same kind of precondition.
 */

import { Snowflake, TriangleAlert } from "lucide-react";
import { useEffect, useState } from "react";

import { EligibilityLock } from "@/components/plan/eligibility-lock";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Spinner } from "@/components/ui/spinner";
import { ApiError } from "@/lib/api";
import type { PlanDetail } from "@/lib/api/plan";
import { absoluteTime, relativeTime, usd } from "@/lib/format";
import { useFreezePlan } from "@/lib/queries";

const GATE_TITLE: Record<string, string> = {
  G1: "Campaign targets",
  G2: "Lead definition",
  G3: "Budget allocation",
  G4: "Channel slate",
};

export function FreezeDialog({
  plan,
  projectId,
  envelopeUsd,
  open,
  onOpenChange,
}: {
  plan: PlanDetail;
  projectId: string;
  envelopeUsd: number | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const freeze = useFreezePlan(plan.plan_run_id, projectId);
  const [typed, setTyped] = useState("");

  // Reopening after a refusal must not start with the last attempt's error
  // still on screen — the reader would be answering a stale objection.
  useEffect(() => {
    if (open) {
      setTyped("");
      freeze.reset();
    }
    // `freeze` is a fresh object every render; only `open` should retrigger this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  const undecided = plan.gates.filter((gate) => gate.status !== "approved");
  const blockingCritique = plan.critique?.blocking ?? [];
  const matches = typed.trim() === String(plan.next_version);
  const refusal = freeze.error instanceof ApiError ? freeze.error : null;
  const blockers = refusal?.problem?.blockers ?? [];

  return (
    <Dialog open={open} onOpenChange={freeze.isPending ? undefined : onOpenChange}>
      <DialogContent
        title={`Freeze this plan as version ${plan.next_version}`}
        description="Freezing is irreversible. The plan becomes the signed-off record, and any change after it means a new version from a new plan run."
        className="max-w-2xl"
      >
        <DialogBody>
          <section className="space-y-1.5">
            <h3 className="text-xs font-medium tracking-wide text-fg-muted uppercase">
              The four decisions this seals
            </h3>
            <ul className="space-y-1">
              {plan.gates.map((gate) => (
                <li
                  key={gate.gate_key}
                  className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 border-b pb-1.5 text-sm last:border-0"
                >
                  <span className="w-7 shrink-0 font-mono text-xs text-fg-subtle">
                    {gate.gate_key}
                  </span>
                  <span className="min-w-32 flex-1 text-fg">
                    {GATE_TITLE[gate.gate_key] ?? gate.node_id}
                  </span>
                  {gate.status === "approved" ? (
                    <span className="text-xs text-fg-muted">
                      {gate.decided_by_name ?? "someone no longer in this workspace"}
                      {gate.decided_at ? (
                        <>
                          {" · "}
                          <time dateTime={gate.decided_at} title={absoluteTime(gate.decided_at)}>
                            {relativeTime(gate.decided_at)}
                          </time>
                        </>
                      ) : null}
                      {gate.edited ? (
                        <span className="text-fg-subtle"> · edited the proposal</span>
                      ) : null}
                    </span>
                  ) : (
                    <span className="text-xs text-status-gate">
                      {gate.status === "not_reached"
                        ? "the run never reached this gate"
                        : gate.status.replace(/_/g, " ")}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </section>

          <section className="space-y-1.5">
            <h3 className="text-xs font-medium tracking-wide text-fg-muted uppercase">
              What the critique found
            </h3>
            {plan.critique?.verdict ? (
              <p className="text-sm text-fg">
                {plan.critique.verdict.replace(/_/g, " ")}
                {plan.critique.advisory.length ? (
                  <span className="text-fg-muted">
                    {" "}
                    · <span data-numeric>{plan.critique.advisory.length}</span> advisory{" "}
                    {plan.critique.advisory.length === 1 ? "note" : "notes"}
                  </span>
                ) : null}
              </p>
            ) : (
              <p className="text-sm text-fg-muted">
                No critique has been recorded for this run. Node 2.6.2 writes one; without it,
                nothing has checked the plan against itself.
              </p>
            )}
            {blockingCritique.length ? (
              <ul className="space-y-1">
                {blockingCritique.map((issue) => (
                  <li key={issue} className="flex gap-2 text-sm text-status-failed">
                    <TriangleAlert aria-hidden className="mt-0.5 size-3.5 shrink-0" />
                    <span>{issue}</span>
                  </li>
                ))}
              </ul>
            ) : null}
          </section>

          <section className="space-y-1.5">
            <h3 className="text-xs font-medium tracking-wide text-fg-muted uppercase">
              What it commits to
            </h3>
            <dl className="flex flex-wrap gap-x-8 gap-y-2 text-sm">
              <Committed label="Monthly envelope">
                {envelopeUsd === null ? "—" : usd(envelopeUsd, 0)}
              </Committed>
              <Committed label="Campaigns">{plan.totals.campaigns}</Committed>
              <Committed label="Ad groups">{plan.totals.ad_groups}</Committed>
              <Committed label="Keywords">{plan.totals.keywords}</Committed>
            </dl>
          </section>

          {undecided.length || blockingCritique.length ? (
            <Alert tone="warning" title="This plan is not ready to freeze">
              {undecided.length
                ? `${undecided.map((gate) => gate.gate_key).join(", ")} ${
                    undecided.length === 1 ? "has" : "have"
                  } not been approved. `
                : ""}
              {blockingCritique.length
                ? `The critique raised ${blockingCritique.length} blocking ${
                    blockingCritique.length === 1 ? "issue" : "issues"
                  }. `
                : ""}
              The server will refuse the freeze, and it is right to.
            </Alert>
          ) : null}

          {blockers.length ? <EligibilityLock blockers={blockers} /> : null}
          {refusal && blockers.length === 0 ? (
            <Alert tone="warning" title="The freeze was refused">
              {refusal.detail}
            </Alert>
          ) : null}

          <Field
            label={`Type ${plan.next_version} to confirm`}
            hint="The version this plan will be known by from now on."
            error={typed && !matches ? `That is not ${plan.next_version}.` : undefined}
            value={typed}
            inputMode="numeric"
            autoComplete="off"
            onChange={(event) => setTyped(event.target.value)}
            disabled={freeze.isPending}
            className="max-w-24"
          />
        </DialogBody>

        <DialogFooter>
          <Button
            variant="secondary"
            onClick={() => onOpenChange(false)}
            disabled={freeze.isPending}
          >
            Cancel
          </Button>
          <Button
            onClick={() => freeze.mutate(plan.next_version)}
            disabled={!matches || freeze.isPending}
          >
            {freeze.isPending ? (
              <Spinner label="Freezing" />
            ) : (
              <Snowflake aria-hidden className="size-3.5" />
            )}
            Freeze version {plan.next_version}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function Committed({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <dt className="text-xs text-fg-muted">{label}</dt>
      <dd data-numeric className="mt-0.5 text-fg">
        {children}
      </dd>
    </div>
  );
}

/**
 * The button, and why it is absent rather than disabled.
 *
 * §15.4 rule 3: "The Freeze button is absent — not merely disabled — for users
 * without `PLAN_FREEZE`." A disabled control tells a viewer that freezing is
 * something they might do once some condition passes, and no condition will.
 * The API enforces it regardless; this only stops the interface making a promise
 * the server will not keep.
 *
 * It *is* disabled — present, greyed, with the reason — when the holder of the
 * permission is looking at a plan that is not ready. That is a state they can
 * change.
 */
export function FreezeButton({
  plan,
  projectId,
  envelopeUsd,
}: {
  plan: PlanDetail;
  projectId: string;
  envelopeUsd: number | null;
}) {
  const [open, setOpen] = useState(false);
  const ready = plan.status === "ready_to_freeze";

  if (plan.status === "frozen") return null;

  return (
    <>
      <Button
        onClick={() => setOpen(true)}
        disabled={!ready}
        title={
          ready
            ? undefined
            : "A plan can be frozen once all four gates are approved and the critique raises nothing blocking."
        }
      >
        <Snowflake aria-hidden className="size-3.5" />
        Freeze plan
      </Button>
      <FreezeDialog
        plan={plan}
        projectId={projectId}
        envelopeUsd={envelopeUsd}
        open={open}
        onOpenChange={setOpen}
      />
    </>
  );
}
