"use client";

import {
  useLayoutEffect,
  useRef,
  useState,
  type HTMLAttributes,
  type ReactNode,
  type ThHTMLAttributes,
  type TdHTMLAttributes,
} from "react";

import { cn } from "@/lib/utils";

/**
 * A data table.
 *
 * Wrapped in its own scroll container so a wide table scrolls itself instead of
 * the page, and the header stays put while the body moves.
 *
 * `min-w-max` is the thing to know before adding a column: it sizes the table
 * to its widest content, so a single unbounded prose cell — a joined issue
 * list, a paragraph of pricing terms — drags the table past every other column
 * and pushes the rightmost ones out of view. Any cell that can hold a sentence
 * needs its own `max-w-*`; the numbers and short labels are what `min-w-max` is
 * for.
 *
 * When the table is wider than its scroller, the scroller becomes a named,
 * focusable region, so a keyboard user can reach and scroll the columns off to
 * the right (WCAG 2.1.1; axe `scrollable-region-focusable`). A table that fits
 * adds no tab stop.
 */
export function Table({
  children,
  className,
  label,
}: {
  children: ReactNode;
  className?: string;
  /** The table's accessible name — screen readers announce it on entry. */
  label: string;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const [scrolls, setScrolls] = useState(false);
  useLayoutEffect(() => {
    const element = scroller.current;
    if (!element) return;
    const measure = () => setScrolls(element.scrollWidth > element.clientWidth + 1);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(element);
    if (element.firstElementChild) observer.observe(element.firstElementChild);
    return () => observer.disconnect();
  }, []);
  return (
    // `relative`: an `sr-only` cell is absolutely positioned, and without a
    // positioned ancestor inside the scroller it escapes the scroller's clip
    // and widens the page instead.
    <div
      ref={scroller}
      className="relative w-full overflow-x-auto focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent"
      {...(scrolls ? { tabIndex: 0, role: "region", "aria-label": label } : {})}
    >
      <table className={cn("w-full min-w-max border-collapse text-sm", className)}>
        <caption className="sr-only">{label}</caption>
        {children}
      </table>
    </div>
  );
}

export function Th({ className, ...props }: ThHTMLAttributes<HTMLTableCellElement>) {
  return (
    <th
      scope="col"
      className={cn(
        "border-b px-4 py-2.5 text-left text-xs font-medium tracking-wide text-fg-subtle",
        className,
      )}
      {...props}
    />
  );
}

export function Td({ className, ...props }: TdHTMLAttributes<HTMLTableCellElement>) {
  return <td className={cn("border-b px-4 py-3 align-middle text-fg", className)} {...props} />;
}

export function Tr({ className, ...props }: HTMLAttributes<HTMLTableRowElement>) {
  return <tr className={cn("hover:bg-surface-hover/60", className)} {...props} />;
}
