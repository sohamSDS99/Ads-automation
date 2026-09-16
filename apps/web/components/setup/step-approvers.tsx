"use client";

import { Alert } from "@/components/ui/alert";
import { Input } from "@/components/ui/input";
import { Select } from "@/components/ui/select";
import { Skeleton } from "@/components/ui/skeleton";
import type { GateInfo } from "@/lib/api/projects";
import { approverCandidates } from "@/lib/api/users";
import { errorMessage, useUsers } from "@/lib/queries";

export type GateDraft = { assignee_id: string | null; sla_hours: number | null };

/**
 * Step 4 — who decides the three gates.
 *
 * Unassigned is a real choice, not an empty one: the gate then goes to whoever
 * holds the approver role and gets there first. Naming someone makes it their
 * inbox item, which matters when the question is legal and only one person can
 * answer it.
 *
 * There is no auto-approve and no SLA that decides for anyone. The SLA only
 * decides when to remind them (PRD §16).
 */
export function StepApprovers({
  gates,
  drafts,
  onChange,
  disabled,
}: {
  gates: GateInfo[];
  drafts: Record<string, GateDraft>;
  onChange: (drafts: Record<string, GateDraft>) => void;
  disabled: boolean;
}) {
  const users = useUsers();
  const error = errorMessage(users);
  const candidates = approverCandidates(users.data?.users ?? []);

  return (
    <div className="space-y-4">
      {error ? (
        <Alert tone="error" title="Workspace members could not be loaded">
          {error}
        </Alert>
      ) : null}

      {users.data && candidates.length === 0 ? (
        <Alert tone="warning" title="Nobody can decide a gate yet">
          This workspace has no active approver or admin other than the people already listed. A run
          will halt at the first gate and wait. Invite an approver from Settings → Members.
        </Alert>
      ) : null}

      {users.isPending ? <Skeleton className="h-64 w-full" /> : null}

      <ul className="divide-y overflow-hidden rounded-[var(--radius)] border bg-surface-raised">
        {gates.map((gate) => {
          const draft = drafts[gate.node_id] ?? { assignee_id: null, sla_hours: null };
          return (
            <li key={gate.node_id} className="grid gap-3 px-4 py-4 md:grid-cols-[1fr_16rem_8rem]">
              <div className="min-w-0">
                <div className="flex items-baseline gap-2">
                  <span className="font-mono text-xs text-fg-subtle">{gate.stage}</span>
                  <h3 className="font-medium text-fg">{gate.name}</h3>
                </div>
                <p className="mt-1 max-w-prose text-sm text-fg-muted">{gate.description}</p>
                <p className="mt-1 text-xs text-fg-subtle">Usually answered by: {gate.audience}</p>
              </div>

              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor={`assignee-${gate.node_id}`}
                  className="text-xs font-medium text-fg-subtle"
                >
                  Assign to
                </label>
                <Select
                  id={`assignee-${gate.node_id}`}
                  value={draft.assignee_id ?? ""}
                  disabled={disabled}
                  onChange={(event) =>
                    onChange({
                      ...drafts,
                      [gate.node_id]: { ...draft, assignee_id: event.target.value || null },
                    })
                  }
                >
                  <option value="">Any approver</option>
                  {candidates.map((user) => (
                    <option key={user.id} value={user.id}>
                      {user.name} · {user.role}
                    </option>
                  ))}
                </Select>
              </div>

              <div className="flex flex-col gap-1.5">
                <label
                  htmlFor={`sla-${gate.node_id}`}
                  className="text-xs font-medium text-fg-subtle"
                >
                  Remind after
                </label>
                <div className="flex items-center gap-2">
                  <Input
                    id={`sla-${gate.node_id}`}
                    type="number"
                    min={1}
                    max={720}
                    inputMode="numeric"
                    disabled={disabled}
                    value={draft.sla_hours ?? ""}
                    placeholder="—"
                    onChange={(event) =>
                      onChange({
                        ...drafts,
                        [gate.node_id]: {
                          ...draft,
                          sla_hours: event.target.value ? Number(event.target.value) : null,
                        },
                      })
                    }
                    className="w-20"
                  />
                  <span className="text-sm text-fg-muted">hours</span>
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      <p className="text-xs text-fg-subtle">
        A gate is never decided automatically. If nobody answers, the run waits — an admin can
        reassign it.
      </p>
    </div>
  );
}
