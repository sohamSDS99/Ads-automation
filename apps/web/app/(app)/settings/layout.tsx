"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import type { Permission } from "@/lib/permissions";
import { useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

/**
 * Everything that configures anything, behind one tab strip.
 *
 * There used to be two places. This screen owned the workspace, and a
 * five-step wizard on each project owned the rest — so setting a project up
 * meant first working out which of the two owned the thing you wanted to
 * change, and the same source card was rendered on three different screens
 * because nobody could decide. One strip of tabs, one card language, and the
 * answer to "where do I change X" is always Settings.
 *
 * The tabs are guarded only where the endpoint behind them would refuse.
 * Every workspace tab is readable by any member —
 * `GET /connections`, `/workspace`, `/projects` and `/users` all need `read`
 * and nothing more — so someone without the write permission sees the real
 * configuration with the controls disabled and a line saying who can change
 * it, which is more use than a refusal. The audit log and the two
 * installation-wide tabs are hidden outright, because their endpoints are the
 * ones that would refuse (PRD §18 law 6 still applies: every route re-checks
 * regardless of what this renders).
 */
const TABS = [
  { href: "/settings", label: "Workspace" },
  { href: "/settings/connections", label: "Connections" },
  { href: "/settings/models", label: "Models" },
  { href: "/settings/context", label: "Business context" },
  { href: "/settings/approvers", label: "Approvers" },
  { href: "/settings/team", label: "Team" },
  { href: "/settings/audit", label: "Audit log", permission: "audit_read" },
  // The last two are the installation rather than this workspace, and only
  // the system administrator holds `platform_admin`. They sit at the end
  // because most people will never see them.
  { href: "/settings/workspaces", label: "Workspaces", permission: "platform_admin" },
  { href: "/settings/accounts", label: "Accounts", permission: "platform_admin" },
] as const satisfies readonly { href: string; label: string; permission?: Permission }[];

/** The two tabs that are about the installation rather than this workspace. */
const INSTALLATION_TABS = new Set(["/settings/workspaces", "/settings/accounts"]);

export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const { has } = useSession();
  const tabs = TABS.filter((tab) => !("permission" in tab) || has(tab.permission));
  // "Workspace settings" over a table of every workspace on the installation
  // is a heading that contradicts what is under it.
  const heading = INSTALLATION_TABS.has(pathname) ? "Installation settings" : "Workspace settings";

  return (
    <div className="mx-auto flex w-full max-w-5xl flex-col gap-7">
      <header>
        <p className="flex items-center gap-2 text-xs font-medium tracking-[0.14em] text-fg-subtle uppercase">
          <span className="size-1.5 rounded-full bg-accent" aria-hidden />
          Settings
        </p>
        <h1 className="mt-3 text-[length:var(--text-2xl)] font-semibold tracking-tight">
          {heading}
        </h1>
      </header>

      <nav aria-label="Settings sections" className="border-b">
        {/* Scrolls rather than wraps: seven tabs do not fit 390px — nine even
            less — and a tab nobody can reach is worse than one they have to
            swipe to. */}
        <ul className="-mb-px flex gap-5 overflow-x-auto [scrollbar-width:none] sm:gap-6">
          {tabs.map((tab) => {
            const active = pathname === tab.href;
            return (
              <li key={tab.href}>
                <Link
                  href={tab.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "inline-flex h-10 shrink-0 items-center whitespace-nowrap border-b-2 text-[length:var(--text-md)] transition-colors",
                    active
                      ? "border-accent font-semibold text-fg"
                      : "border-transparent text-fg-muted hover:text-fg",
                  )}
                >
                  {tab.label}
                </Link>
              </li>
            );
          })}
        </ul>
      </nav>

      {children}
    </div>
  );
}
