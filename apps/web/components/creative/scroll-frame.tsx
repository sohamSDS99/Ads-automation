"use client";

import { useLayoutEffect, useRef, useState, type ReactNode } from "react";

import { cn } from "@/lib/utils";

/**
 * A horizontal scroller for something shown at true scale — a desktop ad
 * preview is 600 px wide whatever the screen, and shrinking it to fit would be
 * a lie about how it renders (§15.2 rule 1).
 *
 * When its content is wider than it, it becomes a named, focusable region so a
 * keyboard user can reach and scroll it (WCAG 2.1.1; axe
 * `scrollable-region-focusable`); when it fits it adds no tab stop — the same
 * contract as `Table`.
 */
export function ScrollFrame({
  label,
  className,
  children,
}: {
  label: string;
  className?: string;
  children: ReactNode;
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
    <div
      ref={scroller}
      className={cn(
        "max-w-full overflow-x-auto rounded-token focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent",
        className,
      )}
      {...(scrolls ? { tabIndex: 0, role: "region", "aria-label": label } : {})}
    >
      {children}
    </div>
  );
}
