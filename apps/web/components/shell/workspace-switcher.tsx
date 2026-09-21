"use client";

import { Check, ChevronsUpDown, Eye, Plus, Settings2 } from "lucide-react";
import Link from "next/link";
import { useState } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Spinner } from "@/components/ui/spinner";
import { toast } from "@/components/ui/toast";
import { ApiError } from "@/lib/api";
import { ROLE_LABEL } from "@/lib/permissions";
import { useSwitchWorkspace } from "@/lib/queries";
import { useSession } from "@/lib/session";
import { cn } from "@/lib/utils";

/**
 * Which workspace you are in, and how to leave it.
 *
 * The topbar used to print the workspace name as static text, which was
 * correct when there was exactly one. Now it is the answer to a question
 * people will ask themselves several times a day — *whose data am I looking
 * at?* — so it is the first thing in the bar, it is legible at a glance, and
 * the way out of it is the same control.
 *
 * Someone with one workspace and no reason to have another still gets the
 * plain label. A menu whose only item is the page you are already on is a door
 * with a wall behind it.
 */
export function WorkspaceSwitcher() {
  const { user } = useSession();
  const [open, setOpen] = useState(false);
  const switcher = useSwitchWorkspace();

  const others = user.workspaces.filter((workspace) => workspace.id !== user.workspace_id);
  const interactive = others.length > 0 || user.is_superadmin;

  if (!interactive) {
    return (
      <span className="hidden min-w-0 truncate text-sm font-medium text-fg md:inline">
        {user.workspace_name}
      </span>
    );
  }

  function choose(id: string) {
    setOpen(false);
    switcher.mutate(id, {
      onError: (error) =>
        toast.error("Could not switch", {
          description:
            error instanceof ApiError ? error.detail : "That workspace could not be opened.",
        }),
    });
  }

  return (
    <DropdownMenu open={open} onOpenChange={setOpen}>
      <DropdownMenuTrigger
        disabled={switcher.isPending}
        className={cn(
          "flex h-9 min-w-0 items-center gap-2 rounded-[var(--radius)] px-2 text-sm transition-colors",
          "hover:bg-surface-hover data-[state=open]:bg-surface-hover",
          "disabled:pointer-events-none disabled:opacity-60",
        )}
        aria-label={`Workspace — ${user.workspace_name}. Switch workspace`}
      >
        {switcher.isPending ? <Spinner label="Switching workspace" /> : null}
        <span className="min-w-0 truncate font-medium text-fg">{user.workspace_name}</span>
        {/* Said out loud rather than implied by a missing role badge: a
            system administrator reading another company's data should be
            reminded of it by the interface, not have to infer it. */}
        {user.via_superadmin ? (
          <span className="hidden items-center gap-1 text-xs text-fg-subtle sm:inline-flex">
            <Eye className="size-3.5" aria-hidden />
            Visiting
          </span>
        ) : null}
        <ChevronsUpDown className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
      </DropdownMenuTrigger>

      <DropdownMenuContent align="start" className="max-w-80 min-w-64">
        <DropdownMenuLabel className="text-xs font-medium tracking-[0.08em] text-fg-subtle uppercase">
          Workspaces
        </DropdownMenuLabel>

        {user.workspaces.map((workspace) => {
          const current = workspace.id === user.workspace_id;
          return (
            <DropdownMenuItem
              key={workspace.id}
              onSelect={(event) => {
                event.preventDefault();
                if (!current) choose(workspace.id);
              }}
              className={cn("items-start", current && "bg-accent-soft/60")}
            >
              <Check
                className={cn("mt-0.5 size-4", current ? "text-accent!" : "opacity-0")}
                aria-hidden
              />
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="truncate font-medium text-fg">{workspace.name}</span>
                <span className="truncate text-xs text-fg-subtle">
                  {workspace.is_member ? ROLE_LABEL[workspace.role] : "Visiting as administrator"}
                </span>
              </span>
            </DropdownMenuItem>
          );
        })}

        {user.is_superadmin ? (
          <>
            <DropdownMenuSeparator />
            <DropdownMenuItem asChild>
              <Link href="/settings/workspaces?new=1">
                <Plus aria-hidden />
                New workspace
              </Link>
            </DropdownMenuItem>
            <DropdownMenuItem asChild>
              <Link href="/settings/workspaces">
                <Settings2 aria-hidden />
                Manage workspaces
              </Link>
            </DropdownMenuItem>
          </>
        ) : null}
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
