"use client";

import { ChevronDown } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

export type SelectProps = React.SelectHTMLAttributes<HTMLSelectElement> & { invalid?: boolean };

/**
 * A native `<select>`, styled.
 *
 * Native on purpose: these appear inline in tables and in a long form, where
 * the platform's own picker is faster to operate, works on a phone, and needs
 * no listbox of our own to keep accessible.
 */
export const Select = React.forwardRef<HTMLSelectElement, SelectProps>(function Select(
  { className, invalid, children, ...props },
  ref,
) {
  return (
    <div className="relative">
      <select
        ref={ref}
        aria-invalid={invalid || undefined}
        className={cn(
          "h-9 w-full appearance-none rounded-[var(--radius)] border bg-surface-raised",
          "pl-3 pr-8 text-sm text-fg",
          "transition-[border-color,background-color] duration-150",
          "hover:border-border-strong",
          "disabled:cursor-not-allowed disabled:bg-surface disabled:text-fg-muted",
          invalid && "border-status-failed hover:border-status-failed",
          className,
        )}
        {...props}
      >
        {children}
      </select>
      <ChevronDown
        aria-hidden
        className="pointer-events-none absolute right-2.5 top-1/2 size-4 -translate-y-1/2 text-fg-subtle"
      />
    </div>
  );
});
