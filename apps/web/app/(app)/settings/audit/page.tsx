"use client";

import { ScrollText } from "lucide-react";
import { useState } from "react";

import { Guarded } from "@/components/auth/guarded";
import { Alert } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Card, CardBody } from "@/components/ui/card";
import { EmptyState } from "@/components/ui/empty-state";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import { Table, Td, Th, Tr } from "@/components/ui/table";
import { actionLabel, AUDIT_ACTIONS, type AuditEntry, type AuditFilters } from "@/lib/api/audit";
import { absoluteTime, relativeTime } from "@/lib/format";
import { errorMessage, useAudit } from "@/lib/queries";

/**
 * Every mutating action, with the person who took it (PRD §15 NF5c).
 *
 * Read-only by construction: the table is append-only in Postgres, and there is
 * no endpoint that edits a row. Filters narrow it; nothing here changes it.
 */
export default function AuditPage() {
  // Guarded here rather than in the layout: every other settings tab is
  // readable by any member and only shows the write controls to those who
  // have them, but `GET /audit` needs `audit_read` and answers 403 without
  // it — which would render as an empty table rather than as a refusal.
  return (
    <Guarded permission="audit_read" what="the audit log">
      <AuditTable />
    </Guarded>
  );
}

function AuditTable() {
  const [filters, setFilters] = useState<AuditFilters>({});
  const audit = useAudit(filters);
  const error = errorMessage(audit);
  const entries = audit.data?.entries ?? [];

  function set(key: keyof AuditFilters, value: string) {
    setFilters((current) => {
      const next = { ...current };
      if (value) next[key] = value;
      else delete next[key];
      // A new filter starts a new page — carrying the old cursor would page
      // through the previous query's results.
      delete next.cursor;
      return next;
    });
  }

  return (
    <div className="flex flex-col gap-5">
      {error ? (
        <Alert tone="error" title="The audit log could not be loaded">
          {error}
        </Alert>
      ) : null}

      <Card>
        <CardBody className="flex flex-wrap items-end gap-3">
          <div className="flex flex-col gap-1.5">
            <label htmlFor="audit-actor" className="text-xs font-medium text-fg-subtle">
              Actor
            </label>
            <Input
              id="audit-actor"
              value={filters.actor ?? ""}
              placeholder="name@company.com"
              onChange={(event) => set("actor", event.target.value)}
              className="w-56"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="audit-action" className="text-xs font-medium text-fg-subtle">
              Action
            </label>
            <Select
              id="audit-action"
              value={filters.action ?? ""}
              onChange={(event) => set("action", event.target.value)}
              className="w-56"
            >
              <option value="">Everything</option>
              {AUDIT_ACTIONS.map((action) => (
                <option key={action} value={action}>
                  {actionLabel(action)}
                </option>
              ))}
            </Select>
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="audit-from" className="text-xs font-medium text-fg-subtle">
              From
            </label>
            <Input
              id="audit-from"
              type="date"
              value={filters.from ?? ""}
              onChange={(event) => set("from", event.target.value)}
              className="w-44"
            />
          </div>
          <div className="flex flex-col gap-1.5">
            <label htmlFor="audit-to" className="text-xs font-medium text-fg-subtle">
              To
            </label>
            <Input
              id="audit-to"
              type="date"
              value={filters.to ?? ""}
              onChange={(event) => set("to", event.target.value)}
              className="w-44"
            />
          </div>
          {Object.keys(filters).length ? (
            <Button variant="ghost" size="sm" onClick={() => setFilters({})}>
              Clear
            </Button>
          ) : null}
        </CardBody>
      </Card>

      {audit.isPending ? <Skeleton className="h-64 w-full" /> : null}

      {audit.data && entries.length === 0 ? (
        <EmptyState
          icon={ScrollText}
          title="Nothing matches"
          description="No recorded action fits these filters. Clear them to see the whole log."
        />
      ) : null}

      {entries.length > 0 ? (
        <Card className="overflow-hidden">
          <Table label="Audit log">
            <thead>
              <tr>
                <Th>When</Th>
                <Th>Who</Th>
                <Th>Did what</Th>
                <Th>To</Th>
                <Th>From</Th>
              </tr>
            </thead>
            <tbody>
              {entries.map((entry) => (
                <AuditRow key={entry.id} entry={entry} />
              ))}
            </tbody>
          </Table>
          {audit.data?.next_cursor ? (
            <div className="flex justify-center border-t px-4 py-3">
              <Button
                variant="secondary"
                size="sm"
                onClick={() =>
                  setFilters((current) => ({ ...current, cursor: audit.data?.next_cursor ?? undefined }))
                }
              >
                Load older
              </Button>
            </div>
          ) : null}
        </Card>
      ) : null}
    </div>
  );
}

function AuditRow({ entry }: { entry: AuditEntry }) {
  return (
    <Tr>
      <Td className="whitespace-nowrap text-fg-muted" title={absoluteTime(entry.created_at)}>
        {relativeTime(entry.created_at)}
      </Td>
      <Td className="whitespace-nowrap">
        {entry.actor_email ?? <span className="text-fg-subtle">System</span>}
      </Td>
      <Td className="whitespace-nowrap">{actionLabel(entry.action)}</Td>
      <Td className="whitespace-nowrap text-fg-muted">
        {entry.target_type}
        {entry.target_id ? (
          <span className="ml-1.5 font-mono text-xs text-fg-subtle">
            {entry.target_id.slice(0, 8)}
          </span>
        ) : null}
      </Td>
      <Td className="whitespace-nowrap font-mono text-xs text-fg-subtle">{entry.ip ?? "—"}</Td>
    </Tr>
  );
}
