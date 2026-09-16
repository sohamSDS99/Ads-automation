"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import { Guarded } from "@/components/auth/guarded";
import { cn } from "@/lib/utils";

const TABS = [
  { href: "/settings", label: "Workspace" },
  { href: "/settings/members", label: "Members" },
  { href: "/settings/audit", label: "Audit log" },
];

/**
 * The admin area.
 *
 * Guarded once here rather than on each page: all three need the same
 * permission, and a tab strip that leads to a refusal is worse than no tab.
 */
export default function SettingsLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-6">
      <header>
        <h1 className="text-[length:var(--text-xl)] font-semibold tracking-tight">Settings</h1>
        <p className="mt-1 text-sm text-fg-muted">
          One workspace, its people, and the record of what they did.
        </p>
      </header>

      <nav aria-label="Settings sections" className="border-b">
        <ul className="-mb-px flex gap-1">
          {TABS.map((tab) => {
            const active = pathname === tab.href;
            return (
              <li key={tab.href}>
                <Link
                  href={tab.href}
                  aria-current={active ? "page" : undefined}
                  className={cn(
                    "inline-flex h-10 items-center border-b-2 px-3 text-sm transition-colors",
                    active
                      ? "border-accent font-medium text-fg"
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

      <Guarded permission="settings_write" what="workspace settings">
        {children}
      </Guarded>
    </div>
  );
}
