"use client";

import { ApprovalsInbox } from "@/components/approvals/inbox";
import { Guarded } from "@/components/auth/guarded";

/**
 * `/approvals` — the cross-project approvals inbox (PRD §13.3, §13.4 F).
 *
 * Reachable by anyone holding `approval_decide`; the rows themselves say
 * whether this particular person may act on them, because a gate can be
 * assigned to one approver while another is looking at it.
 */
export default function ApprovalsPage() {
  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <header>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Approvals</h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Three points in every run stop and wait for a person: the claims we may make, the one
          thing we will say that competitors do not, and which audience lists we may use.
        </p>
      </header>

      <Guarded permission="approval_decide" what="the approvals inbox">
        <ApprovalsInbox />
      </Guarded>
    </div>
  );
}
