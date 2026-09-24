"use client";

import { useId } from "react";

import { cn } from "@/lib/utils";

export type SegmentOption<T extends string> = { value: T; label: string; hint?: string };

/**
 * A short exclusive choice shown all at once — `2 | 3`, `1K 2K 4K`.
 *
 * Native radio inputs under the drawn segments, so the keyboard contract is
 * the platform's: one tab stop for the group, arrow keys move the choice. The
 * chosen segment is marked by a border, weight and ink together, never by a
 * fill colour alone. For more than a handful of options use a select.
 */
export function SegmentedControl<T extends string>({
  label,
  options,
  value,
  onChange,
  className,
  describedBy,
}: {
  /** The group's accessible name. */
  label: string;
  options: SegmentOption<T>[];
  value: T | null;
  onChange: (value: T) => void;
  className?: string;
  describedBy?: string;
}) {
  const name = useId();
  return (
    <div
      role="radiogroup"
      aria-label={label}
      aria-describedby={describedBy}
      // `w-fit`: as a flex-column child it would otherwise stretch to the
      // column and draw an empty track beside the last segment.
      className={cn("inline-flex w-fit max-w-full flex-wrap gap-0.5 rounded-token border bg-surface p-0.5", className)}
    >
      {options.map((option) => (
        <label key={option.value} className="relative" title={option.hint}>
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={value === option.value}
            onChange={() => onChange(option.value)}
            className="peer sr-only"
          />
          <span
            className={cn(
              "block cursor-pointer select-none rounded-token border border-transparent px-3 py-1 text-sm tabular-nums text-fg-muted",
              "transition-colors duration-150 hover:text-fg motion-reduce:transition-none",
              "peer-checked:border-border-strong peer-checked:bg-surface-raised peer-checked:font-medium peer-checked:text-fg",
              "peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-accent",
            )}
          >
            {option.label}
          </span>
        </label>
      ))}
    </div>
  );
}
