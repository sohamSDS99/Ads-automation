"use client";

import { FileSearch, FolderKanban, Settings, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import type { Permission } from "@/lib/permissions";
import { APPROVAL_POLL_MS, useApprovals } from "@/lib/queries";
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
  // `read`, not `settings_write`: Settings now holds the project setup an
  // operator owns, and every tab on it is readable by any member.
  { href: "/settings", label: "Settings", icon: Settings, permission: "read" },
] as const satisfies readonly { href: string; label: string; icon: unknown; permission: Permission }[];

export function Sidebar() {
  const pathname = usePathname();
  const { has } = useSession();
  const visible = NAV.filter((item) => has(item.permission));
  const waiting = useWaitingCount(has("approval_decide"));

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
                <span className="relative flex shrink-0">
                  <Icon className="size-4" aria-hidden />
                  {/* On the narrow rail there is no room for a count, so the
                      same fact becomes a dot on the icon. */}
                  {href === "/approvals" && waiting > 0 ? (
                    <span
                      aria-hidden
                      className="absolute -top-0.5 -right-0.5 size-1.5 rounded-full bg-status-gate md:hidden"
                    />
                  ) : null}
                </span>
                {/* `sr-only` rather than `hidden`: on the narrow rail the
                    label is the link's accessible name, and an icon with no
                    name is an unlabelled link. */}
                <span className="sr-only md:not-sr-only">{label}</span>
                {href === "/approvals" && waiting > 0 ? (
                  <span
                    data-numeric
                    className="ml-auto hidden rounded-full bg-status-gate/15 px-1.5 py-0.5 text-xs font-medium text-fg md:inline"
                  >
                    {waiting}
                    <span className="sr-only"> waiting on you</span>
                  </span>
                ) : null}
              </Link>
            </li>
          );
        })}
      </ul>

      <p className="hidden px-4 py-3 text-xs text-fg-subtle md:block">Stage 01 · Research</p>
    </nav>
  );
}

/**
 * How many gates are waiting on this person.
 *
 * Polled once a minute (PRD §13.4 F) and invalidated by the run console the
 * moment a gate opens over SSE, so someone already watching a run sees the
 * badge move without waiting for the next poll.
 */
function useWaitingCount(enabled: boolean): number {
  const query = useApprovals({ mine: true, status: "pending" }, { pollMs: APPROVAL_POLL_MS, enabled });
  return query.data?.items.length ?? 0;
}
