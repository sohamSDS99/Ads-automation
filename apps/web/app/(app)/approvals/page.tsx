"use client";

import { ShieldCheck } from "lucide-react";

import { Guarded } from "@/components/auth/guarded";
import { EmptyState } from "@/components/ui/empty-state";

/**
 * The approvals inbox is built in the next phase, along with the gate
 * mechanics it lists. The route exists now so the navigation an approver signs
 * in to is not a dead link — an empty destination is a truthful answer, a 404
 * is not.
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
        <EmptyState
          icon={ShieldCheck}
          title="Nothing waiting on you"
          description="Gates arrive here when a run reaches one. Deciding them, and the run console behind them, ships in the next phase — you can already be assigned to a gate from a project's setup screen."
        />
      </Guarded>
    </div>
  );
}
