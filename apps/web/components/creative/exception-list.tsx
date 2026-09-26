"use client";

import { CircleCheck, CircleDashed, CircleX, Undo2, type LucideIcon } from "lucide-react";
import Link from "next/link";

import { Badge } from "@/components/ui/badge";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { KIND_LABEL, type ExceptionSet, type ExceptionStatus } from "@/lib/api/exceptions";
import { absoluteTime, relativeTime } from "@/lib/format";
import { cn } from "@/lib/utils";

const STATUS: Record<ExceptionStatus, { label: string; icon: LucideIcon; ink: string; tone: "neutral" | "warning" | "danger" }> = {
  open: { label: "Open", icon: CircleDashed, ink: "text-status-gate-ink", tone: "warning" },
  cleared: { label: "Cleared", icon: CircleCheck, ink: "text-status-success", tone: "neutral" },
  rejected: { label: "Rejected", icon: CircleX, ink: "text-status-failed-ink", tone: "danger" },
  withdrawn: { label: "Withdrawn", icon: Undo2, ink: "text-fg-subtle", tone: "neutral" },
};

/**
 * `ExceptionList` — every `CreativeException` of the run and where it stands
 * (Stage 04 PRD §15.4 K): the claim, disclaimer or image right, the assets it
 * holds, and who decided it when. Read-only: H3 is decided by the named legal
 * owner in Approvals, with step-up (law 40) — never here.
 */
export function ExceptionList({ set, nameOf }: { set: ExceptionSet; nameOf: (userId: string) => string | null }) {
  const open = set.exceptions.filter((e) => e.status === "open").length;
  if (set.exceptions.length === 0) {
    return (
      <p className="rounded-token border px-4 py-6 text-sm text-fg-muted" data-testid="exceptions-empty">
        No exceptions. Every claim this run ships was licensed at the pin, so H3 is not required.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-3" data-testid="exception-list">
      {open > 0 ? (
        <p className="text-sm text-fg">
          <span className="font-medium tabular-nums">{open} open</span> — the named legal owner clears them in{" "}
          <Link href="/approvals" className="text-accent hover:underline">
            Approvals, Signatures &amp; attestations
          </Link>
          . Until then they block launch.
        </p>
      ) : null}
      <div className="rounded-token border">
        <Table label="Legal exceptions" className="min-w-0">
          <thead>
            <Tr>
              <Th>Exception</Th>
              <Th>Status</Th>
              <Th className="text-right">Assets</Th>
              <Th className="hidden sm:table-cell">Decided</Th>
            </Tr>
          </thead>
          <tbody>
            {set.exceptions.map((row) => {
              const status = STATUS[row.status];
              const Icon = status.icon;
              const who = row.decided_by ? nameOf(row.decided_by) : null;
              return (
                <Tr key={row.exception_id} data-testid="exception-row" data-status={row.status}>
                  <Td className="w-full max-w-0">
                    <span className="flex min-w-0 items-center gap-2">
                      <Badge className="shrink-0">{KIND_LABEL[row.kind]}</Badge>
                      <span className="truncate" title={row.subject ?? undefined}>
                        {row.subject ? `“${row.subject}”` : "No subject"}
                      </span>
                    </span>
                  </Td>
                  <Td>
                    <Badge tone={status.tone} data-status={row.status}>
                      <Icon className={cn("size-3", status.ink)} aria-hidden />
                      {status.label}
                    </Badge>
                  </Td>
                  <Td className="text-right tabular-nums">{row.asset_ids.length}</Td>
                  <Td className="hidden text-fg-muted sm:table-cell">
                    {row.decided_at ? (
                      <>
                        {who ?? "Someone"} ·{" "}
                        <time dateTime={row.decided_at} title={absoluteTime(row.decided_at)}>
                          {relativeTime(row.decided_at)}
                        </time>
                      </>
                    ) : (
                      "—"
                    )}
                  </Td>
                </Tr>
              );
            })}
          </tbody>
        </Table>
      </div>
    </div>
  );
}
