"use client";

import { ExternalLink, Inbox, Loader2 } from "lucide-react";
import { useState } from "react";

import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Textarea } from "@/components/ui/textarea";
import { ApiError } from "@/lib/api";
import {
  KIND_COPY,
  ORIGIN_LABEL,
  applyAmendment,
  dismissAmendment,
  type Amendment,
} from "@/lib/api/amendments";
import { absoluteTime, relativeTime } from "@/lib/format";
import { errorMessage, useAmendments } from "@/lib/queries";
import { cn, httpUrl } from "@/lib/utils";

/**
 * The policy amendment inbox (PRD §15.3 G).
 *
 * The four classes are not a severity ladder. `mechanical` already applied
 * itself and shows the MINOR it produced; `substantive` and `unclassified` wait
 * for a person; `signature_affecting` is a different kind of event entirely —
 * it has already taken a legal signature away from a named person, and the row
 * says whose. That is why this screen names signatures instead of counting
 * them: a number cannot tell you who has to sign again.
 */
export function AmendmentInbox({ projectId }: { projectId?: string }) {
  const amendments = useAmendments(projectId ? { project_id: projectId } : {});
  const [dismissing, setDismissing] = useState<Amendment | null>(null);

  if (amendments.isLoading) {
    return (
      <div className="space-y-3" aria-busy>
        <Skeleton className="h-32 w-full" />
        <Skeleton className="h-32 w-full" />
      </div>
    );
  }
  if (amendments.isError) {
    return (
      <Alert tone="error" title="The amendment inbox could not be read">
        {errorMessage(amendments)}
      </Alert>
    );
  }

  const items = amendments.data?.items ?? [];
  if (items.length === 0) {
    return (
      <EmptyState
        icon={Inbox}
        title="Nothing is asking the rulebook to change"
        description="The policy watcher checks Google's published pages on a schedule. When one changes, or a claim expires, or an ad is disapproved, the reason lands here."
      />
    );
  }

  return (
    <>
      <ul className="space-y-3">
        {items.map((item) => (
          <AmendmentRow
            key={item.id}
            amendment={item}
            onRefetch={() => void amendments.refetch()}
            onDismiss={() => setDismissing(item)}
          />
        ))}
      </ul>

      <DismissDialog
        amendment={dismissing}
        onClose={() => setDismissing(null)}
        onDone={() => {
          setDismissing(null);
          void amendments.refetch();
        }}
      />
    </>
  );
}

function AmendmentRow({
  amendment,
  onRefetch,
  onDismiss,
}: {
  amendment: Amendment;
  onRefetch: () => void;
  onDismiss: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const kind = KIND_COPY[amendment.change_kind] ?? {
    label: amendment.change_kind,
    detail: "",
  };
  const dangerous = amendment.change_kind === "signature_affecting";
  const sourceLink = httpUrl(amendment.source_url);
  const settled = ["applied", "auto_applied", "dismissed"].includes(amendment.status);

  const apply = async () => {
    setBusy(true);
    setError(null);
    try {
      await applyAmendment(amendment.id);
      onRefetch();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : "That did not go through.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <li
      className={cn(
        "rounded-[var(--radius)] border bg-surface-raised",
        dangerous && "border-status-failed",
      )}
    >
      <header className="flex flex-wrap items-start justify-between gap-3 border-b px-5 py-3.5">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <Badge tone={dangerous ? "danger" : amendment.status === "auto_applied" ? "neutral" : "warning"}>
              {kind.label}
            </Badge>
            <span className="text-sm font-medium text-fg">
              {ORIGIN_LABEL[amendment.origin] ?? amendment.origin}
            </span>
            {amendment.project_name ? (
              <span className="text-sm text-fg-muted">· {amendment.project_name}</span>
            ) : null}
          </div>
          <p className="mt-0.5 text-xs text-fg-subtle">
            Detected {relativeTime(amendment.detected_at)}
            {amendment.source_label ? ` · ${amendment.source_label}` : null}
          </p>
        </div>
        {/* Through `httpUrl`, not straight into the attribute. `source_url` is
            `PolicySource.url` — configuration written by `settings_write`, not
            by this stage — and an `href` is the one place a stored string turns
            back into executable code. */}
        {sourceLink ? (
          <a
            href={sourceLink.href}
            target="_blank"
            rel="noreferrer"
            className="inline-flex items-center gap-1 text-xs text-accent hover:underline"
          >
            Source
            <ExternalLink aria-hidden className="size-3" />
          </a>
        ) : null}
      </header>

      <div className="space-y-3 px-5 py-4">
        <p className="text-sm text-fg-muted">{kind.detail}</p>
        {amendment.rationale ? (
          <p className="text-sm text-fg">{amendment.rationale}</p>
        ) : null}

        {amendment.diff ? (
          <details className="rounded-[var(--radius)] border bg-surface">
            <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-fg-muted">
              What changed
            </summary>
            <pre className="overflow-x-auto px-3 pb-3 font-mono text-xs text-fg-muted">
              {JSON.stringify(amendment.diff, null, 2)}
            </pre>
          </details>
        ) : null}

        {/* Named, not counted. This is the whole point of the red row. */}
        {amendment.voided_signatures.length > 0 ? (
          <div className="rounded-[var(--radius)] border border-status-failed bg-surface px-4 py-3">
            <h4 className="text-xs font-medium text-fg">Signatures this voided</h4>
            <ul className="mt-2 space-y-1.5">
              {amendment.voided_signatures.map((signature) => (
                <li key={signature.signature_id} className="text-sm text-fg">
                  <strong className="font-medium">
                    {signature.signer_name ?? "An approver"}
                  </strong>
                  , signed {absoluteTime(signature.signed_at)} —{" "}
                  <span data-numeric>{signature.claim_count}</span> claims
                </li>
              ))}
            </ul>
            <p data-numeric className="mt-2 text-sm text-fg-muted">
              {amendment.requeued_claim_ids.length} claims must be signed again.
            </p>
          </div>
        ) : null}

        {amendment.status === "auto_applied" ? (
          <p className="text-sm text-fg-muted">
            Applied automatically
            {amendment.applied_ruleset_version ? (
              <>
                {" "}
                as{" "}
                <code className="font-mono text-xs">{amendment.applied_ruleset_version}</code>
              </>
            ) : null}
            . Mechanical changes need no decision.
          </p>
        ) : null}

        {amendment.status === "dismissed" ? (
          <p className="text-sm text-fg-muted">
            Dismissed by {amendment.reviewed_by_name ?? "somebody"}
            {amendment.review_note ? ` — ${amendment.review_note}` : null}
          </p>
        ) : null}

        {amendment.status === "applied" ? (
          <p className="text-sm text-fg-muted">
            Applied by {amendment.reviewed_by_name ?? "somebody"}
            {amendment.applied_ruleset_version ? (
              <>
                {" "}
                as{" "}
                <code className="font-mono text-xs">{amendment.applied_ruleset_version}</code>
              </>
            ) : null}
            .
          </p>
        ) : null}

        {error ? (
          <p className="text-sm text-status-failed" aria-live="polite">
            {error}
          </p>
        ) : null}

        {amendment.can_decide && !settled ? (
          <div className="flex items-center justify-end gap-2">
            <Button variant="secondary" size="sm" onClick={onDismiss} disabled={busy}>
              Dismiss with reason
            </Button>
            <Button size="sm" onClick={() => void apply()} disabled={busy}>
              {busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : null}
              Apply
            </Button>
          </div>
        ) : null}
      </div>
    </li>
  );
}

function DismissDialog({
  amendment,
  onClose,
  onDone,
}: {
  amendment: Amendment | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    if (!amendment) return;
    setBusy(true);
    setError(null);
    try {
      await dismissAmendment(amendment.id, reason.trim());
      setReason("");
      onDone();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.detail : "That did not go through.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={Boolean(amendment)} onOpenChange={(open) => (open ? null : onClose())}>
      <DialogContent
        title="Dismiss this amendment"
        description="Dismissing does not un-void anything. It records that the rulebook does not need to change."
      >
        <DialogBody>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="dismiss-reason" className="text-sm font-medium text-fg">
              Reason
            </label>
            <Textarea
              id="dismiss-reason"
              rows={3}
              autoFocus
              value={reason}
              onChange={(event) => setReason(event.target.value)}
              placeholder="Why this does not apply to us."
            />
            <p className="text-xs text-fg-subtle">
              This is the only artifact this decision leaves behind, so write it for somebody
              reading it in a year.
            </p>
          </div>
          {error ? <p className="text-sm text-status-failed">{error}</p> : null}
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          <Button disabled={!reason.trim() || busy} onClick={() => void submit()}>
            {busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : null}
            Dismiss
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
