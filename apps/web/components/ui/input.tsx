"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

export type InputProps = React.InputHTMLAttributes<HTMLInputElement> & {
  invalid?: boolean;
};

export const Input = React.forwardRef<HTMLInputElement, InputProps>(function Input(
  { className, invalid, ...props },
  ref,
) {
  return (
    <input
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        // One elevation cue: the border. No shadow under a bordered field.
        "h-10 w-full rounded-[var(--radius)] border bg-surface-raised px-3 text-sm text-fg",
        "placeholder:text-fg-subtle",
        "transition-[border-color,background-color] duration-150",
        "hover:border-border-strong",
        "disabled:cursor-not-allowed disabled:bg-surface disabled:text-fg-muted",
        invalid && "border-status-failed hover:border-status-failed",
        className,
      )}
      {...props}
    />
  );
});
