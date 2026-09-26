"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowRight, Download, Scale, ShieldCheck, Undo2 } from "lucide-react";
import { useMemo, useState } from "react";

import { StepUpCeremony } from "@/components/claims/step-up-dialog";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { SegmentedControl } from "@/components/ui/segmented";
import { Skeleton } from "@/components/ui/skeleton";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import type { CreativeAssetItem } from "@/lib/api/creative-runs";
import { listEvidence, evidenceSummary, SOURCE_LABEL } from "@/lib/api/evidence";
import {
  clearCreativeExceptions,
  H3_STATEMENT,
  KIND_LABEL,
  openExceptions,
  previewWithdrawExceptions,
  withdrawConsequence,
  withdrawCreativeExceptions,
  type ClearReceipt,
  type CreativeExceptionItem,
  type ExceptionDecision,
  type Swapped,
} from "@/lib/api/exceptions";
import { mediaContentUrl, mediaReferenceContentUrl } from "@/lib/api/media-library";
import type { HumanTask } from "@/lib/api/tasks";
import { absoluteTime } from "@/lib/format";
import { errorMessage, keys, useCreativeAssets, useCreativeExceptions } from "@/lib/queries";
import { useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

type RowDecision = { decision: ExceptionDecision | null; note: string };

const DECISION_OPTIONS: { value: ExceptionDecision; label: string }[] = [
  { value: "cleared", label: "Clear" },
  { value: "rejected", label: "Reject" },
];

/** The receipt a completed H3 task already holds — `exceptions/clear` stored it. */
function storedReceipt(task: HumanTask): ClearReceipt | null {
  const stored = task.submitted_payload;
  if (!stored || typeof stored["decided_hash"] !== "string") return null;
  const list = (key: string) => (Array.isArray(stored[key]) ? (stored[key] as string[]) : []);
  return {
    run_id: task.guideline_run_id ?? "",
    set_hash: stored["decided_hash"] as string,
    register_hash: typeof stored["register_hash"] === "string" ? (stored["register_hash"] as string) : "",
    decided_by: String(stored["decided_by"] ?? ""),
    decided_at: String(stored["decided_at"] ?? task.completed_at ?? ""),
    statement: String(stored["statement"] ?? ""),
    signature_id: typeof stored["signature_id"] === "string" ? (stored["signature_id"] as string) : null,
    ruleset_version: typeof stored["ruleset_version"] === "string" ? (stored["ruleset_version"] as string) : null,
    cleared: list("cleared"),
    rejected: list("rejected"),
    swapped: Array.isArray(stored["swapped"]) ? (stored["swapped"] as Swapped[]) : [],
    resumed: false,
  };
}

/**
 * H3 in the `Signatures & attestations` tab (Stage 04 PRD §15.4 I).
 *
 * For the named legal owner — the task's assignee, whom the server alone
 * names (`can_submit`) — one row per exception: kind, subject, where it
 * appears, the proposed substantiation, the evidence, and what ships if it is
 * rejected, rendered. Clear or reject per row; there is no "clear all".
 * Submitting opens Stage 03's step-up ceremony with the `set_hash` in mono
 * and ends in a receipt.
 *
 * Everyone else reads `Awaiting {legal owner}` over the same rows with every
 * decide control absent (§15.5 item 4) — including an administrator, who
 * cannot clear H3 (Law 40). A `creative_execute` holder also gets `Withdraw
 * exceptions`, whose confirmation states the swap and drop counts the server
 * computes before anything is confirmed.
 */
export function H3TaskCard({ task, onDone }: { task: HumanTask; onDone: () => void }) {
  const runId = task.guideline_run_id;
  const session = useSession();
  const set = useCreativeExceptions(runId);
  const assets = useCreativeAssets(runId ?? "", Boolean(runId));
  const [decisions, setDecisions] = useState<Record<string, RowDecision>>({});
  const [signing, setSigning] = useState<{ decisions: { exception_id: string; decision: ExceptionDecision; note?: string }[]; setHash: string } | null>(null);
  const [receipt, setReceipt] = useState<ClearReceipt | null>(null);
  const [moved, setMoved] = useState<string | null>(null);
  const [withdrawOpen, setWithdrawOpen] = useState(false);

  const open = task.status === "pending" || task.status === "in_progress" || task.status === "blocked";
  const deciding = open && task.can_submit;
  const canWithdraw = open && !task.can_submit && session.has("creative_execute");
  const owner = task.assignee_name ?? task.assignee_email ?? "the legal owner";
  const rows = openExceptions(set.data);
  const settled = storedReceipt(task);
  const shownReceipt = receipt ?? settled;
  const byId = useMemo(
    () => new Map((assets.data?.items ?? []).map((asset) => [asset.id, asset])),
    [assets.data],
  );

  const decided = rows.filter((row) => decisions[row.exception_id]?.decision).length;
  const cleared = rows.filter((row) => decisions[row.exception_id]?.decision === "cleared").length;
  const allDecided = rows.length > 0 && decided === rows.length;

  const startSigning = () => {
    if (!set.data?.set_hash || !allDecided) return;
    setMoved(null);
    setSigning({
      setHash: set.data.set_hash,
      decisions: rows.map((row) => {
        const chosen = decisions[row.exception_id] as RowDecision & { decision: ExceptionDecision };
        const note = chosen.note.trim();
        return { exception_id: row.exception_id, decision: chosen.decision, ...(note ? { note } : {}) };
      }),
    });
  };

  return (
    <article
      aria-labelledby={`h3-${task.id}`}
      className={cn("rounded-token border bg-surface-raised", open && "border-status-gate")}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b px-5 py-4">
        <div className="min-w-0">
          <h3 id={`h3-${task.id}`} className="flex items-center gap-2 text-base font-medium text-fg">
            <Scale aria-hidden className="size-4 shrink-0 text-status-gate" />
            {task.title || "Legal exceptions"}
          </h3>
          <p className="mt-0.5 text-sm text-fg-muted">
            {task.project_name ?? "Project"} · H3 · blocks launch
            {task.created_at ? ` · opened ${absoluteTime(task.created_at)}` : ""}
          </p>
        </div>
        <Badge>H3</Badge>
      </header>

      {!deciding && open ? (
        <div className="flex flex-wrap items-center justify-between gap-3 border-b px-5 py-3">
          <p className="text-sm text-fg" data-testid="h3-awaiting">
            Awaiting {owner}
            <span className="block text-xs text-fg-muted">
              Only the project&rsquo;s named legal owner decides these. Nobody can do it for them, including an
              administrator.
            </span>
          </p>
          {canWithdraw && rows.length > 0 ? (
            <Button variant="secondary" size="sm" onClick={() => setWithdrawOpen(true)}>
              <Undo2 aria-hidden />
              Withdraw exceptions
            </Button>
          ) : null}
        </div>
      ) : null}

      {task.status === "not_required" ? (
        <p className="px-5 py-4 text-sm text-fg-muted">
          Every exception was withdrawn. Their assets swapped to fallbacks or dropped, nothing was licensed, and H3
          was not needed.
        </p>
      ) : null}

      {shownReceipt ? (
        <div className="p-4">
          <H3Receipt receipt={shownReceipt} />
        </div>
      ) : null}

      {open && !receipt ? (
        <div className="flex flex-col gap-3 p-4">
          {set.isPending ? <Skeleton className="h-40 w-full" /> : null}
          {set.isError ? (
            <Alert tone="error" title="The exceptions could not be read">
              {errorMessage(set)}
            </Alert>
          ) : null}
          {moved ? (
            <Alert tone="warning" title="The exceptions changed after you read them">
              {moved} Nothing was recorded. Read them again below and decide again.
            </Alert>
          ) : null}
          {set.data && rows.length === 0 ? (
            <p className="text-sm text-fg-muted">No exception is open on this run.</p>
          ) : null}

          <ol className="flex flex-col gap-3">
            {rows.map((row, index) => (
              <li key={row.exception_id}>
                <ExceptionRow
                  row={row}
                  index={index}
                  assets={byId}
                  decision={deciding ? (decisions[row.exception_id] ?? { decision: null, note: "" }) : null}
                  onDecide={(next) => setDecisions((prior) => ({ ...prior, [row.exception_id]: next }))}
                />
              </li>
            ))}
          </ol>

          {deciding && rows.length > 0 ? (
            <div className="flex flex-wrap items-center justify-between gap-3 border-t pt-3">
              <p className="text-sm tabular-nums text-fg-muted" aria-live="polite">
                {decided} of {rows.length} decided · {cleared} cleared · {decided - cleared} rejected
              </p>
              <Button onClick={startSigning} disabled={!allDecided || !set.data?.set_hash}>
                <ShieldCheck aria-hidden />
                Sign {rows.length} {rows.length === 1 ? "decision" : "decisions"}
              </Button>
            </div>
          ) : null}
        </div>
      ) : null}

      {signing && runId ? (
        <StepUpCeremony
          open
          onOpenChange={(next) => {
            if (!next) setSigning(null);
          }}
          title="Sign these legal decisions"
          statement={H3_STATEMENT}
          summary={
            <>
              <strong className="font-medium">{signing.decisions.filter((d) => d.decision === "cleared").length}</strong>{" "}
              cleared{" · "}
              <strong className="font-medium">{signing.decisions.filter((d) => d.decision === "rejected").length}</strong>{" "}
              rejected
            </>
          }
          setHash={signing.setHash}
          confirmLabel={`Sign ${signing.decisions.length} ${signing.decisions.length === 1 ? "decision" : "decisions"}`}
          sign={(token) =>
            clearCreativeExceptions(runId, {
              decisions: signing.decisions,
              setHash: signing.setHash,
              reauthToken: token,
            })
          }
          onSigned={(result) => {
            // Snapshotted into state, so the receipt outlives the refetch that
            // closes the task (S3-P8: a receipt that unmounts is no receipt).
            setReceipt(result);
            setSigning(null);
            void set.refetch();
            onDone();
          }}
          onSetMoved={(detail) => {
            setSigning(null);
            setMoved(detail);
            setDecisions({});
            void set.refetch();
          }}
        />
      ) : null}

      {canWithdraw && runId ? (
        <WithdrawDialog
          open={withdrawOpen}
          onOpenChange={setWithdrawOpen}
          runId={runId}
          rows={rows}
          owner={owner}
          onWithdrawn={() => {
            void set.refetch();
            void assets.refetch();
            onDone();
          }}
        />
      ) : null}
    </article>
  );
}

function assetPreview(asset: CreativeAssetItem | undefined, subject: string | null, struck = false) {
  if (!asset) return <span className="text-xs text-fg-muted">An asset this run no longer lists</span>;
  const media = typeof asset.fields["master_media_id"] === "string" ? (asset.fields["master_media_id"] as string) : null;
  if (asset.kind === "image" && media) {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- a signed 302 the optimiser cannot follow
      <img
        src={mediaContentUrl(media, "preview")}
        alt={`${asset.campaign_ref} image`}
        loading="lazy"
        decoding="async"
        className={cn("h-16 w-auto rounded-token border bg-surface object-contain", struck && "opacity-50")}
      />
    );
  }
  if (asset.text === null) {
    return (
      <span className={cn("text-sm text-fg-muted", struck && "line-through")}>
        {asset.kind === "image" ? "Image" : asset.kind} with no stored preview
        <span className="ml-1.5 font-mono text-xs text-fg-subtle">{asset.surface}</span>
      </span>
    );
  }
  const text = asset.text;
  const at = subject ? text.toLowerCase().indexOf(subject.toLowerCase()) : -1;
  return (
    <span className={cn("text-sm text-fg", struck && "text-fg-muted line-through")}>
      {at >= 0 && subject ? (
        <>
          {text.slice(0, at)}
          <mark className="rounded-sm bg-accent-soft px-0.5 text-accent-soft-fg">{text.slice(at, at + subject.length)}</mark>
          {text.slice(at + subject.length)}
        </>
      ) : (
        text
      )}
      <span className="ml-1.5 font-mono text-xs text-fg-subtle">{asset.surface}</span>
    </span>
  );
}

/** One exception, as §15.4 I lists it, with the decide control for the legal owner alone. */
function ExceptionRow({
  row,
  index,
  assets,
  decision,
  onDecide,
}: {
  row: CreativeExceptionItem;
  index: number;
  assets: ReadonlyMap<string, CreativeAssetItem>;
  /** null: this reader cannot decide — the control is absent. */
  decision: RowDecision | null;
  onDecide: (next: RowDecision) => void;
}) {
  const proposed = row.proposed;
  const referenceId = typeof proposed["reference_id"] === "string" ? (proposed["reference_id"] as string) : null;
  const titleId = `exception-${row.exception_id}`;
  return (
    <section
      aria-labelledby={titleId}
      data-testid="h3-row"
      className={cn(
        "rounded-token border p-4",
        decision?.decision === "cleared" && "border-status-success",
        decision?.decision === "rejected" && "border-status-failed",
      )}
    >
      <header className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
        <span className="text-xs tabular-nums text-fg-muted">{index + 1}.</span>
        <Badge>{KIND_LABEL[row.kind]}</Badge>
        <h4 id={titleId} className="min-w-0 text-sm font-medium text-fg">
          {row.subject ? `“${row.subject}”` : KIND_LABEL[row.kind]}
        </h4>
      </header>

      <dl className="mt-3 grid gap-x-6 gap-y-3 md:grid-cols-2">
        <div className="min-w-0 md:col-span-2">
          <dt className="text-xs text-fg-subtle">Where it appears</dt>
          <dd className="mt-1 flex flex-col gap-1.5">
            <span className="text-sm tabular-nums text-fg-muted">
              {row.asset_ids.length} {row.asset_ids.length === 1 ? "asset" : "assets"} · {row.occurrences}{" "}
              {row.occurrences === 1 ? "occurrence" : "occurrences"}
            </span>
            {row.asset_ids.length > 0 ? (
              <ul className="flex flex-col gap-1.5">
                {row.asset_ids.map((id) => (
                  <li key={id} className="flex flex-wrap items-center gap-2">
                    {assetPreview(assets.get(id), row.subject)}
                  </li>
                ))}
              </ul>
            ) : null}
          </dd>
        </div>

        <div className="min-w-0">
          <dt className="text-xs text-fg-subtle">Proposed substantiation</dt>
          <dd className="mt-1 text-sm text-fg">
            <Substantiation row={row} referenceId={referenceId} />
          </dd>
        </div>

        <div className="min-w-0">
          <dt className="text-xs text-fg-subtle">Evidence</dt>
          <dd className="mt-1">
            <EvidenceList ids={row.evidence_ids} />
          </dd>
        </div>

        <div className="min-w-0 md:col-span-2">
          <dt className="text-xs text-fg-subtle">What ships if rejected</dt>
          <dd className="mt-1" data-testid="h3-if-rejected">
            <IfRejected swaps={row.if_rejected ?? []} assets={assets} subject={row.subject} kind={row.kind} />
          </dd>
        </div>
      </dl>

      {decision ? (
        <div className="mt-4 flex flex-col gap-2 border-t pt-3">
          <SegmentedControl
            label={`Decision on exception ${index + 1}`}
            options={DECISION_OPTIONS}
            value={decision.decision}
            onChange={(value) => onDecide({ ...decision, decision: value })}
          />
          <details className="group text-sm" open={decision.note.length > 0}>
            <summary className="w-fit cursor-pointer text-fg-muted hover:text-fg">Add a note, kept with the decision</summary>
            <Textarea
              aria-label={`Note on exception ${index + 1}`}
              value={decision.note}
              maxLength={2000}
              className="mt-1.5 min-h-16"
              onChange={(event) => onDecide({ ...decision, note: event.target.value })}
            />
          </details>
        </div>
      ) : null}
    </section>
  );
}

function Substantiation({ row, referenceId }: { row: CreativeExceptionItem; referenceId: string | null }) {
  const p = row.proposed;
  if (row.kind === "new_claim") {
    const markets = Array.isArray(p["market_scope"]) ? (p["market_scope"] as string[]).join(", ") : "";
    const languages = Array.isArray(p["languages"]) ? (p["languages"] as string[]).join(", ") : "";
    return (
      <span className="flex flex-col gap-0.5">
        <span>
          {typeof p["substantiation"] === "string" && p["substantiation"]
            ? (p["substantiation"] as string)
            : "None proposed. Clearing licenses the claim on your signature and the evidence here."}
        </span>
        <span className="text-xs text-fg-muted">
          {typeof p["claim_type"] === "string" ? `${String(p["claim_type"]).replaceAll("_", " ")} claim` : "Claim"}
          {markets ? ` · ${markets}` : ""}
          {languages ? ` · ${languages}` : ""}
        </span>
      </span>
    );
  }
  if (row.kind === "disclaimer") {
    return (
      <span className="flex flex-col gap-0.5">
        <span>{typeof p["text"] === "string" ? `“${p["text"] as string}”` : "No wording proposed"}</span>
        {typeof p["placement"] === "string" ? (
          <span className="text-xs text-fg-muted">Placed at the {p["placement"] as string}</span>
        ) : null}
      </span>
    );
  }
  return (
    <span className="flex flex-col gap-1.5">
      <span>{typeof p["basis"] === "string" ? (p["basis"] as string) : "No basis recorded"}</span>
      {referenceId ? (
        // eslint-disable-next-line @next/next/no-img-element -- a signed 302 the optimiser cannot follow
        <img
          src={mediaReferenceContentUrl(referenceId)}
          alt="The reference this right is about"
          loading="lazy"
          decoding="async"
          className="h-16 w-auto rounded-token border bg-surface object-contain"
        />
      ) : null}
    </span>
  );
}

function EvidenceList({ ids }: { ids: string[] }) {
  const evidence = useQuery({
    queryKey: keys.evidence({ ids }),
    queryFn: () => listEvidence({ ids, limit: Math.max(ids.length, 1) }),
    enabled: ids.length > 0,
    staleTime: 60_000,
  });
  if (ids.length === 0) return <p className="text-sm text-fg-muted">None attached. The decision rests on the signer.</p>;
  if (evidence.isPending) return <Skeleton className="h-10 w-full" />;
  if (evidence.isError) return <p className="text-sm text-status-failed">Evidence could not be read: {errorMessage(evidence)}</p>;
  return (
    <ul className="flex flex-col gap-1">
      {evidence.data.items.map((item) => (
        <li key={item.id} className="text-sm text-fg">
          <span className="text-xs text-fg-muted">{SOURCE_LABEL[item.source]} · </span>
          {item.source_url ? (
            <a href={item.source_url} target="_blank" rel="noreferrer" className="text-accent hover:underline">
              {evidenceSummary(item).slice(0, 140)}
            </a>
          ) : (
            evidenceSummary(item).slice(0, 140)
          )}
        </li>
      ))}
    </ul>
  );
}

function IfRejected({
  swaps,
  assets,
  subject,
  kind,
}: {
  swaps: Swapped[];
  assets: ReadonlyMap<string, CreativeAssetItem>;
  subject: string | null;
  kind: CreativeExceptionItem["kind"];
}) {
  if (swaps.length === 0) {
    return (
      <p className="text-sm text-fg-muted">
        {kind === "disclaimer" ? "The disclaimer is not added. No asset changes." : "No asset changes."}
      </p>
    );
  }
  return (
    <ul className="flex flex-col gap-2">
      {swaps.map((swap) => (
        <li key={swap.out} className="flex flex-wrap items-center gap-2">
          {assetPreview(assets.get(swap.out), subject, true)}
          <ArrowRight aria-label="is replaced by" className="size-4 shrink-0 text-fg-subtle" />
          {swap.into ? (
            assetPreview(assets.get(swap.into), null)
          ) : (
            <span className="text-sm text-fg">Nothing — it drops and its slot ships one short</span>
          )}
        </li>
      ))}
    </ul>
  );
}

/** The H3 receipt: rendered in place, and again from the task once it is settled. */
export function H3Receipt({ receipt }: { receipt: ClearReceipt }) {
  const download = () => {
    const blob = new Blob([JSON.stringify(receipt, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `h3-${receipt.run_id}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };
  const swapped = receipt.swapped.filter((swap) => swap.into !== null).length;
  const dropped = receipt.swapped.length - swapped;
  return (
    <section aria-label="H3 receipt" data-testid="h3-receipt" className="rounded-token border border-status-success bg-surface">
      <header className="flex items-start gap-3 border-b px-5 py-4">
        <ShieldCheck aria-hidden className="mt-0.5 size-5 shrink-0 text-status-success" />
        <div className="min-w-0 flex-1">
          <h4 className="text-base font-medium text-fg">Signed</h4>
          <p className="mt-0.5 text-sm tabular-nums text-fg-muted">
            {receipt.cleared.length} cleared · {receipt.rejected.length} rejected
            {receipt.decided_at ? ` · ${absoluteTime(receipt.decided_at)}` : ""}
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={download}>
          <Download aria-hidden />
          Export receipt
        </Button>
      </header>
      <dl className="grid gap-x-6 gap-y-3 px-5 py-4 sm:grid-cols-2">
        <ReceiptRow label="Set hash">
          <code className="truncate font-mono text-xs" title={receipt.set_hash}>
            {receipt.set_hash}
          </code>
          <CopyButton value={receipt.set_hash} label="Copy set hash" />
        </ReceiptRow>
        <ReceiptRow label="Claim signature">
          {receipt.signature_id ? (
            <>
              <code className="truncate font-mono text-xs">{receipt.signature_id}</code>
              <CopyButton value={receipt.signature_id} label="Copy signature id" />
            </>
          ) : (
            "No claim was cleared, so no claim signature"
          )}
        </ReceiptRow>
        <ReceiptRow label="Ruleset">
          {receipt.ruleset_version ? `Repinned to ${receipt.ruleset_version}` : "Not repinned"}
        </ReceiptRow>
        <ReceiptRow label="Assets">
          <span className="tabular-nums">
            {swapped} swapped to fallbacks · {dropped} dropped
          </span>
        </ReceiptRow>
      </dl>
      <div className="border-t px-5 py-4">
        <h5 className="text-xs font-medium text-fg-subtle">What was attested</h5>
        <p className="mt-1.5 text-sm leading-relaxed text-fg">{receipt.statement}</p>
      </div>
    </section>
  );
}

function ReceiptRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-subtle">{label}</dt>
      <dd className="mt-0.5 flex min-w-0 items-center gap-1.5 text-sm text-fg">{children}</dd>
    </div>
  );
}

/** `Withdraw exceptions`: the swap and drop counts, from the server, before confirming. */
function WithdrawDialog({
  open,
  onOpenChange,
  runId,
  rows,
  owner,
  onWithdrawn,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  runId: string;
  rows: CreativeExceptionItem[];
  owner: string;
  onWithdrawn: () => void;
}) {
  const ids = rows.map((row) => row.exception_id);
  const preview = useQuery({
    queryKey: [...keys.creativeExceptions(runId), "withdraw-preview", ids] as const,
    queryFn: () => previewWithdrawExceptions(runId, ids),
    enabled: open && ids.length > 0,
    staleTime: 0,
    retry: false,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      await withdrawCreativeExceptions(runId, ids);
      onOpenChange(false);
      onWithdrawn();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : "Nothing was withdrawn.");
      void preview.refetch();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={busy ? undefined : onOpenChange}>
      <DialogContent
        title={`Withdraw ${ids.length} legal ${ids.length === 1 ? "exception" : "exceptions"}`}
        description={`They leave ${owner}'s set. Nothing is licensed: the copy they tie up swaps to its precomputed fallback, or drops.`}
      >
        <DialogBody>
          {preview.isPending ? (
            <p className="flex items-center gap-2 text-sm text-fg-muted">
              <Spinner label="Counting" /> Counting what swaps and what drops…
            </p>
          ) : preview.isError ? (
            <Alert tone="error" title="Cannot withdraw">
              {preview.error instanceof ApiError ? preview.error.detail : "The consequence could not be computed."}
            </Alert>
          ) : (
            <>
              <p className="text-base font-medium tabular-nums text-fg" data-testid="withdraw-consequence">
                {withdrawConsequence(ids.length, preview.data)}.
              </p>
              <p className="text-sm text-fg-muted">
                {preview.data.h3_ends
                  ? `Nothing is left for ${owner}: H3 ends as not required and the run resumes.`
                  : `${owner} still decides the exceptions that remain.`}
              </p>
            </>
          )}
          <ul className="flex flex-col gap-1 text-sm text-fg">
            {rows.map((row) => (
              <li key={row.exception_id}>
                {KIND_LABEL[row.kind]} · {row.subject ? `“${row.subject}”` : "no subject"}
              </li>
            ))}
          </ul>
          {error ? (
            <Alert tone="error" title="Not withdrawn">
              {error}
            </Alert>
          ) : null}
        </DialogBody>
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button
            onClick={() => void confirm()}
            disabled={busy || !preview.isSuccess}
            // The ink shade: white on `status-failed` is 3.4:1 (axe, measured).
            className="bg-status-failed-ink hover:bg-status-failed-ink/90"
          >
            {busy ? <Spinner label="Withdrawing" /> : null}
            Withdraw {ids.length} {ids.length === 1 ? "exception" : "exceptions"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
