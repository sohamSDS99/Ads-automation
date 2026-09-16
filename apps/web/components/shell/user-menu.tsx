"use client";

import { LogOut, UserRound } from "lucide-react";
import { useRouter } from "next/navigation";
import { useState } from "react";

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { RoleBadge } from "@/components/ui/role-badge";
import { Spinner } from "@/components/ui/spinner";
import { logout } from "@/lib/api";
import { ROLE_DESCRIPTION } from "@/lib/permissions";
import { useSession } from "@/lib/session";

export function UserMenu() {
  const { user } = useSession();
  const router = useRouter();
  const [signingOut, setSigningOut] = useState(false);

  async function signOut() {
    setSigningOut(true);
    try {
      await logout();
    } finally {
      // Even if the call failed, the safe move is to send them to /login and
      // let the server decide what their cookie is still worth.
      router.replace("/login");
      router.refresh();
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger
        className="flex h-9 items-center gap-2 rounded-[var(--radius)] px-2 text-sm text-fg-muted transition-colors hover:bg-surface-hover hover:text-fg data-[state=open]:bg-surface-hover data-[state=open]:text-fg"
        aria-label={`Account — ${user.name}`}
      >
        <UserRound className="size-4" aria-hidden />
        <span className="hidden max-w-32 truncate sm:inline">{user.name}</span>
      </DropdownMenuTrigger>

      <DropdownMenuContent>
        <DropdownMenuLabel>
          <p className="truncate text-sm font-medium text-fg">{user.name}</p>
          <p className="mt-0.5 truncate text-xs text-fg-muted">{user.email}</p>
          <RoleBadge role={user.role} className="mt-2" />
          <p className="mt-1.5 text-xs text-fg-subtle">{ROLE_DESCRIPTION[user.role]}</p>
        </DropdownMenuLabel>

        <DropdownMenuSeparator />

        {/* The account screen arrives with settings in P8. Saying so beats a
            dead link that looks like a bug. */}
        <DropdownMenuItem disabled>
          <UserRound aria-hidden />
          Account settings
          <span className="ml-auto text-xs text-fg-subtle">P8</span>
        </DropdownMenuItem>

        <DropdownMenuItem
          onSelect={(event) => {
            event.preventDefault();
            void signOut();
          }}
          disabled={signingOut}
        >
          {signingOut ? <Spinner label="Signing out" /> : <LogOut aria-hidden />}
          Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
