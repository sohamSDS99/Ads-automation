"use client";

import { useQuery } from "@tanstack/react-query";
import { Loader2, TriangleAlert } from "lucide-react";
import { useParams } from "next/navigation";
import { useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import {
  getSignOffMatrix,
  ownerLabel,
  previewMatrixChange,
  putSignOffMatrix,
} from "@/lib/api/governance";
import { keys } from "@/lib/queries";

/**
 * Reassigning the legal owner (PRD §15.3 C.6).
 *
 * The rule this dialog exists to honour: it states the **exact** number of
 * signatures the change will void *before* the confirm control is reachable.
 * Both numbers come from `GET /signoff-matrix/preview` — the same query the
 * write re-runs — because a count the client estimated would eventually
 * disagree with what actually happened, and the one screen where that must
 * never happen is the one that takes somebody's legal signature away.
 *
 * The confirm button stays out of reach until the preview has resolved. A
 * spinner that lets you click through it is a dialog that let somebody void
 * three signatures without being told.
 */
export function ReassignOwnerDialog({
  open,
  onOpenChange,
  currentOwnerName,
  onDone,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  currentOwnerName: string | null;
  onDone: () => void;
}) {
  const params = useParams<{ id: string }>();
  const projectId = params.id;
  const [candidate, setCandidate] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const matrix = useQuery({
    queryKey: keys.signoffMatrix(projectId),
    queryFn: () => getSignOffMatrix(projectId),
    enabled: open,
  });

  const preview = useQuery({
    queryKey: keys.matrixPreview(projectId, candidate),
    queryFn: () => previewMatrixChange(projectId, candidate),
    enabled: open && Boolean(candidate),
  });

  const options = (matrix.data?.eligible_legal_owners ?? []).filter(
    (slot) => slot.user_id !== matrix.data?.matrix?.legal_owner.user_id,
  );
  const cost = preview.data;
  const ready = Boolean(candidate) && preview.isSuccess;
  const needsReason = cost?.reason_required ?? true;

  const submit = async () => {
    const current = matrix.data?.matrix;
    if (!current || !candidate) return;
    setBusy(true);
    setError(null);
    try {
      await putSignOffMatrix(projectId, {
        brand_owner_id: current.brand_owner.user_id,
        legal_owner_id: candidate,
        performance_owner_id: current.performance_owner.user_id,
        reason: reason.trim() || undefined,
      });
      onOpenChange(false);
      onDone();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : "That did not go through.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title="Reassign the legal owner"
        description="An administrator can move this duty. An administrator can never perform it."
      >
        <DialogBody>
          {matrix.isLoading ? (
            <Skeleton className="h-9 w-full" />
          ) : options.length === 0 ? (
            <Alert tone="warning" title="Nobody else can hold this">
              The legal signature is held by `approver` and by no other role, and every eligible
              approver is already named. Invite or promote somebody before reassigning.
            </Alert>
          ) : (
            <>
              <div className="flex flex-col gap-1.5">
                <label htmlFor="new-owner" className="text-sm font-medium text-fg">
                  New legal owner
                </label>
                <Select
                  id="new-owner"
                  value={candidate}
                  onChange={(event) => setCandidate(event.target.value)}
                >
                  <option value="">Choose somebody</option>
                  {options.map((slot) => (
                    <option key={slot.user_id} value={slot.user_id}>
                      {ownerLabel(slot)}
                    </option>
                  ))}
                </Select>
                <p className="text-xs text-fg-subtle">
                  Currently {currentOwnerName ?? "unnamed"}.
                </p>
              </div>

              {/* The consequence, in words and numbers, before the control. */}
              <div aria-live="polite">
                {candidate && preview.isPending ? (
                  <p className="flex items-center gap-2 text-sm text-fg-muted">
                    <Loader2 aria-hidden className="size-4 animate-spin" />
                    Counting what this would void…
                  </p>
                ) : cost && cost.voided_count > 0 ? (
                  <Alert tone="error" title="This voids signatures that already exist">
                    <p data-numeric>
                      <strong className="font-medium">{cost.voided_count}</strong>{" "}
                      {cost.voided_count === 1 ? "signature" : "signatures"} by{" "}
                      {cost.from_legal_owner_name ?? "the current owner"} will be voided.{" "}
                      <strong className="font-medium">{cost.requeued_count}</strong>{" "}
                      {cost.requeued_count === 1 ? "claim returns" : "claims return"} to the
                      queue and must be signed again by {cost.to_legal_owner_name ?? "the new owner"}.
                    </p>
                  </Alert>
                ) : cost ? (
                  <Alert tone="info" title="Nothing is voided">
                    {cost.from_legal_owner_name ?? "The current owner"} has signed nothing on this
                    project yet, so this change costs no signatures.
                  </Alert>
                ) : null}
              </div>

              {candidate ? (
                <div className="flex flex-col gap-1.5">
                  <label htmlFor="reassign-reason" className="text-sm font-medium text-fg">
                    Reason {needsReason ? null : <span className="text-fg-subtle">(optional)</span>}
                  </label>
                  <Textarea
                    id="reassign-reason"
                    rows={3}
                    value={reason}
                    onChange={(event) => setReason(event.target.value)}
                    placeholder="Why the liability is moving. This is recorded and cannot be edited."
                  />
                </div>
              ) : null}

              {error ? (
                <p className="flex items-start gap-1.5 text-sm text-status-failed">
                  <TriangleAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
                  {error}
                </p>
              ) : null}
            </>
          )}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button
            // Unreachable until the cost is known and, when required, stated.
            disabled={!ready || busy || (needsReason && !reason.trim())}
            onClick={() => void submit()}
          >
            {busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : null}
            {cost && cost.voided_count > 0
              ? `Reassign and void ${cost.voided_count}`
              : "Reassign"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
