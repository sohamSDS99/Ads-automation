"use client";

import { useId, useRef, type ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * A tab strip with real keyboard semantics.
 *
 * Hand-written rather than pulled in as another primitive: this is the one
 * pattern the ARIA authoring practices specify completely — arrows move,
 * Home/End jump, only the selected tab is in the tab order — and implementing
 * it is smaller than the dependency.
 */
export type TabItem = { id: string; label: string; badge?: ReactNode };

export function Tabs({
  items,
  value,
  onChange,
  label,
  className,
}: {
  items: TabItem[];
  value: string;
  onChange: (id: string) => void;
  label: string;
  className?: string;
}) {
  const group = useId();
  const strip = useRef<HTMLDivElement>(null);

  const move = (delta: number | "first" | "last") => {
    const index = items.findIndex((item) => item.id === value);
    const next =
      delta === "first"
        ? 0
        : delta === "last"
          ? items.length - 1
          : (index + delta + items.length) % items.length;
    const target = items[next];
    if (!target) return;
    onChange(target.id);
    strip.current?.querySelector<HTMLButtonElement>(`#${CSS.escape(`${group}-${target.id}`)}`)?.focus();
  };

  return (
    <div
      ref={strip}
      role="tablist"
      aria-label={label}
      className={cn(
        // Scrolls rather than clips: four tabs do not fit 390px, and a tab
        // nobody can reach is worse than one they have to swipe to.
        "flex items-center gap-1 overflow-x-auto border-b px-2 [scrollbar-width:none]",
        className,
      )}
      onKeyDown={(event) => {
        const keys: Record<string, number | "first" | "last"> = {
          ArrowRight: 1,
          ArrowLeft: -1,
          Home: "first",
          End: "last",
        };
        const action = keys[event.key];
        if (action === undefined) return;
        event.preventDefault();
        move(action);
      }}
    >
      {items.map((item) => {
        const selected = item.id === value;
        return (
          <button
            key={item.id}
            id={`${group}-${item.id}`}
            type="button"
            role="tab"
            aria-selected={selected}
            tabIndex={selected ? 0 : -1}
            onClick={() => onChange(item.id)}
            className={cn(
              "-mb-px inline-flex shrink-0 items-center gap-1.5 border-b-2 px-2 py-2 text-sm transition-colors",
              selected
                ? "border-accent font-medium text-fg"
                : "border-transparent text-fg-muted hover:text-fg",
            )}
          >
            {item.label}
            {item.badge ? <span className="text-xs text-fg-subtle">{item.badge}</span> : null}
          </button>
        );
      })}
    </div>
  );
}
