"use client";

import { cn } from "@/lib/utils";

/**
 * An on/off control for a setting that takes effect as it is flipped.
 *
 * A `<button role="switch">`, so Space and Enter both toggle it and a screen
 * reader announces "on" or "off". The off track is `fg-subtle`, not a border
 * grey: a control has to clear 3:1 against the surface it sits on (WCAG 2.2
 * 1.4.11), and `border-strong` on white is 1.5:1. State is carried by the
 * thumb's position as well as the colour, never by colour alone.
 */
export function Switch({
  checked,
  onCheckedChange,
  id,
  disabled,
  className,
  ...aria
}: {
  checked: boolean;
  onCheckedChange: (checked: boolean) => void;
  id?: string;
  disabled?: boolean;
  className?: string;
  "aria-label"?: string;
  "aria-labelledby"?: string;
  "aria-describedby"?: string;
}) {
  return (
    <button
      type="button"
      role="switch"
      id={id}
      aria-checked={checked}
      disabled={disabled}
      onClick={() => onCheckedChange(!checked)}
      data-state={checked ? "checked" : "unchecked"}
      className={cn(
        "group inline-flex h-5 w-9 shrink-0 items-center rounded-full p-0.5",
        "bg-fg-subtle transition-colors duration-150 motion-reduce:transition-none",
        "data-[state=checked]:bg-accent disabled:cursor-not-allowed disabled:opacity-50",
        className,
      )}
      {...aria}
    >
      <span
        aria-hidden
        className={cn(
          "size-4 rounded-full bg-surface-raised transition-transform duration-150 motion-reduce:transition-none",
          "group-data-[state=checked]:translate-x-4",
        )}
      />
    </button>
  );
}
