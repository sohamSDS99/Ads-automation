import { Lock } from "lucide-react";

import { ROLE_LABEL, type Permission, type Role } from "@/lib/permissions";

/**
 * What a 403 looks like in place.
 *
 * Rendered where the content would have been rather than as a whole-page error,
 * so the rest of the screen keeps working and the person can still see what
 * they do have access to.
 */
export function NoAccess({
  permission,
  role,
  what = "this",
}: {
  permission?: Permission;
  role?: Role;
  what?: string;
}) {
  return (
    <div className="flex flex-col items-center justify-center gap-3 rounded-[var(--radius)] border border-dashed bg-surface px-6 py-12 text-center">
      <Lock className="size-5 text-fg-subtle" aria-hidden />
      <h2 className="text-[length:var(--text-md)] font-medium text-fg">
        You don&apos;t have access to {what}
      </h2>
      <p className="max-w-sm text-sm text-fg-muted">
        {role ? `Your role is ${ROLE_LABEL[role]}. ` : ""}
        Ask a workspace admin if you need
        {permission ? ` the ${permission.replace(/_/g, " ")} permission` : " access"}.
      </p>
    </div>
  );
}
