import { ROLE_LABEL, type Role } from "@/lib/permissions";
import { cn } from "@/lib/utils";

/**
 * A role, stated quietly.
 *
 * No colour coding: the palette's colours already mean run states, and reusing
 * them here would make "approver" look like "needs approval".
 */
export function RoleBadge({ role, className }: { role: Role; className?: string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full border px-2 py-0.5",
        "text-xs font-medium text-fg-muted",
        className,
      )}
    >
      {ROLE_LABEL[role]}
    </span>
  );
}
