"use client";

import { UserRound } from "lucide-react";

import { HealthDot } from "@/components/shell/health-dot";
import { ThemeToggle } from "@/components/shell/theme-toggle";
import { Button } from "@/components/ui/button";

export function Topbar() {
  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b bg-surface px-4">
      <HealthDot />
      <div className="flex items-center gap-1">
        <ThemeToggle />
        {/* Replaced by the real session menu in P0b. */}
        <Button variant="ghost" size="icon" aria-label="Account" disabled>
          <UserRound />
        </Button>
      </div>
    </header>
  );
}
