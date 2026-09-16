"use client";

import * as React from "react";

import { cn } from "@/lib/utils";

export type TextareaProps = React.TextareaHTMLAttributes<HTMLTextAreaElement> & {
  invalid?: boolean;
};

export const Textarea = React.forwardRef<HTMLTextAreaElement, TextareaProps>(function Textarea(
  { className, invalid, ...props },
  ref,
) {
  return (
    <textarea
      ref={ref}
      aria-invalid={invalid || undefined}
      className={cn(
        "w-full rounded-[var(--radius)] border bg-surface-raised px-3 py-2.5 text-sm text-fg",
        // Long-form fields are read as prose, so they get a prose measure and
        // line height rather than the single-line input's.
        "min-h-24 leading-relaxed",
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
