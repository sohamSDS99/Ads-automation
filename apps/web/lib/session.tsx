"use client";

import { createContext, useCallback, useContext, useMemo, type ReactNode } from "react";

import type { Me } from "@/lib/api";
import type { Permission } from "@/lib/permissions";

type SessionValue = {
  user: Me;
  permissions: ReadonlySet<Permission>;
  /** Whether the signed-in user holds a permission. For rendering only. */
  has: (permission: Permission) => boolean;
};

const SessionContext = createContext<SessionValue | null>(null);

/**
 * Holds the signed-in user for the whole authenticated tree.
 *
 * Seeded on the server from `GET /auth/me`, so the first paint already knows
 * the role and nothing flashes the wrong navigation.
 */
export function SessionProvider({ user, children }: { user: Me; children: ReactNode }) {
  const permissions = useMemo(() => new Set(user.permissions), [user.permissions]);
  const has = useCallback((permission: Permission) => permissions.has(permission), [permissions]);
  const value = useMemo(() => ({ user, permissions, has }), [user, permissions, has]);

  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): SessionValue {
  const value = useContext(SessionContext);
  if (value === null) {
    throw new Error("useSession must be used inside the authenticated layout");
  }
  return value;
}

/**
 * Render children only when the signed-in user holds `permission`.
 *
 * A convenience for the interface, not a security boundary: the server checks
 * the same permission on every route it exposes (PRD §18 law 6).
 */
export function Can({
  permission,
  children,
  fallback = null,
}: {
  permission: Permission;
  children: ReactNode;
  fallback?: ReactNode;
}) {
  return useSession().has(permission) ? <>{children}</> : <>{fallback}</>;
}
