"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Pencil, ShieldCheck, X } from "lucide-react";
import { useState } from "react";

import { EvidenceCard } from "@/components/run/node-evidence";
import { Button } from "@/components/ui/button";
import { JsonTree } from "@/components/ui/json-tree";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import {
  currentProposal,
  decideApproval,
  type ApprovalItem,
} from "@/lib/api/approvals";
import { nodeLabel } from "@/lib/api/runs";
import { absoluteTime, relativeTime } from "@/lib/format";
import { useEvidence } from "@/lib/queries";
import { cn } from "@/lib/utils";

/**
 * One gate, and the decision it is waiting for (PRD §13.4 B and F).
 *
 * The same component in the run console and in the inbox, deliberately: a
 * second implementation would be a second set of rules about who may act, and
 * the two would drift. Whether the buttons render at all is `can_decide`, which
 * the server computes — the API re-checks it regardless (PRD §18 law 6).
 */
export function ApprovalCard({
  approval,
  onDecided,
  header,
  titled = true,
  className,
}: {
  approval: ApprovalItem;
  onDecided: () => void;
  /** The inbox says which project and run this belongs to; the console knows. */
  header?: React.ReactNode;
  /** Off in the inbox, where the row a person just expanded already says it. */
  titled?: boolean;
  className?: string;
}) {
  const queryClient = useQueryClient();
  const proposal = currentProposal(approval);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(() => JSON.stringify(proposal, null, 2));
  const [note, setNote] = useState("");
  const pending = approval.status === "pending";

  const edited = parseEdit(draft, proposal);

  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") =>
      decideApproval(approval.id, {
        decision,
        note: note.trim() || undefined,
        edited_proposal: decision === "approve" && editing && edited.value ? edited.value : undefined,
      }),
    onSuccess: async (result, decision) => {
      await queryClient.invalidateQueries({ queryKey: ["approvals"] });
      await queryClient.invalidateQueries({ queryKey: ["runs", approval.run_id] });
      toast.success(decision === "approve" ? "Approved" : "Rejected", {
        description: result.resumed
          ? "The run picked up where it stopped."
          : `The run is ${result.run_status.replace(/_/g, " ")}.`,
      });
      onDecided();
    },
    onError: (error) => {
      toast.error("That decision did not go through", {
        description: error instanceof ApiError ? error.detail : "Try again in a moment.",
      });
    },
  });

  return (
    <section
      className={cn(
        "rounded-[var(--radius)] border bg-surface-raised",
        pending ? "border-status-gate" : "border-border",
        className,
      )}
      aria-label={`Approval for ${nodeLabel(approval.node_name)}`}
    >
      <header className="flex flex-wrap items-start justify-between gap-2 border-b px-3.5 py-3">
        <div className="min-w-0">
          {titled ? (
            <h3 className="flex items-center gap-1.5 text-[length:var(--text-md)] font-medium tracking-tight">
              <ShieldCheck className="size-4 shrink-0 text-status-gate" aria-hidden />
              {nodeLabel(approval.node_name)}
            </h3>
          ) : null}
          {header ?? (
            <p className="mt-0.5 text-xs text-fg-subtle">
              <span className="font-mono">{approval.node_id}</span> · opened{" "}
              <time dateTime={approval.created_at} title={absoluteTime(approval.created_at)}>
                {relativeTime(approval.created_at)}
              </time>
            </p>
          )}
        </div>
        <Waiting approval={approval} />
      </header>

      <div className="space-y-3 px-3.5 py-3">
        <div>
          <div className="mb-1.5 flex items-center justify-between gap-2">
            <h4 className="text-xs font-medium tracking-wide text-fg-muted uppercase">
              What the agent proposes
            </h4>
            {pending && approval.can_decide ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setEditing(!editing)}
                aria-pressed={editing}
              >
                <Pencil aria-hidden />
                {editing ? "Stop editing" : "Edit"}
              </Button>
            ) : null}
          </div>

          {editing ? (
            <div className="space-y-1.5">
              <Textarea
                value={draft}
                onChange={(event) => setDraft(event.target.value)}
                rows={14}
                spellCheck={false}
                aria-label="Proposal"
                invalid={edited.error !== null}
                className="font-mono text-xs"
              />
              <p className={cn("text-xs", edited.error ? "text-status-failed" : "text-fg-subtle")}>
                {edited.error ??
                  "Approving sends this edited version — everything downstream reads it, not the draft."}
              </p>
            </div>
          ) : (
            <div className="max-h-72 overflow-y-auto rounded-[var(--radius)] border bg-surface p-2.5">
              <JsonTree value={proposal} />
            </div>
          )}
        </div>

        <ProposalEvidence approval={approval} />

        {pending && approval.can_decide ? (
          <div className="space-y-2">
            <Textarea
              value={note}
              onChange={(event) => setNote(event.target.value)}
              rows={2}
              placeholder="Why — recorded in the audit log. Required when rejecting."
              aria-label="Decision note"
            />
            <div className="flex flex-wrap justify-end gap-2">
              <Button
                variant="secondary"
                onClick={() => decide.mutate("reject")}
                disabled={decide.isPending || note.trim().length === 0}
                title={note.trim().length === 0 ? "Say why before rejecting" : undefined}
              >
                {decide.isPending && decide.variables === "reject" ? (
                  <Spinner label="Rejecting" />
                ) : (
                  <X aria-hidden />
                )}
                Reject
              </Button>
              <Button
                onClick={() => decide.mutate("approve")}
                disabled={decide.isPending || (editing && edited.error !== null)}
              >
                {decide.isPending && decide.variables === "approve" ? (
                  <Spinner label="Approving" />
                ) : (
                  <Check aria-hidden />
                )}
                {editing ? "Approve with changes" : "Approve"}
              </Button>
            </div>
          </div>
        ) : null}

        {!pending ? <Decided approval={approval} /> : null}
      </div>
    </section>
  );
}

function Waiting({ approval }: { approval: ApprovalItem }) {
  if (approval.status !== "pending") return null;
  if (approval.can_decide) {
    return <span className="text-xs font-medium text-status-gate">Waiting on you</span>;
  }
  return (
    <span className="text-xs text-fg-muted">
      Waiting on {approval.assignee_email ?? `any ${approval.required_role}`}
    </span>
  );
}

function Decided({ approval }: { approval: ApprovalItem }) {
  const verdict =
    approval.status === "approved"
      ? "Approved"
      : approval.status === "rejected"
        ? "Rejected"
        : "Expired";
  return (
    <div className="rounded-[var(--radius)] border bg-surface px-3 py-2 text-sm">
      <p className="font-medium text-fg">
        {verdict}
        {approval.decided_at ? (
          <>
            {" "}
            <time
              className="font-normal text-fg-muted"
              dateTime={approval.decided_at}
              title={absoluteTime(approval.decided_at)}
            >
              {relativeTime(approval.decided_at)}
            </time>
          </>
        ) : null}
      </p>
      {approval.decision_note ? (
        <p className="mt-1 text-fg-muted">{approval.decision_note}</p>
      ) : null}
    </div>
  );
}

/** The rows behind the proposal, when the gate node cited any. */
function ProposalEvidence({ approval }: { approval: ApprovalItem }) {
  const ids = citedIds(approval.proposal);
  const query = useEvidence({ project_id: approval.project_id, ids, limit: 50 }, ids.length > 0);
  const items = query.data?.pages.flatMap((page) => page.items) ?? [];
  if (items.length === 0) return null;
  return (
    <div>
      <h4 className="mb-1.5 text-xs font-medium tracking-wide text-fg-muted uppercase">
        What it is based on
      </h4>
      <div className="max-h-64 space-y-2 overflow-y-auto">
        {items.map((item) => (
          <EvidenceCard key={item.id} item={item} projectId={approval.project_id} />
        ))}
      </div>
    </div>
  );
}

function citedIds(proposal: Record<string, unknown>): string[] {
  const ids = proposal.evidence_ids;
  return Array.isArray(ids) ? ids.filter((id): id is string => typeof id === "string") : [];
}

/**
 * The edited proposal, or why it cannot be sent.
 *
 * Validated as the person types rather than on submit: a decision that bounces
 * off a JSON error after the click is a decision the run has already waited on.
 */
function parseEdit(
  draft: string,
  fallback: Record<string, unknown>,
): { value: Record<string, unknown> | null; error: string | null } {
  try {
    const parsed: unknown = JSON.parse(draft);
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      return { value: null, error: "The proposal has to stay an object." };
    }
    return { value: parsed as Record<string, unknown>, error: null };
  } catch {
    void fallback;
    return { value: null, error: "That is not valid JSON yet." };
  }
}
