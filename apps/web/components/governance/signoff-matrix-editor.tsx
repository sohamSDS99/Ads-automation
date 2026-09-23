"use client";

import { useQuery } from "@tanstack/react-query";
import { Loader2, ShieldAlert } from "lucide-react";
import { useEffect, useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import {
  getSignOffMatrix,
  ownerLabel,
  previewMatrixChange,
  putSignOffMatrix,
  type OwnerSlot,
} from "@/lib/api/governance";
import { absoluteTime } from "@/lib/format";
import { keys } from "@/lib/queries";

/**
 * The sign-off matrix editor (PRD §21, S3-P8 scope).
 *
 * Three slots, one of which is different in kind. Brand and performance
 * ownership are assignments. **Legal ownership is a liability**, and moving it
 * voids every signature the outgoing owner made — so this screen's real job is
 * to make that cost visible before it is paid, in numbers the server counted.
 *
 * Two deliberate asymmetries:
 *
 * * a change that voids nothing saves without ceremony, because a confirmation
 *   on a harmless action is what trains people to click through the dangerous
 *   one;
 * * the legal-owner list holds only `approver`s, because law 23 means an
 *   administrator can never hold that signature and offering one would be
 *   offering an impossibility.
 */
export function SignOffMatrixEditor({ projectId }: { projectId: string }) {
  const state = useQuery({
    queryKey: keys.signoffMatrix(projectId),
    queryFn: () => getSignOffMatrix(projectId),
  });

  const [brand, setBrand] = useState("");
  const [legal, setLegal] = useState("");
  const [performance, setPerformance] = useState("");
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const matrix = state.data?.matrix ?? null;

  useEffect(() => {
    if (!matrix) return;
    setBrand(matrix.brand_owner.user_id);
    setLegal(matrix.legal_owner.user_id);
    setPerformance(matrix.performance_owner.user_id);
  }, [matrix]);

  const legalChanged = Boolean(matrix) && legal !== matrix?.legal_owner.user_id;

  const preview = useQuery({
    queryKey: keys.matrixPreview(projectId, legal),
    queryFn: () => previewMatrixChange(projectId, legal),
    enabled: Boolean(legal) && legalChanged,
  });

  const cost = legalChanged ? preview.data : null;
  const dirty =
    !matrix ||
    brand !== matrix.brand_owner.user_id ||
    legal !== matrix.legal_owner.user_id ||
    performance !== matrix.performance_owner.user_id;

  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await putSignOffMatrix(projectId, {
        brand_owner_id: brand,
        legal_owner_id: legal,
        performance_owner_id: performance,
        reason: reason.trim() || undefined,
      });
      setReason("");
      await state.refetch();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : "That did not save.");
    } finally {
      setBusy(false);
    }
  };

  if (state.isLoading) return <Skeleton className="h-64 w-full" />;
  if (state.isError) {
    return (
      <Alert tone="error" title="The sign-off matrix could not be read">
        {state.error instanceof ApiError ? state.error.detail : "Try again."}
      </Alert>
    );
  }

  const all = state.data?.eligible_owners ?? [];
  const signers = state.data?.eligible_legal_owners ?? [];
  const canEdit = state.data?.can_edit ?? false;

  return (
    <section className="rounded-[var(--radius)] border bg-surface-raised">
      <header className="border-b px-5 py-4">
        <h2 className="text-[length:var(--text-md)] font-medium text-fg">Sign-off matrix</h2>
        <p className="mt-0.5 text-sm text-fg-muted">
          {matrix
            ? `Version ${matrix.version}, set by ${matrix.set_by_name ?? "somebody"} on ${absoluteTime(matrix.set_at)}.`
            : "This project has no matrix yet. G6 decides one, or you can set it here."}
        </p>
      </header>

      <div className="space-y-4 px-5 py-4">
        <Slot
          label="Brand owner"
          hint="Approves the voice, the lexicon and the visual identity rules."
          value={brand}
          options={all}
          disabled={!canEdit}
          onChange={setBrand}
        />
        <Slot
          label="Legal owner"
          hint="The one identity the claim signature is narrowed to. Only an approver can hold it, and an administrator never can."
          value={legal}
          options={signers}
          disabled={!canEdit}
          onChange={setLegal}
        />
        <Slot
          label="Performance owner"
          hint="Answers for what the rules cost in reach and conversion."
          value={performance}
          options={all}
          disabled={!canEdit}
          onChange={setPerformance}
        />

        {/* The consequence, stated before the save control, in the server's
            numbers. `aria-live` because the figure changes as the select does. */}
        <div aria-live="polite">
          {legalChanged && preview.isPending ? (
            <p className="flex items-center gap-2 text-sm text-fg-muted">
              <Loader2 aria-hidden className="size-4 animate-spin" />
              Counting what this would void…
            </p>
          ) : cost && cost.voided_count > 0 ? (
            <Alert tone="error" title="Changing the legal owner voids signatures">
              <p data-numeric>
                {ownerLabel(matrix?.legal_owner)} → {cost.to_legal_owner_name ?? "the new owner"}.{" "}
                <strong className="font-medium">{cost.voided_count}</strong>{" "}
                {cost.voided_count === 1 ? "signature is" : "signatures are"} voided and{" "}
                <strong className="font-medium">{cost.requeued_count}</strong>{" "}
                {cost.requeued_count === 1 ? "claim returns" : "claims return"} to the queue.
              </p>
            </Alert>
          ) : cost ? (
            <Alert tone="info" title="Nothing is voided">
              {ownerLabel(matrix?.legal_owner)} → {cost.to_legal_owner_name ?? "the new owner"}.
              No signature exists yet, so this change costs nothing.
            </Alert>
          ) : null}
        </div>

        {legalChanged ? (
          <div className="flex flex-col gap-1.5">
            <label htmlFor="matrix-reason" className="text-sm font-medium text-fg">
              Reason for the change
            </label>
            <Textarea
              id="matrix-reason"
              rows={2}
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Recorded in the audit log and cannot be edited."
            />
          </div>
        ) : null}

        {error ? (
          <p className="flex items-start gap-1.5 text-sm text-status-failed" aria-live="polite">
            <ShieldAlert aria-hidden className="mt-0.5 size-4 shrink-0" />
            {error}
          </p>
        ) : null}
      </div>

      {canEdit ? (
        <footer className="flex items-center justify-end gap-2 border-t px-5 py-3.5">
          <Button
            disabled={
              !dirty ||
              busy ||
              !brand ||
              !legal ||
              !performance ||
              // Unreachable until the cost is known, and until a reason is given
              // when the legal owner is the thing moving.
              (legalChanged && (preview.isPending || !reason.trim()))
            }
            onClick={() => void save()}
          >
            {busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : null}
            {cost && cost.voided_count > 0
              ? `Save and void ${cost.voided_count}`
              : "Save matrix"}
          </Button>
        </footer>
      ) : null}
    </section>
  );
}

function Slot({
  label,
  hint,
  value,
  options,
  disabled,
  onChange,
}: {
  label: string;
  hint: string;
  value: string;
  options: OwnerSlot[];
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <div className="grid gap-1.5 sm:grid-cols-[minmax(0,14rem)_minmax(0,1fr)] sm:items-start sm:gap-4">
      <div>
        <label className="text-sm font-medium text-fg">{label}</label>
        <p className="mt-0.5 text-xs text-fg-subtle">{hint}</p>
      </div>
      <Select
        aria-label={label}
        value={value}
        disabled={disabled}
        onChange={(event) => onChange(event.target.value)}
      >
        <option value="">Nobody</option>
        {options.map((slot) => (
          <option key={slot.user_id} value={slot.user_id}>
            {ownerLabel(slot)}
          </option>
        ))}
      </Select>
    </div>
  );
}
