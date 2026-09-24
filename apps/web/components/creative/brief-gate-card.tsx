"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Ban, CircleCheck, CircleX, Hourglass, PencilLine, ShieldCheck } from "lucide-react";
import { useEffect, useState } from "react";

import { MonoId } from "@/components/creative/mono-id";
import { Alert } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Textarea } from "@/components/ui/textarea";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { decideApproval, type ApprovalItem } from "@/lib/api/approvals";
import { GATE_LABEL } from "@/lib/api/creative";
import {
  applyBriefEdits,
  editableLines,
  type BriefAuthorisation,
  type BriefEdits,
  type CreativeBriefView,
} from "@/lib/api/creative-runs";
import { absoluteTime, relativeTime, usd } from "@/lib/format";
import { keys } from "@/lib/queries";

function count(value: number, one: string, many: string): string {
  return `${value} ${value === 1 ? one : many}`;
}

/**
 * "Authorises 20 RSAs, 36 images, 4 videos and up to $38.40 of media spend"
 * (§15.4 D, §15.2 rule 8). Every figure is G7's own, from the server; this
 * only puts them in a sentence, leaving out the kinds of work there are none of.
 */
export function authorisationSentence(authorises: BriefAuthorisation, tense: "present" | "past"): string {
  const verb = tense === "present" ? "Authorises" : "Authorised";
  const parts = [
    authorises.rsas > 0 ? count(authorises.rsas, "RSA", "RSAs") : null,
    authorises.images > 0 ? count(authorises.images, "image", "images") : null,
    authorises.videos > 0 ? count(authorises.videos, "video", "videos") : null,
  ].filter((part): part is string => part !== null);
  const media = Number(authorises.media_usd);
  const spend =
    media > 0 ? `up to ${usd(authorises.media_usd)} of media spend` : "no media spend";
  if (parts.length === 0) return `${verb} the plan of work and ${spend}`;
  return `${verb} ${parts.join(", ")} and ${spend}`;
}

/**
 * `BriefGateCard` — G7, for the performance owner (Stage 04 PRD §15.4 D).
 *
 * A card because it holds a decision (§15.2 rule 4). It says what approving
 * authorises in numbers before anything is committed, shows the hash the
 * approval is scoped to, and — only for whoever the approvals surface says
 * may decide (`can_decide`) — offers `Approve brief`, `Reject with note` and
 * `Edit`. For anyone else those controls are absent, not disabled (§15.5
 * item 4); the api refuses them regardless.
 *
 * An edit is the words of the brief's lines, sent with the approval as
 * `edited_proposal`: the server revalidates it against `CreativeBrief`,
 * refuses anything code wrote, and re-hashes it, and a refusal is shown here
 * in the server's own words.
 */
export function BriefGateCard({
  view,
  approval,
  edits,
  onEdit,
  onDiscard,
}: {
  view: CreativeBriefView;
  approval: ApprovalItem | null;
  edits: BriefEdits | null;
  onEdit: () => void;
  onDiscard: () => void;
}) {
  const queryClient = useQueryClient();
  const [rejecting, setRejecting] = useState(false);
  const [note, setNote] = useState("");
  const [refusal, setRefusal] = useState<string | null>(null);

  // A refusal names what was wrong with the edit that was sent; once the
  // approver changes a line it no longer describes what is on screen.
  useEffect(() => setRefusal(null), [edits]);

  const pending = approval?.status === "pending";
  const deciding = Boolean(approval && pending && approval.can_decide);
  const changed = changedEdits(view, edits);

  const decide = useMutation({
    mutationFn: (decision: "approve" | "reject") => {
      if (!approval) throw new Error("No G7 gate to decide.");
      return decideApproval(approval.id, {
        decision,
        note: note.trim() || undefined,
        edited_proposal:
          decision === "approve" && changed
            ? applyBriefEdits(approval.proposal, changed)
            : undefined,
      });
    },
    onSuccess: async (_result, decision) => {
      setRefusal(null);
      setRejecting(false);
      onDiscard();
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["approvals"] }),
        queryClient.invalidateQueries({ queryKey: keys.run(view.run_id) }),
      ]);
      toast.success(decision === "approve" ? "Brief approved" : "Brief rejected", {
        description:
          decision === "approve"
            ? `${authorisationSentence(view.authorises, "past")}. The run carries on from here.`
            : "The run stops at G7. A new run can take the brief from the start.",
      });
    },
    onError: (error) => {
      setRefusal(
        error instanceof ApiError
          ? error.detail
          : "The decision did not reach the server. Check your connection and try again.",
      );
    },
  });

  return (
    <section
      aria-labelledby="g7-title"
      className="flex flex-col gap-4 rounded-token border bg-surface-raised p-4"
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <h2 id="g7-title" className="flex items-center gap-2 text-sm font-semibold">
          <ShieldCheck className="size-4 text-status-gate" aria-hidden />
          {GATE_LABEL.G7}
        </h2>
        <GateStatus approval={approval} />
      </header>

      <p className="text-lg font-semibold leading-snug tracking-tight">
        {authorisationSentence(view.authorises, approval?.status === "approved" ? "past" : "present")}.
      </p>

      <dl className="flex flex-col gap-2 text-sm">
        <div className="flex items-center justify-between gap-3">
          <dt className="text-fg-muted">{view.approved_hash ? "Approved hash" : "Brief hash"}</dt>
          <dd>
            <MonoId value={view.approved_hash ?? view.brief_hash} label="brief hash" />
          </dd>
        </div>
        <Decider approval={approval} />
      </dl>

      {refusal ? (
        <Alert tone="error" title="The server refused this decision">
          {refusal}
        </Alert>
      ) : null}

      {deciding ? (
        rejecting ? (
          <div className="flex flex-col gap-2">
            <Textarea
              value={note}
              onChange={(event) => setNote(event.target.value)}
              placeholder="What has to change before this brief can be approved"
              aria-label="Why the brief is rejected"
              rows={3}
              autoFocus
            />
            <div className="flex flex-wrap gap-2">
              <Button
                variant="secondary"
                onClick={() => decide.mutate("reject")}
                disabled={decide.isPending || note.trim().length === 0}
                title={note.trim().length === 0 ? "Say why before rejecting" : undefined}
              >
                {decide.isPending ? <Spinner label="Rejecting" /> : <CircleX aria-hidden />}
                Reject brief
              </Button>
              <Button variant="ghost" onClick={() => setRejecting(false)} disabled={decide.isPending}>
                Keep reviewing
              </Button>
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-2">
            <Button onClick={() => decide.mutate("approve")} disabled={decide.isPending}>
              {decide.isPending ? <Spinner label="Approving" /> : <CircleCheck aria-hidden />}
              {changed ? "Approve edited brief" : "Approve brief"}
            </Button>
            {edits !== null ? (
              <>
                <p className="text-xs text-fg-muted">
                  {changed
                    ? `${count(Object.keys(changed).length, "line", "lines")} edited. Approving revalidates the brief, recounts its words and stamps a new hash.`
                    : "Edit the lines in the brief. Plan facts, sources and the media plan are written by code and stay as they are."}
                </p>
                <Button variant="ghost" onClick={onDiscard} disabled={decide.isPending}>
                  Discard edits
                </Button>
              </>
            ) : (
              <div className="flex flex-wrap gap-2">
                <Button variant="secondary" onClick={() => setRejecting(true)} className="flex-1">
                  <Ban aria-hidden />
                  Reject with note
                </Button>
                <Button variant="ghost" onClick={onEdit} className="flex-1">
                  <PencilLine aria-hidden />
                  Edit
                </Button>
              </div>
            )}
          </div>
        )
      ) : null}
    </section>
  );
}

/** Only the lines whose words actually changed; an untouched field is not an edit. */
function changedEdits(view: CreativeBriefView, edits: BriefEdits | null): BriefEdits | null {
  if (!edits) return null;
  const original = new Map(editableLines(view.brief));
  const changed = Object.fromEntries(
    Object.entries(edits).filter(([path, text]) => original.get(path) !== text),
  );
  return Object.keys(changed).length > 0 ? changed : null;
}

function GateStatus({ approval }: { approval: ApprovalItem | null }) {
  if (!approval) return <Badge>No gate open</Badge>;
  switch (approval.status) {
    case "pending":
      return (
        <Badge tone="accent">
          <Hourglass className="size-3" aria-hidden />
          Waiting for sign-off
        </Badge>
      );
    case "approved":
      return (
        <Badge>
          <CircleCheck className="size-3 text-status-success" aria-hidden />
          Approved
        </Badge>
      );
    case "rejected":
      return (
        <Badge tone="danger">
          <CircleX className="size-3 text-status-failed" aria-hidden />
          Rejected
        </Badge>
      );
    case "expired":
      return <Badge tone="warning">Expired</Badge>;
  }
}

function Decider({ approval }: { approval: ApprovalItem | null }) {
  if (!approval) {
    return (
      <div>
        <dt className="sr-only">Gate</dt>
        <dd className="text-fg-muted">
          No G7 gate is open for this brief, so nothing can approve it here.
        </dd>
      </div>
    );
  }
  if (approval.status === "pending") {
    return (
      <div className="flex items-center justify-between gap-3">
        <dt className="text-fg-muted">Waiting on</dt>
        <dd className="truncate text-fg">{approval.assignee_email ?? "Any approver"}</dd>
      </div>
    );
  }
  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <dt className="text-fg-muted">Decided</dt>
        <dd className="text-fg">
          <time dateTime={approval.decided_at ?? undefined} title={absoluteTime(approval.decided_at)}>
            {relativeTime(approval.decided_at)}
          </time>
        </dd>
      </div>
      {approval.decision_note ? (
        <div className="flex flex-col gap-1">
          <dt className="text-fg-muted">Note</dt>
          <dd className="text-fg">{approval.decision_note}</dd>
        </div>
      ) : null}
    </>
  );
}
