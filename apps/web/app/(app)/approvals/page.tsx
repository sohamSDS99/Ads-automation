"use client";

import { useState } from "react";

import { ApprovalsInbox } from "@/components/approvals/inbox";
import { TaskInbox } from "@/components/approvals/task-inbox";
import { NoAccess } from "@/components/auth/no-access";
import { Tabs } from "@/components/ui/tabs";
import { APPROVAL_POLL_MS, useApprovals, useHumanTasks } from "@/lib/queries";
import { useSession } from "@/lib/session";

/**
 * `/approvals` — one inbox, two tabs (Stage 03 PRD §15.2).
 *
 * **Decisions** are the gates: the agent proposed and a human confirms, and any
 * holder of the gate's role may do it. **Signatures & attestations** are the
 * person-tasks: one named individual, no role fallback, no administrator
 * override. They look similar and they are not the same thing, which is exactly
 * why they sit side by side rather than mixed into one list.
 *
 * **The page-level guard moved down a level, deliberately.** It used to wrap
 * everything in `approval_decide`, which was right when gates were all this
 * screen held. A person-task assignee holds `claim_sign` / `attest_submit`, and
 * the two sets only overlap today because `approver` happens to carry all
 * three — a role with tasks and no gates would have been locked out of its own
 * inbox. So the page is reachable by anyone signed in and each tab guards
 * itself.
 *
 * A tab with nothing in it still renders and says so. A tab that disappeared
 * would be indistinguishable from a bug, and this is the screen people check
 * when they are waiting on something.
 */
export default function ApprovalsPage() {
  const { has } = useSession();
  const canDecide = has("approval_decide");
  const [tab, setTab] = useState(canDecide ? "decisions" : "signatures");

  // Both counts, summed into one sidebar badge. Each is read independently so
  // a failure in one half cannot silently zero the other.
  const gates = useApprovals(
    { mine: true, status: "pending" },
    { pollMs: APPROVAL_POLL_MS, enabled: canDecide },
  );
  const tasks = useHumanTasks({ mine: true }, { pollMs: APPROVAL_POLL_MS });

  const gateCount = gates.data?.items.length ?? 0;
  const taskCount = tasks.data?.mine_open ?? 0;

  return (
    <div className="mx-auto flex max-w-3xl flex-col gap-6">
      <header>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Approvals</h1>
        <p className="mt-1 max-w-prose text-sm text-fg-muted">
          Two kinds of waiting. A decision is something the agent proposed and a person confirms.
          A signature is something the agent cannot do at all — it belongs to one named person and
          nobody, including an administrator, can perform it for them.
        </p>
      </header>

      <Tabs
        label="Approvals"
        value={tab}
        onChange={setTab}
        items={[
          {
            id: "decisions",
            label: "Decisions",
            badge: canDecide && gateCount > 0 ? gateCount : undefined,
          },
          {
            id: "signatures",
            label: "Signatures & attestations",
            badge: taskCount > 0 ? taskCount : undefined,
          },
        ]}
      />

      {tab === "decisions" ? (
        canDecide ? (
          <ApprovalsInbox />
        ) : (
          <NoAccess permission="approval_decide" what="the decisions inbox" />
        )
      ) : (
        <TaskInbox />
      )}
    </div>
  );
}
