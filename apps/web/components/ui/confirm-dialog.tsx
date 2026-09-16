"use client";

import { useState, type ReactNode } from "react";

import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter } from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";

/**
 * "Are you sure", for the actions where the answer might be no.
 *
 * The body says what will happen in concrete terms — how many sessions get
 * dropped, whose key disappears — because a confirm dialog that only restates
 * the button label teaches people to click through it.
 */
export function ConfirmDialog({
  open,
  onOpenChange,
  title,
  description,
  body,
  confirmLabel,
  destructive = false,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  title: string;
  description?: string;
  body?: ReactNode;
  confirmLabel: string;
  destructive?: boolean;
  onConfirm: () => Promise<void> | void;
}) {
  const [busy, setBusy] = useState(false);

  async function confirm() {
    setBusy(true);
    try {
      await onConfirm();
      onOpenChange(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog open={open} onOpenChange={busy ? undefined : onOpenChange}>
      <DialogContent title={title} description={description}>
        {body ? <DialogBody>{body}</DialogBody> : null}
        <DialogFooter>
          <Button variant="secondary" onClick={() => onOpenChange(false)} disabled={busy}>
            Cancel
          </Button>
          <Button
            onClick={confirm}
            disabled={busy}
            className={destructive ? "bg-status-failed hover:bg-status-failed/90" : undefined}
          >
            {busy ? <Spinner label="Working" /> : null}
            {confirmLabel}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
