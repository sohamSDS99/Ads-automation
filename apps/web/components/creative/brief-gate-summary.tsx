import { FileText, ShieldCheck } from "lucide-react";
import Link from "next/link";

import { buttonVariants } from "@/components/ui/button-variants";
import type { ApprovalItem } from "@/lib/api/approvals";
import { GATE_LABEL } from "@/lib/api/creative";

/**
 * G7 as the Creative Console's node panel shows it: who it waits on, and the
 * way to the brief page. Deliberately no decide control here — approving
 * authorises spend, and the numbers it authorises and the brief itself are on
 * that page (§15.2 rule 8: consequences before commitment).
 */
export function BriefGateSummary({
  approval,
  projectId,
}: {
  approval: ApprovalItem;
  projectId: string;
}) {
  return (
    <div className="flex flex-col gap-3 rounded-token border bg-surface-raised p-3">
      <p className="flex items-center gap-2 text-sm font-medium">
        <ShieldCheck className="size-4 text-status-gate" aria-hidden />
        {GATE_LABEL.G7}
      </p>
      <p className="text-sm text-fg-muted">
        Waiting on {approval.assignee_email ?? "an approver"}. The brief, what approving it
        authorises, and the decision are on the brief page.
      </p>
      <Link
        href={`/projects/${projectId}/creative/runs/${approval.run_id}/brief`}
        className={buttonVariants({
          variant: approval.can_decide ? "primary" : "secondary",
          size: "sm",
        })}
      >
        <FileText aria-hidden />
        {approval.can_decide ? "Review and decide the brief" : "Read the brief"}
      </Link>
    </div>
  );
}
