"use client";

import { CircleAlert } from "lucide-react";
import { useId, type ReactNode } from "react";

import { Input, type InputProps } from "@/components/ui/input";
import { cn } from "@/lib/utils";

type FieldProps = Omit<InputProps, "id"> & {
  label: string;
  /** Shown under the field, in place of the hint, when set. */
  error?: string;
  hint?: ReactNode;
};

/**
 * A labelled input with its error wired up for screen readers.
 *
 * The error replaces the hint rather than stacking under it, so the field never
 * changes height between states and the form does not jump as you tab through it.
 */
export function Field({ label, error, hint, className, ...props }: FieldProps) {
  const id = useId();
  const messageId = `${id}-message`;
  const hasMessage = Boolean(error ?? hint);

  return (
    <div className="flex flex-col gap-1.5">
      <label htmlFor={id} className="text-sm font-medium text-fg">
        {label}
      </label>
      <Input
        id={id}
        invalid={Boolean(error)}
        aria-describedby={hasMessage ? messageId : undefined}
        className={className}
        {...props}
      />
      <p
        id={messageId}
        aria-live="polite"
        className={cn(
          "flex min-h-4 items-center gap-1.5 text-xs",
          error ? "text-status-failed" : "text-fg-muted",
        )}
      >
        {error ? (
          <>
            <CircleAlert className="size-3.5 shrink-0" aria-hidden />
            {error}
          </>
        ) : (
          hint
        )}
      </p>
    </div>
  );
}
