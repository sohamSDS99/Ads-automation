"use client";

import * as TooltipPrimitive from "@radix-ui/react-tooltip";
import type { ReactNode } from "react";

import { cn } from "@/lib/utils";

export const TooltipProvider = TooltipPrimitive.Provider;

/**
 * A short explanation on hover and on focus.
 *
 * Mostly used to say *why* a control is disabled — which is the one case the
 * primitive cannot do alone, because a disabled button fires no pointer events.
 * `wrapDisabled` puts a focusable span around it so the reason is reachable by
 * mouse and by keyboard instead of being invisible to both.
 */
export function Tooltip({
  content,
  children,
  side = "top",
  wrapDisabled = false,
}: {
  content: ReactNode;
  children: ReactNode;
  side?: "top" | "right" | "bottom" | "left";
  wrapDisabled?: boolean;
}) {
  if (!content) return <>{children}</>;
  return (
    <TooltipPrimitive.Root>
      <TooltipPrimitive.Trigger asChild>
        {wrapDisabled ? (
          <span tabIndex={0} className="inline-flex rounded-[var(--radius)]">
            {children}
          </span>
        ) : (
          children
        )}
      </TooltipPrimitive.Trigger>
      <TooltipPrimitive.Portal>
        <TooltipPrimitive.Content
          side={side}
          sideOffset={6}
          className={cn(
            "z-50 max-w-72 rounded-[calc(var(--radius)-2px)] border bg-surface-raised",
            "px-2.5 py-1.5 text-xs text-fg shadow-[var(--shadow-overlay)]",
            "menu-surface",
          )}
        >
          {content}
        </TooltipPrimitive.Content>
      </TooltipPrimitive.Portal>
    </TooltipPrimitive.Root>
  );
}
