"use client";

import { CircleAlert, CircleCheck, Info, TriangleAlert } from "lucide-react";
import { Toaster as Sonner, toast } from "sonner";

/**
 * Toasts, themed to the tokens.
 *
 * Every mutating action in this app is optimistic and rolls back on failure
 * (PRD §13.5 #5), which only works if the rollback says something. That is what
 * these are for — confirmation is secondary.
 */
export function Toaster() {
  return (
    <Sonner
      position="bottom-right"
      gap={8}
      offset={16}
      toastOptions={{
        unstyled: true,
        classNames: {
          toast:
            "flex w-full items-start gap-2.5 rounded-[var(--radius)] border bg-surface-raised px-3.5 py-3 text-sm text-fg shadow-[var(--shadow-overlay)]",
          title: "font-medium",
          description: "text-fg-muted",
          actionButton:
            "ml-auto shrink-0 rounded-[calc(var(--radius)-4px)] bg-accent px-2.5 py-1 text-xs font-medium text-accent-fg",
          closeButton: "text-fg-subtle",
        },
      }}
      icons={{
        success: <CircleCheck className="size-4 shrink-0 text-status-success" aria-hidden />,
        error: <CircleAlert className="size-4 shrink-0 text-status-failed" aria-hidden />,
        info: <Info className="size-4 shrink-0 text-fg-subtle" aria-hidden />,
        // Same glyph and same colour as `Alert tone="warning"`. A toast that
        // means "it half worked" must not look like one that means "done".
        warning: <TriangleAlert className="size-4 shrink-0 text-status-gate" aria-hidden />,
      }}
    />
  );
}

export { toast };
