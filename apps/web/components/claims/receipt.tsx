"use client";

import { Download, ShieldCheck, ShieldX } from "lucide-react";

import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { absoluteTime } from "@/lib/format";
import type { SignatureReceipt } from "@/lib/api/claims";
import { cn } from "@/lib/utils";

/**
 * The signature receipt (PRD §15.3 C.5).
 *
 * Rendered in place rather than as a toast, and reachable again afterwards from
 * `GET /claims/{id}/signature` — a receipt you can only see once is not a
 * receipt. It is also the artifact somebody screenshots into a compliance pack,
 * so every field that identifies the signature is present as text, not as an
 * icon or a colour.
 */
export function SignatureReceiptView({
  receipt,
  className,
}: {
  receipt: SignatureReceipt;
  className?: string;
}) {
  const voided = Boolean(receipt.voided_at);

  const download = () => {
    // Built from the receipt already in hand. No second request, so what is
    // exported is exactly what was shown.
    const blob = new Blob([JSON.stringify(receipt, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = `signature-${receipt.signature_id}.json`;
    anchor.click();
    URL.revokeObjectURL(url);
  };

  return (
    <section
      className={cn(
        "rounded-[var(--radius)] border bg-surface-raised",
        voided ? "border-status-failed" : "border-status-success",
        className,
      )}
      aria-label="Signature receipt"
    >
      <header className="flex items-start gap-3 border-b px-5 py-4">
        {voided ? (
          <ShieldX aria-hidden className="mt-0.5 size-5 shrink-0 text-status-failed" />
        ) : (
          <ShieldCheck aria-hidden className="mt-0.5 size-5 shrink-0 text-status-success" />
        )}
        <div className="min-w-0 flex-1">
          <h3 className="text-[length:var(--text-md)] font-medium text-fg">
            {voided ? "This signature was voided" : "Signed"}
          </h3>
          <p className="mt-0.5 text-sm text-fg-muted" data-numeric>
            {receipt.approved_count} approved · {receipt.rejected_count} rejected ·{" "}
            {absoluteTime(receipt.signed_at)}
          </p>
        </div>
        <Button variant="secondary" size="sm" onClick={download}>
          <Download aria-hidden />
          Export
        </Button>
      </header>

      <dl className="grid gap-x-6 gap-y-3 px-5 py-4 sm:grid-cols-2">
        <Row label="Signature">
          <span className="flex items-center gap-1.5">
            <code className="truncate font-mono text-xs">{receipt.signature_id}</code>
            <CopyButton value={receipt.signature_id} label="Copy signature id" />
          </span>
        </Row>
        <Row label="Set hash">
          <span className="flex items-center gap-1.5">
            <code className="truncate font-mono text-xs" title={receipt.set_hash}>
              {receipt.set_hash}
            </code>
            <CopyButton value={receipt.set_hash} label="Copy set hash" />
          </span>
        </Row>
        <Row label="Method">{methodLabel(receipt.method)}</Row>
        <Row label="Expires">
          {receipt.expires_at ? absoluteTime(receipt.expires_at) : "Does not expire"}
        </Row>
      </dl>

      <div className="border-t px-5 py-4">
        <h4 className="text-xs font-medium text-fg-subtle">What was attested</h4>
        <p className="mt-1.5 text-sm leading-relaxed text-fg">{receipt.statement}</p>
      </div>

      {voided ? (
        <div className="border-t border-status-failed/40 bg-surface px-5 py-4">
          <h4 className="text-xs font-medium text-fg-subtle">Voided</h4>
          <p className="mt-1 text-sm text-fg">
            {absoluteTime(receipt.voided_at as string)}
            {receipt.void_reason ? ` — ${receipt.void_reason}` : null}
          </p>
        </div>
      ) : null}
    </section>
  );
}

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="min-w-0">
      <dt className="text-xs text-fg-subtle">{label}</dt>
      <dd className="mt-0.5 min-w-0 text-sm text-fg">{children}</dd>
    </div>
  );
}

/** Mirrors `agent.db.models.SignatureMethod`. One value today, by design. */
function methodLabel(method: string): string {
  return method === 'step_up_password' ? 'Password step-up' : method;
}
