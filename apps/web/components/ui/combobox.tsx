"use client";

import * as PopoverPrimitive from "@radix-ui/react-popover";
import { Command } from "cmdk";
import { Check, ChevronsUpDown, Search } from "lucide-react";
import { useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

export type ComboboxItem = {
  value: string;
  /** What the person types to find it. */
  search: string;
  /** The row in the open list — free to be richer than the trigger. */
  render: ReactNode;
  /** The trigger's label once chosen. */
  label: ReactNode;
  disabled?: boolean;
};

/**
 * A searchable picker for a list too long to scroll.
 *
 * Built for the model routing table, where there are several hundred options
 * and the choice depends on numbers that have to be visible per row — a native
 * `<select>` can show a string and nothing else.
 */
export function Combobox({
  items,
  value,
  onChange,
  placeholder = "Search…",
  emptyLabel = "Nothing matches",
  triggerLabel,
  disabled,
  id,
  className,
}: {
  items: ComboboxItem[];
  value: string | null;
  onChange: (value: string) => void;
  placeholder?: string;
  emptyLabel?: string;
  /** Shown when nothing is selected yet. */
  triggerLabel: ReactNode;
  disabled?: boolean;
  id?: string;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  const selected = items.find((item) => item.value === value);

  return (
    <PopoverPrimitive.Root open={open} onOpenChange={setOpen}>
      <PopoverPrimitive.Trigger
        id={id}
        disabled={disabled}
        role="combobox"
        aria-expanded={open}
        className={cn(
          "flex h-9 w-full items-center justify-between gap-2 rounded-[var(--radius)] border",
          "bg-surface-raised px-3 text-left text-sm text-fg",
          "transition-[border-color,background-color] duration-150 hover:border-border-strong",
          "disabled:cursor-not-allowed disabled:bg-surface disabled:text-fg-muted",
          className,
        )}
      >
        <span className="min-w-0 truncate">{selected ? selected.label : triggerLabel}</span>
        <ChevronsUpDown className="size-4 shrink-0 text-fg-subtle" aria-hidden />
      </PopoverPrimitive.Trigger>

      <PopoverPrimitive.Portal>
        <PopoverPrimitive.Content
          align="start"
          sideOffset={6}
          className={cn(
            "z-50 w-[var(--radix-popover-trigger-width)] min-w-72 overflow-hidden",
            "rounded-[var(--radius)] border bg-surface-raised shadow-[var(--shadow-overlay)]",
            "menu-surface",
          )}
        >
          <Command loop className="flex max-h-80 flex-col">
            <div className="flex items-center gap-2 border-b px-3">
              <Search className="size-4 shrink-0 text-fg-subtle" aria-hidden />
              <Command.Input
                placeholder={placeholder}
                className="h-10 w-full bg-transparent text-sm text-fg outline-none placeholder:text-fg-subtle"
              />
            </div>
            <Command.List className="overflow-y-auto p-1">
              <Command.Empty className="px-3 py-6 text-center text-sm text-fg-muted">
                {emptyLabel}
              </Command.Empty>
              {items.map((item) => (
                <Command.Item
                  key={item.value}
                  value={item.search}
                  disabled={item.disabled}
                  onSelect={() => {
                    onChange(item.value);
                    setOpen(false);
                  }}
                  className={cn(
                    "flex cursor-pointer select-none items-center gap-2 rounded-[calc(var(--radius)-4px)]",
                    "px-2.5 py-2 text-sm text-fg outline-none",
                    "data-[selected=true]:bg-surface-hover",
                    "data-[disabled=true]:pointer-events-none data-[disabled=true]:text-fg-subtle",
                  )}
                >
                  <Check
                    aria-hidden
                    className={cn(
                      "size-4 shrink-0 text-accent",
                      item.value === value ? "opacity-100" : "opacity-0",
                    )}
                  />
                  <span className="min-w-0 flex-1">{item.render}</span>
                </Command.Item>
              ))}
            </Command.List>
          </Command>
        </PopoverPrimitive.Content>
      </PopoverPrimitive.Portal>
    </PopoverPrimitive.Root>
  );
}
