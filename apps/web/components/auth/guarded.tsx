"use client";

import type { ReactNode } from "react";

import { NoAccess } from "@/components/auth/no-access";
import type { Permission } from "@/lib/permissions";
import { useSession } from "@/lib/session";

/**
 * `<Can>` with the refusal drawn in.
 *
 * Use this where the permission gates a region of the page that would otherwise
 * be blank; use `<Can>` where the right answer is to show nothing at all, like
 * a button in a toolbar.
 */
export function Guarded({
  permission,
  what,
  children,
}: {
  permission: Permission;
  what?: string;
  children: ReactNode;
}) {
  const { has, user } = useSession();
  if (!has(permission)) return <NoAccess permission={permission} role={user.role} what={what} />;
  return <>{children}</>;
}
