"use client";

import { HealthDot } from "@/components/shell/health-dot";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { UserMenu } from "@/components/shell/user-menu";
import { useSession } from "@/lib/session";

export function Topbar() {
  const { user } = useSession();

  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b bg-surface px-4">
      <div className="flex min-w-0 items-center gap-3">
        <HealthDot />
        <span className="hidden truncate text-sm text-fg-muted md:inline">
          {user.workspace_name}
        </span>
      </div>
      <div className="flex items-center gap-1">
        <ThemeToggle />
        <UserMenu />
      </div>
    </header>
  );
}
