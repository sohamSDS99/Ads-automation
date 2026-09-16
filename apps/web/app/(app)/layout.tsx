import { redirect } from "next/navigation";
import type { ReactNode } from "react";

import { AppShell } from "@/components/shell/app-shell";
import { SessionProvider } from "@/lib/session";
import { getServerSession } from "@/lib/server-session";

/**
 * Everything behind the sign-in wall.
 *
 * The session is resolved on the server before anything renders, so the first
 * paint already has the right role and the navigation never flickers between
 * permission sets. `middleware.ts` has usually redirected already; this catches
 * the case it cannot see — a cookie that is present but revoked, expired, or
 * belongs to a user who has since been disabled.
 */
export default async function AuthenticatedLayout({ children }: { children: ReactNode }) {
  const user = await getServerSession();
  if (user === null) redirect("/login");

  return (
    <SessionProvider user={user}>
      <AppShell>{children}</AppShell>
    </SessionProvider>
  );
}
