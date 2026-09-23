"use client";

import { Check, ChevronDown, FileSearch, RotateCw, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";

import { claimTypeLabel } from "@/components/claims/chips";
import { SignatureReceiptView } from "@/components/claims/receipt";
import { StepUpDialog } from "@/components/claims/step-up-dialog";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import type { ClaimDecision, ClaimSummary, SignatureReceipt } from "@/lib/api/claims";
import { cn } from "@/lib/utils";

type Verdict = "approved" | "rejected";
type Row = { verdict: Verdict | null; note: string; expiresAt: string; touched: boolean };

/**
 * A row for a claim the decision map has not caught up with yet.
 *
 * Hoisted out of the component so its identity is stable: the decisions memo
 * depends on it, and a function rebuilt every render would rebuild the memo
 * every render too — on the one screen where a 500-row recompute is felt.
 */
const blank = (claim?: ClaimSummary): Row => ({
  verdict: null,
  note: "",
  expiresAt: claim?.expires_at ? claim.expires_at.slice(0, 10) : "",
  touched: false,
});

/**
 * The signature drawer (PRD §15.3 C.1–C.3).
 *
 * A drawer and not a modal, deliberately: the question being answered is "what
 * am I signing, against what register", and covering the register to ask it
 * would remove the context the answer depends on. The one modal in this flow is
 * the step-up, which genuinely needs protected focus.
 *
 * **`touched` is the anti-rubber-stamp mechanism.** §15.3 C.2 allows bulk
 * approve but forbids "approve all unseen", so a row only becomes bulk-eligible
 * once it has actually been expanded. Marking rows touched on render would
 * satisfy the letter of the rule and defeat it entirely — which is why the flag
 * is set by the expand handler and by nothing else.
 */
export function SignatureDrawer({
  open,
  onClose,
  guidelineId,
  claims,
  setHash,
  onSigned,
  onReRead,
}: {
  open: boolean;
  onClose: () => void;
  guidelineId: string;
  claims: ClaimSummary[];
  setHash: string;
  onSigned: () => void;
  /** Close and re-read. The 409's only exit — see the interstitial below. */
  onReRead: () => void;
}) {
  const [rows, setRows] = useState<Record<string, Row>>({});
  const [expanded, setExpanded] = useState<string | null>(null);
  const [stepUp, setStepUp] = useState(false);
  const [receipt, setReceipt] = useState<SignatureReceipt | null>(null);
  const [moved, setMoved] = useState<string | null>(null);
  const panel = useRef<HTMLDivElement>(null);

  // Re-seeded whenever the set changes, so a refetch after a 409 cannot leave
  // decisions attached to claims that are no longer in the register.
  useEffect(() => {
    setRows(
      Object.fromEntries(
        claims.map((claim) => [claim.id, blank(claim)]),
      ),
    );
  }, [claims]);

  // Focus the panel rather than a control inside it: a drawer that opens with
  // "Approve" focused is one keystroke away from a decision nobody read.
  //
  // The element that had focus is remembered and restored on close. Radix does
  // this for the two dialogs in this flow; the drawer is hand-rolled — Radix's
  // `Dialog` centres and this needed a side panel — so it does it here rather
  // than relying on the trigger happening to survive in the DOM.
  useEffect(() => {
    if (!open) return;
    const restore = document.activeElement as HTMLElement | null;
    panel.current?.focus();
    return () => restore?.focus?.();
  }, [open]);

  /**
   * Keep Tab inside the panel.
   *
   * `aria-modal="true"` tells a screen reader that everything behind this is
   * inert. Without a trap that is a lie a keyboard user discovers by tabbing
   * into a register they were told they could not reach — and on this screen
   * the things behind the overlay are the rows being signed.
   */
  const trapTab = (event: KeyboardEvent) => {
    if (event.key !== "Tab" || !panel.current) return;
    const focusable = panel.current.querySelectorAll<HTMLElement>(
      'a[href], button:not([disabled]), input:not([disabled]), textarea:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
    const active = document.activeElement;
    if (event.shiftKey && (active === first || active === panel.current)) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  const decided = claims.filter((claim) => rows[claim.id]?.verdict).length;
  const undecided = claims.length - decided;
  const touchedCount = claims.filter((claim) => rows[claim.id]?.touched).length;

  const decisions: ClaimDecision[] = useMemo(
    () =>
      claims
        .filter((claim) => rows[claim.id]?.verdict)
        .map((claim) => {
          const row = rows[claim.id] ?? blank(claim);
          return {
            claim_id: claim.id,
            normalized_text: claim.normalized_text,
            decision: row.verdict as Verdict,
            note: row.note.trim() || null,
            expires_at: row.expiresAt ? new Date(`${row.expiresAt}T00:00:00Z`).toISOString() : null,
          };
        }),
    [claims, rows],
  );

  const set = (id: string, patch: Partial<Row>) =>
    setRows((current) => ({ ...current, [id]: { ...(current[id] ?? blank()), ...patch } }));

  const toggle = (id: string) => {
    const next = expanded === id ? null : id;
    setExpanded(next);
    // Touched on expansion, never on render. This is the rule.
    if (next) set(id, { touched: true });
  };

  if (!open) return null;

  return (
    <>
      <div
        className="fixed inset-0 z-40 bg-black/30"
        onClick={onClose}
        aria-hidden
      />
      <aside
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label="Sign claims"
        tabIndex={-1}
        className={cn(
          "fixed inset-y-0 right-0 z-40 flex w-full max-w-2xl flex-col border-l bg-bg",
          "shadow-[var(--shadow-overlay)]",
        )}
        onKeyDown={(event) => {
          if (event.key === "Escape") onClose();
          trapTab(event);
        }}
      >
        <header className="flex items-start justify-between gap-4 border-b px-5 py-4">
          <div className="min-w-0">
            <h2 className="text-[length:var(--text-md)] font-medium tracking-tight text-fg">
              {receipt ? "Signed" : `Sign ${claims.length} claims`}
            </h2>
            <p className="mt-0.5 text-sm text-fg-muted">
              {receipt
                ? "Keep this receipt. It is reachable again from any claim in the set."
                : "Each claim is decided on its own. Nothing is signed until you confirm your password."}
            </p>
          </div>
          <Button variant="ghost" size="icon" onClick={onClose} aria-label="Close">
            <X aria-hidden />
          </Button>
        </header>

        {moved ? (
          // §16 rule 2's 409, rendered as an interstitial rather than a toast.
          // A signer must not be able to dismiss "the register changed" and try
          // again — the whole point of the hash is that they re-read it.
          <div className="flex flex-1 flex-col items-center justify-center gap-4 px-8 text-center">
            <div aria-live="assertive" className="max-w-md">
              <h3 className="text-[length:var(--text-md)] font-medium text-fg">
                The register changed while you were reading it
              </h3>
              <p className="mt-2 text-sm text-fg-muted">{moved}</p>
              <p className="mt-2 text-sm text-fg-muted">
                Nothing was signed. Read the set again — what you approve has to be what is
                actually in the register.
              </p>
            </div>
            <Button onClick={onReRead}>
              <RotateCw aria-hidden />
              Re-read the register
            </Button>
          </div>
        ) : receipt ? (
          <div className="flex-1 overflow-y-auto px-5 py-4">
            <SignatureReceiptView receipt={receipt} />
          </div>
        ) : (
          <>
            <div className="flex-1 overflow-y-auto">
              <ul className="divide-y">
                {claims.map((claim) => {
                  const row = rows[claim.id];
                  const isOpen = expanded === claim.id;
                  return (
                    <li key={claim.id} className={cn(row?.verdict && "bg-surface")}>
                      <div className="flex items-start gap-3 px-5 py-3">
                        <button
                          type="button"
                          onClick={() => toggle(claim.id)}
                          aria-expanded={isOpen}
                          className="flex min-w-0 flex-1 items-start gap-2 text-left"
                        >
                          <ChevronDown
                            aria-hidden
                            className={cn(
                              "mt-0.5 size-4 shrink-0 text-fg-subtle transition-transform duration-200",
                              isOpen && "rotate-180",
                            )}
                          />
                          <span className="min-w-0">
                            <span className="block text-sm text-fg">{claim.claim_text}</span>
                            <span className="mt-0.5 block text-xs text-fg-subtle">
                              {claimTypeLabel(claim.claim_type)} · {claim.market_scope.join(", ") || "all markets"}
                              {row?.touched ? null : " · not read yet"}
                            </span>
                          </span>
                        </button>
                        <div className="flex shrink-0 items-center gap-1">
                          <Button
                            size="sm"
                            variant={row?.verdict === "approved" ? "primary" : "secondary"}
                            aria-pressed={row?.verdict === "approved"}
                            onClick={() => set(claim.id, { verdict: "approved", touched: true })}
                          >
                            <Check aria-hidden />
                            Approve
                          </Button>
                          <Button
                            size="sm"
                            variant={row?.verdict === "rejected" ? "primary" : "secondary"}
                            aria-pressed={row?.verdict === "rejected"}
                            onClick={() => set(claim.id, { verdict: "rejected", touched: true })}
                          >
                            <X aria-hidden />
                            Reject
                          </Button>
                        </div>
                      </div>

                      {isOpen ? (
                        <div className="space-y-4 border-t bg-surface px-5 py-4">
                          <div>
                            <h4 className="text-xs font-medium text-fg-subtle">
                              What this signature licenses
                            </h4>
                            <ul className="mt-1.5 flex flex-wrap gap-1.5">
                              {/* Deduplicated. `surface_forms` is free text the
                                  register does not dedupe, so two entries can be
                                  identical — which renders the same chip twice
                                  and, keyed by value, collides in React. Showing
                                  a signer the same licensed form twice is noise
                                  either way. */}
                              {claim.surface_forms.length > 0 ? (
                                [...new Set(claim.surface_forms)].map((form) => (
                                  <li
                                    key={form}
                                    className="rounded-[calc(var(--radius)-4px)] border bg-surface-raised px-2 py-0.5 text-xs text-fg"
                                  >
                                    {form}
                                  </li>
                                ))
                              ) : (
                                <li className="text-xs text-fg-muted">
                                  Only the exact wording above.
                                </li>
                              )}
                            </ul>
                          </div>

                          <div className="grid gap-3 sm:grid-cols-2">
                            <div>
                              <h4 className="text-xs font-medium text-fg-subtle">Evidence</h4>
                              {claim.evidence_ids.length > 0 ? (
                                <a
                                  className="mt-1 inline-flex items-center gap-1.5 text-sm text-accent hover:underline"
                                  href={`/evidence?ids=${claim.evidence_ids.join(",")}`}
                                >
                                  <FileSearch aria-hidden className="size-3.5" />
                                  {claim.evidence_ids.length} sources
                                </a>
                              ) : (
                                <p className="mt-1 text-sm text-status-failed">
                                  Nothing substantiates this claim.
                                </p>
                              )}
                            </div>
                            <div>
                              <label className="text-xs font-medium text-fg-subtle" htmlFor={`exp-${claim.id}`}>
                                Expires
                              </label>
                              <Input
                                id={`exp-${claim.id}`}
                                type="date"
                                className="mt-1"
                                value={row?.expiresAt ?? ""}
                                onChange={(event) => set(claim.id, { expiresAt: event.target.value })}
                              />
                            </div>
                          </div>

                          <div>
                            <label className="text-xs font-medium text-fg-subtle" htmlFor={`note-${claim.id}`}>
                              Note (optional)
                            </label>
                            <Textarea
                              id={`note-${claim.id}`}
                              rows={2}
                              className="mt-1"
                              value={row?.note ?? ""}
                              onChange={(event) => set(claim.id, { note: event.target.value })}
                              placeholder="Why you decided this way."
                            />
                          </div>
                        </div>
                      ) : null}
                    </li>
                  );
                })}
              </ul>
            </div>

            <footer className="border-t bg-surface px-5 py-3.5">
              <div className="flex flex-wrap items-center justify-between gap-3">
                <p aria-live="polite" data-numeric className="text-sm text-fg-muted">
                  <strong className="font-medium text-fg">{decided}</strong> of {claims.length}{" "}
                  decided
                  {undecided > 0 ? (
                    <span className="text-fg-subtle"> · {undecided} still undecided</span>
                  ) : null}
                </p>
                <div className="flex items-center gap-2">
                  <Button
                    variant="secondary"
                    size="sm"
                    // Only over rows actually read. "Approve all unseen" is the
                    // thing §15.3 C.2 forbids, and this is where it is refused.
                    disabled={touchedCount === 0}
                    title={
                      touchedCount === 0
                        ? "Open a claim before approving it in bulk"
                        : `Approve the ${touchedCount} you have read`
                    }
                    onClick={() =>
                      setRows((current) =>
                        Object.fromEntries(
                          Object.entries(current).map(([id, row]) => [
                            id,
                            row.touched && !row.verdict ? { ...row, verdict: "approved" } : row,
                          ]),
                        ),
                      )
                    }
                  >
                    Approve the {touchedCount} read
                  </Button>
                  <Button disabled={undecided > 0} onClick={() => setStepUp(true)}>
                    Sign {decided} claims
                  </Button>
                </div>
              </div>
              {undecided > 0 ? (
                <p className="mt-2 text-xs text-fg-subtle">
                  Every claim in the set needs a decision. A partially-approved set is normal;
                  a partially-<em>read</em> one is not.
                </p>
              ) : null}
            </footer>
          </>
        )}
      </aside>

      <StepUpDialog
        open={stepUp}
        onOpenChange={setStepUp}
        guidelineId={guidelineId}
        decisions={decisions}
        setHash={setHash}
        onSigned={(signed) => {
          setStepUp(false);
          setReceipt(signed);
          onSigned();
        }}
        onRegisterMoved={(detail) => setMoved(detail)}
      />
    </>
  );
}
