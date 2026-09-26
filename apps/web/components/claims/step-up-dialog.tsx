"use client";

import { AlertTriangle, Loader2 } from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";

import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { CopyButton } from "@/components/ui/copy-button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { ApiError } from "@/lib/api";
import { ATTESTATION, reauth, signClaims, type ClaimDecision, type SignatureReceipt } from "@/lib/api/claims";

/**
 * The step-up ceremony (PRD §15.3 C.4).
 *
 * This is the one modal in Stage 03 that earns being a modal: it needs
 * protected focus, because the thing being confirmed is a legal signature and
 * a stray click on the register behind it must not be able to change what is
 * being signed.
 *
 * Four rules govern the password, and none of them is optional:
 *
 * 1. it lives in a ref, never in state that could be serialised into a devtools
 *    snapshot or a React error boundary's payload;
 * 2. `autoComplete="off"` and a `new-password` hint, so no manager offers to
 *    remember a value whose entire purpose is to prove presence *now*;
 * 3. it is cleared on unmount and on every close, including Escape;
 * 4. the re-auth token it mints is a local `const` inside `submit` — it is
 *    never stored, never cached, and goes out of scope when the call returns.
 *
 * The `set_hash` is displayed because the signer is attesting to a specific set
 * and the hash is what identifies it. It is echoed from the list response and
 * never computed here: a client that computed its own could not detect the
 * register moving underneath it, which is the only thing the hash is for.
 */
export function StepUpDialog({
  open,
  onOpenChange,
  guidelineId,
  decisions,
  setHash,
  onSigned,
  onRegisterMoved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  guidelineId: string;
  decisions: ClaimDecision[];
  setHash: string;
  onSigned: (receipt: SignatureReceipt) => void;
  /** The 409. Handled by the drawer as an interstitial, never as a toast. */
  onRegisterMoved: (detail: string) => void;
}) {
  const approved = decisions.filter((item) => item.decision === "approved").length;
  const rejected = decisions.length - approved;
  return (
    <StepUpCeremony
      open={open}
      onOpenChange={onOpenChange}
      title="Sign this claim set"
      statement={ATTESTATION}
      summary={
        <>
          <strong className="font-medium">{approved}</strong> approved
          {" · "}
          <strong className="font-medium">{rejected}</strong> rejected
        </>
      }
      setHash={setHash}
      confirmLabel={`Sign ${decisions.length} claims`}
      sign={(token) =>
        signClaims(guidelineId, {
          decisions,
          statement: ATTESTATION,
          set_hash: setHash,
          reauth_token: token,
        })
      }
      onSigned={onSigned}
      onSetMoved={onRegisterMoved}
    />
  );
}

/**
 * The ceremony itself, for any set a named person signs with a step-up: Stage
 * 03's claim set above, Stage 04's H3 exceptions (§15.4 I: "Submit opens the
 * Stage 03 step-up dialog with `set_hash` in mono"). The four password rules
 * hold for every caller, because they live here and nowhere else; `sign` gets
 * the freshly minted token and nothing else ever sees it.
 */
export function StepUpCeremony<R>({
  open,
  onOpenChange,
  title,
  statement,
  summary,
  setHash,
  confirmLabel,
  sign,
  onSigned,
  onSetMoved,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  statement: string;
  /** What is being signed, in numbers ("2 cleared · 1 rejected"). */
  summary: ReactNode;
  setHash: string;
  confirmLabel: string;
  /** Spend the re-auth token. Called once per attempt, token never stored. */
  sign: (reauthToken: string) => Promise<R>;
  onSigned: (result: R) => void;
  /** The 409: the set moved under the signer. An interstitial, never a toast. */
  onSetMoved: (detail: string) => void;
}) {
  const password = useRef("");
  const field = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const clear = () => {
    password.current = "";
    if (field.current) field.current.value = "";
  };

  // Clears on unmount as well as on close: a dialog torn down by a route change
  // mid-flow must not leave the value behind in a detached node.
  useEffect(() => clear, []);
  useEffect(() => {
    if (!open) {
      clear();
      setError(null);
      setBusy(false);
    }
  }, [open]);

  const submit = async () => {
    if (!password.current) {
      setError("Enter your password to sign.");
      field.current?.focus();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      // Minted and spent inside one function body. There is no assignment of
      // this value to anything that outlives the call.
      const { token } = await reauth(password.current);
      const result = await sign(token);
      clear();
      onSigned(result);
    } catch (caught) {
      const problem = caught instanceof ApiError ? caught : null;
      if (problem?.status === 409) {
        clear();
        onOpenChange(false);
        onSetMoved(problem.detail);
        return;
      }
      setError(problem?.detail ?? "That did not go through. Nothing was signed.");
      // A wrong password leaves every decision intact; only the field resets.
      clear();
      field.current?.focus();
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={title}
        description="Your password proves you are here now. A session alone does not."
        className="max-w-xl"
        onOpenAutoFocus={(event) => {
          event.preventDefault();
          field.current?.focus();
        }}
      >
        <DialogBody>
          <div className="rounded-[var(--radius)] border bg-surface p-4">
            <p className="text-sm leading-relaxed text-fg">{statement}</p>
          </div>

          <dl className="grid gap-3 sm:grid-cols-2">
            <div>
              <dt className="text-xs text-fg-subtle">You are signing</dt>
              <dd data-numeric className="mt-0.5 text-sm text-fg">
                {summary}
              </dd>
            </div>
            <div className="min-w-0">
              <dt className="text-xs text-fg-subtle">Set hash</dt>
              <dd className="mt-0.5 flex items-center gap-1.5">
                <code className="truncate font-mono text-xs text-fg-muted" title={setHash}>
                  {setHash}
                </code>
                <CopyButton value={setHash} label="Copy set hash" />
              </dd>
            </div>
          </dl>

          <div className="flex flex-col gap-1.5">
            <label htmlFor="stepup-password" className="text-sm font-medium text-fg">
              Your password
            </label>
            <Input
              id="stepup-password"
              ref={field}
              type="password"
              autoComplete="off"
              // Tells a manager this is not a credential worth storing. The
              // browser's own heuristics otherwise offer to save it.
              data-1p-ignore
              data-lpignore="true"
              name="step-up-proof"
              invalid={Boolean(error)}
              disabled={busy}
              onChange={(event) => {
                password.current = event.target.value;
                if (error) setError(null);
              }}
              onKeyDown={(event) => {
                if (event.key === "Enter" && !busy) void submit();
              }}
            />
            <p
              aria-live="polite"
              className="flex min-h-4 items-center gap-1.5 text-xs text-status-failed"
            >
              {error ? (
                <>
                  <AlertTriangle aria-hidden className="size-3.5 shrink-0" />
                  {error}
                </>
              ) : null}
            </p>
          </div>

          <Alert tone="warning" title="This is recorded against your name">
            The signature is append-only. It can be revoked, with a reason, but it cannot be
            edited or deleted — a correction is a new signature.
          </Alert>
        </DialogBody>
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button onClick={() => void submit()} disabled={busy}>
            {busy ? <Loader2 aria-hidden className="size-4 animate-spin" /> : null}
            {busy ? "Signing…" : confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
