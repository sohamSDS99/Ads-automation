import { Images, ShieldCheck } from "lucide-react";
import Link from "next/link";

import { buttonVariants } from "@/components/ui/button-variants";
import type { ApprovalItem } from "@/lib/api/approvals";
import { GATE_LABEL } from "@/lib/api/creative";
import { reviewCard } from "@/lib/api/review";

/**
 * G8 / G8b as the approvals inbox and the Creative Console's node panel show
 * them: who they wait on and the way to the review workspace. No decide
 * control here — an asset is decided while looking at it, beside its
 * reference, with its four checks (§15.4 H). One inbox: this is a row in
 * Decisions, not a second queue.
 */
export function ReviewGateSummary({ approval, projectId }: { approval: ApprovalItem; projectId: string }) {
  const count = reviewCard(approval).items?.length ?? 0;
  const assets = `${count} ${count === 1 ? "asset" : "assets"}`;
  return (
    <div className="flex flex-col gap-3 rounded-token border bg-surface-raised p-3">
      <p className="flex items-center gap-2 text-sm font-medium">
        <ShieldCheck className="size-4 text-status-gate" aria-hidden />
        {GATE_LABEL[approval.gate_key] ?? approval.gate_key}
      </p>
      <p className="text-sm text-fg-muted">
        {approval.status === "pending"
          ? `Waiting on ${approval.assignee_email ?? "the brand owner"}. ${assets}, reviewed one at a time with four checks each.`
          : `${assets}, ${approval.status}.`}
      </p>
      <Link
        href={`/projects/${projectId}/creative/runs/${approval.run_id}/review`}
        className={buttonVariants({
          variant: approval.can_decide && approval.status === "pending" ? "primary" : "secondary",
          size: "sm",
        })}
      >
        <Images aria-hidden />
        {approval.can_decide && approval.status === "pending" ? `Review ${assets}` : `Look at the ${assets}`}
      </Link>
    </div>
  );
}
