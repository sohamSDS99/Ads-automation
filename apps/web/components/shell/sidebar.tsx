"use client";

import { FileSearch, FolderKanban, Settings, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import type { Permission } from "@/lib/permissions";
import { useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

/**
 * Navigation, with the permission each destination actually needs (PRD §13.3).
 *
 * Filtering here is about not offering a door that is locked — the API refuses
 * the route regardless of what this renders.
 */
const NAV = [
  { href: "/", label: "Projects", icon: FolderKanban, permission: "read" },
  { href: "/approvals", label: "Approvals", icon: ShieldCheck, permission: "approval_decide" },
  { href: "/evidence", label: "Evidence", icon: FileSearch, permission: "read" },
  { href: "/settings", label: "Settings", icon: Settings, permission: "settings_write" },
] as const satisfies readonly { href: string; label: string; icon: unknown; permission: Permission }[];

export function Sidebar() {
  const pathname = usePathname();
  const { has } = useSession();
  const visible = NAV.filter((item) => has(item.permission));

  return (
    <nav aria-label="Main" className="flex h-full w-14 shrink-0 flex-col border-r bg-surface md:w-56">
      <div className="flex h-14 items-center justify-center gap-2 border-b px-4 md:justify-start">
        <span className="size-2 rounded-full bg-accent" aria-hidden />
        <span className="hidden text-sm font-semibold tracking-tight md:inline">
          Research Agent
        </span>
      </div>

      <ul className="flex flex-1 flex-col gap-1 p-2">
        {visible.map(({ href, label, icon: Icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <li key={href}>
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                title={label}
                className={cn(
                  "flex items-center gap-3 rounded-[var(--radius)] py-2 text-sm transition-colors",
                  "justify-center px-0 md:justify-start md:px-3",
                  active
                    ? "bg-accent-soft font-medium text-accent"
                    : "text-fg-muted hover:bg-surface-hover hover:text-fg",
                )}
              >
                <Icon className="size-4 shrink-0" aria-hidden />
                {/* `sr-only` rather than `hidden`: on the narrow rail the
                    label is the link's accessible name, and an icon with no
                    name is an unlabelled link. */}
                <span className="sr-only md:not-sr-only">{label}</span>
              </Link>
            </li>
          );
        })}
      </ul>

      <p className="hidden px-4 py-3 text-xs text-fg-subtle md:block">Stage 01 · Research</p>
    </nav>
  );
}
