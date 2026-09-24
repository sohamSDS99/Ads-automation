"use client";

import {
  FileSearch,
  FolderKanban,
  Settings,
  ShieldCheck,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { StageNav } from "@/components/shell/stage-nav";
import type { Permission } from "@/lib/permissions";
import { APPROVAL_POLL_MS, useApprovals } from "@/lib/queries";
import { useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

type NavItem = { href: string; label: string; icon: LucideIcon; permission: Permission };

/**
 * The pages that are not a pipeline stage, with the permission each
 * destination actually needs (PRD §13.3).
 *
 * These sit at the foot of the panel. The stages are the work, and they own
 * the top; this is the shelf under it — where you go when you are done with
 * the project in front of you, not while you are in it.
 *
 * Filtering here is about not offering a door that is locked — the API refuses
 * the route regardless of what this renders.
 */
const PAGES = [
  { href: "/", label: "Projects", icon: FolderKanban, permission: "read" },
  { href: "/approvals", label: "Approvals", icon: ShieldCheck, permission: "approval_decide" },
  { href: "/evidence", label: "Evidence", icon: FileSearch, permission: "read" },
] as const satisfies readonly NavItem[];

/**
 * Settings sits on its own below the rest: it configures the app rather than
 * being another place in it, and it is the one row people hunt for by
 * position instead of by name.
 *
 * `read`, not `settings_write`: Settings now holds the project setup an
 * operator owns, and every tab on it is readable by any member.
 */
const SETTINGS = {
  href: "/settings",
  label: "Settings",
  icon: Settings,
  permission: "read",
} as const satisfies NavItem;

export function Sidebar() {
  const pathname = usePathname();
  const { has } = useSession();
  const waiting = useWaitingCount(has("approval_decide"));
  const projectId = projectIdFrom(pathname);
  const pages = PAGES.filter((item) => has(item.permission));

  return (
    <div className="flex h-full w-14 shrink-0 flex-col border-r bg-surface md:w-60">
      <div className="flex h-14 shrink-0 items-center justify-center gap-2 border-b px-4 md:justify-start">
        <span className="size-2 rounded-full bg-accent" aria-hidden />
        <span className="hidden text-sm font-semibold tracking-tight md:inline">
          Research Agent
        </span>
      </div>

      {/* The stages belong to a project, so they are here when there is one
          and not before. A stage row outside a project is a door onto
          nothing: there is no "current project" in this product to fall back
          on, and picking the last one somebody opened would navigate them
          somewhere they did not name. */}
      {projectId === null ? null : <StageNav projectId={projectId} />}

      {/* Pinned to the foot on every route, so the stage group arriving above
          it never moves it. Somewhere to look that does not depend on where
          you already are is most of what a side panel is for. */}
      <div aria-hidden className="flex-1" />

      <nav aria-label="Main" className="shrink-0 border-t p-2">
        <ul className="flex flex-col gap-1">
          {pages.map((item) => (
            <li key={item.href}>
              <NavRow
                item={item}
                active={isActive(item.href, pathname)}
                badge={item.href === "/approvals" ? waiting : 0}
              />
            </li>
          ))}
        </ul>

        {has(SETTINGS.permission) ? (
          <ul className="mt-2 flex flex-col gap-1 border-t pt-2">
            <li>
              <NavRow item={SETTINGS} active={isActive(SETTINGS.href, pathname)} />
            </li>
          </ul>
        ) : null}
      </nav>
    </div>
  );
}

function NavRow({
  item,
  active,
  badge = 0,
}: {
  item: NavItem;
  active: boolean;
  badge?: number;
}) {
  const Icon = item.icon;
  return (
    <Link
      href={item.href}
      aria-current={active ? "page" : undefined}
      title={item.label}
      className={cn(
        "flex items-center gap-3 rounded-[var(--radius)] py-2 text-sm transition-colors",
        "justify-center px-0 md:justify-start md:px-3",
        active
          ? "bg-accent-soft font-medium text-accent-soft-fg"
          : "text-fg-muted hover:bg-surface-hover hover:text-fg",
      )}
    >
      <span className="relative flex shrink-0">
        <Icon className="size-4" aria-hidden />
        {/* On the narrow rail there is no room for a count, so the same fact
            becomes a dot on the icon. */}
        {badge > 0 ? (
          <span
            aria-hidden
            className="absolute -top-0.5 -right-0.5 size-1.5 rounded-full bg-status-gate md:hidden"
          />
        ) : null}
      </span>
      {/* `sr-only` rather than `hidden`: on the narrow rail the label is the
          link's accessible name, and an icon with no name is an unlabelled
          link. */}
      <span className="sr-only md:not-sr-only md:min-w-0 md:flex-1 md:truncate">{item.label}</span>
      {badge > 0 ? (
        <span
          data-numeric
          className="hidden shrink-0 rounded-full bg-status-gate/15 px-1.5 py-0.5 text-xs font-medium text-fg md:inline"
        >
          {badge}
          <span className="sr-only"> waiting on you</span>
        </span>
      ) : null}
    </Link>
  );
}

/** Whether a destination is the one being looked at. */
function isActive(href: string, pathname: string): boolean {
  // The project list is at `/`, so it matches exactly or it would match
  // everything. A project route highlights a stage instead, which is the
  // more specific answer to "where am I".
  return href === "/" ? pathname === "/" : pathname.startsWith(href);
}

/**
 * The project in the URL, or null.
 *
 * `/projects/[id]` is the only route shape that carries one: the list lives at
 * `/`, so there is no `/projects` index to mistake for an id.
 */
function projectIdFrom(pathname: string): string | null {
  return /^\/projects\/([^/]+)/.exec(pathname)?.[1] ?? null;
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
