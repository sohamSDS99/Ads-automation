"use client";

import { ChevronDown, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import { ApprovalCard } from "@/components/approvals/approval-card";
import { BriefGateSummary } from "@/components/creative/brief-gate-summary";
import { Alert } from "@/components/ui/alert";
import { EmptyState } from "@/components/ui/empty-state";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs } from "@/components/ui/tabs";
import { slaState, type ApprovalFilters, type ApprovalItem } from "@/lib/api/approvals";
import { nodeLabel } from "@/lib/api/runs";
import { absoluteTime, relativeTime } from "@/lib/format";
import { APPROVAL_POLL_MS, errorMessage, useApprovals } from "@/lib/queries";
import { cn } from "@/lib/utils";

/**
 * Every gate waiting on somebody, across every project (PRD §13.4 F).
 *
 * Rows expand in place rather than linking away: the decision needs the
 * proposal and the evidence behind it, and an approver who has to open a run
 * console to answer a yes/no question is an approver who answers it late.
 */
type Scope = "mine" | "open" | "decided";

const SCOPES: Record<Scope, { label: string; filters: ApprovalFilters; empty: string }> = {
  mine: {
    label: "Waiting on you",
    filters: { mine: true, status: "pending" },
    empty: "Nothing waiting on you",
  },
  open: {
    label: "All open gates",
    filters: { status: "pending" },
    empty: "No gate is open",
  },
  decided: {
    label: "Decided",
    filters: { status: "approved" },
    empty: "Nothing decided yet",
  },
};

export function ApprovalsInbox() {
  const [scope, setScope] = useState<Scope>("mine");
  const { filters, empty } = SCOPES[scope];
  const query = useApprovals(filters, { pollMs: APPROVAL_POLL_MS });
  const items = query.data?.items ?? [];

  return (
    <div className="flex flex-col gap-4">
      <Tabs
        label="Approval scope"
        value={scope}
        onChange={(id) => setScope(id as Scope)}
        items={(Object.keys(SCOPES) as Scope[]).map((id) => ({ id, label: SCOPES[id].label }))}
        className="-mx-1"
      />

      {query.isError ? (
        <Alert tone="error" title="The inbox could not be loaded">
          {errorMessage(query)}
        </Alert>
      ) : null}

      {query.isPending ? <Skeleton className="h-24 w-full" /> : null}

      {query.data && items.length === 0 ? (
        <EmptyState
          icon={ShieldCheck}
          title={empty}
          description={
            scope === "mine"
              ? "Three points in every run stop for a person. When one is routed to you it appears here, with the proposal and the evidence behind it."
              : "Gates open while a run is working, and close the moment someone decides them."
          }
        />
      ) : null}

      <ul className="flex flex-col gap-2">
        {items.map((approval) => (
          <li key={approval.id}>
            <InboxRow approval={approval} onDecided={() => void query.refetch()} />
          </li>
        ))}
      </ul>
    </div>
  );
}

function InboxRow({ approval, onDecided }: { approval: ApprovalItem; onDecided: () => void }) {
  const [open, setOpen] = useState(false);
  const sla = slaState(approval);

  return (
    <div
      className={cn(
        "overflow-hidden rounded-[var(--radius)] border bg-surface-raised",
        approval.can_decide && approval.status === "pending" && "border-status-gate",
      )}
    >
      <button
        type="button"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center gap-3 px-4 py-3 text-left transition-colors hover:bg-surface-hover"
      >
        <ShieldCheck
          aria-hidden
          className={cn(
            "size-4 shrink-0",
            approval.status === "pending" ? "text-status-gate" : "text-fg-subtle",
          )}
        />
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-medium text-fg">
            {nodeLabel(approval.node_name)}
          </span>
          <span className="mt-0.5 block truncate text-xs text-fg-subtle">
            {approval.project_name ?? "Unknown project"}
            {" · opened "}
            <time dateTime={approval.created_at} title={absoluteTime(approval.created_at)}>
              {relativeTime(approval.created_at)}
            </time>
            {approval.run_triggered_by_name ? ` · run by ${approval.run_triggered_by_name}` : ""}
          </span>
        </span>
        {sla && approval.status === "pending" ? <SlaChip sla={sla} /> : null}
        {approval.status !== "pending" ? (
          <span className="shrink-0 text-xs text-fg-muted capitalize">{approval.status}</span>
        ) : null}
        <ChevronDown
          aria-hidden
          className={cn("size-4 shrink-0 text-fg-subtle transition-transform", open && "rotate-180")}
        />
      </button>

      {open ? (
        <div className="border-t p-3">
          {/* Stage 04's G7 is decided beside the brief it approves and the
              spend it authorises (§15.4 D), so the inbox hands over to the
              brief page instead of offering a decision without either. */}
          {approval.gate_key === "G7" ? (
            <BriefGateSummary approval={approval} projectId={approval.project_id} />
          ) : (
            <ApprovalCard
              approval={approval}
              onDecided={onDecided}
              titled={false}
              className="border-0"
              header={
                <p className="mt-0.5 text-xs text-fg-subtle">
                  <span className="font-mono">{approval.node_id}</span>
                  {" · "}
                  <Link
                    href={`/projects/${approval.project_id}/runs/${approval.run_id}`}
                    className="text-accent hover:underline"
                  >
                    Open the run console
                  </Link>
                </p>
              }
            />
          )}
        </div>
      ) : null}
    </div>
  );
}

/** How long is left, or how long it is overdue. */
function SlaChip({ sla }: { sla: { hoursLeft: number; late: boolean } }) {
  const hours = Math.abs(sla.hoursLeft);
  const text =
    hours >= 48
      ? `${Math.round(hours / 24)}d`
      : hours >= 1
        ? `${Math.round(hours)}h`
        : `${Math.max(1, Math.round(hours * 60))}m`;
  return (
    <span
      className={cn(
        "shrink-0 rounded-full border px-2 py-0.5 text-xs",
        sla.late ? "border-status-failed text-status-failed" : "border-border text-fg-muted",
      )}
    >
      {sla.late ? `${text} late` : `${text} left`}
    </span>
  );
}
