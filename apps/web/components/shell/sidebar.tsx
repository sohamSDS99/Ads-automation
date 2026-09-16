"use client";

import { FileSearch, FolderKanban, Settings, ShieldCheck } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

import { cn } from "@/lib/utils";

const NAV = [
  { href: "/", label: "Projects", icon: FolderKanban },
  { href: "/approvals", label: "Approvals", icon: ShieldCheck },
  { href: "/evidence", label: "Evidence", icon: FileSearch },
  { href: "/settings", label: "Settings", icon: Settings },
] as const;

export function Sidebar() {
  const pathname = usePathname();

  return (
    <nav aria-label="Main" className="flex h-full w-56 shrink-0 flex-col border-r bg-surface">
      <div className="flex h-14 items-center gap-2 border-b px-4">
        <span className="size-2 rounded-full bg-accent" aria-hidden />
        <span className="text-sm font-semibold tracking-tight">Research Agent</span>
      </div>

      <ul className="flex flex-1 flex-col gap-1 p-2">
        {NAV.map(({ href, label, icon: Icon }) => {
          const active = href === "/" ? pathname === "/" : pathname.startsWith(href);
          return (
            <li key={href}>
              <Link
                href={href}
                aria-current={active ? "page" : undefined}
                className={cn(
                  "flex items-center gap-3 rounded-[var(--radius)] px-3 py-2 text-sm transition-colors",
                  active
                    ? "bg-accent-soft text-accent font-medium"
                    : "text-fg-muted hover:bg-surface-hover hover:text-fg",
                )}
              >
                <Icon className="size-4" aria-hidden />
                {label}
              </Link>
            </li>
          );
        })}
      </ul>

      <p className="px-4 py-3 text-xs text-fg-subtle">Stage 01 · Research</p>
    </nav>
  );
}
